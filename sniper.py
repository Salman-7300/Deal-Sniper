#!/usr/bin/env python3
"""
Deal-Sniper (serverlos via GitHub Actions)

Prüft mydealz-RSS-Feeds gegen deine Watchlist-Regeln (watchlist.yaml):
  1. Feeds holen, Titel/Preis/Händler extrahieren
  2. Regeln matchen (Keywords + Maximalpreis)
  3. Deduplizieren über state/seen.json (wird ins Repo committet)
  4. Neue Treffer per Telegram pushen
  5. Statisches Dashboard nach docs/index.html rendern

Secrets aus Umgebungsvariablen: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yaml

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
DOCS_DIR = BASE / "docs"
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
TAG_RE = re.compile(r"<[^>]+>")
# Preise wie "139,90€", "1.299€", "89 €"
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€")

SEEN_MAX = 3000     # so viele Deal-IDs merken wir uns (Dedupe)
HITS_MAX = 150      # so viele Treffer zeigt das Dashboard


# ── State (wird vom Workflow committet) ────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ── Feeds holen & parsen ───────────────────────────────────────────────────
def parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_deals(feed_names: list[str]) -> list[dict]:
    deals = []
    run_seen: set[str] = set()  # gleicher Deal in mehreren Feeds -> nur einmal
    with httpx.Client(timeout=20, headers=UA, follow_redirects=True) as client:
        for name in feed_names:
            url = ("https://www.mydealz.de/rss/hot" if name == "hot"
                   else f"https://www.mydealz.de/rss/gruppe/{name}")
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                print(f"WARN: Feed '{name}' nicht erreichbar: {exc}", file=sys.stderr)
                continue

            for entry in feedparser.parse(resp.content).entries:
                link = getattr(entry, "link", "") or ""
                title = re.sub(r"\s+", " ", getattr(entry, "title", "")).strip()
                if not link or not title:
                    continue
                guid = hashlib.sha256(link.encode()).hexdigest()[:24]
                if guid in run_seen:
                    continue
                run_seen.add(guid)
                summary_html = getattr(entry, "summary", "") or ""
                summary = re.sub(r"\s+", " ", TAG_RE.sub(" ", summary_html)).strip()
                merchant = ""
                pm = getattr(entry, "pepper_merchant", None)
                if isinstance(pm, dict):
                    merchant = pm.get("name", "")
                deals.append({
                    "guid": guid,
                    "title": title, "link": link,
                    "price": parse_price(summary) or parse_price(title),
                    "merchant": merchant,
                    "haystack": f"{title} {summary}".lower(),
                })
    print(f"{len(deals)} Deals aus {len(feed_names)} Feeds geladen")
    return deals


# ── Regeln matchen ─────────────────────────────────────────────────────────
def match_rule(deal: dict, rule: dict) -> bool:
    hay = deal["haystack"]
    if not all(kw.lower() in hay for kw in rule.get("all", [])):
        return False
    if any(kw.lower() in hay for kw in rule.get("none", []) if kw):
        return False
    max_price = rule.get("max_price")
    if max_price is not None:
        if deal["price"] is None or deal["price"] > float(max_price):
            return False
    return True


def find_hits(deals: list[dict], rules: list[dict], seen: set[str]) -> list[dict]:
    hits = []
    for deal in deals:
        if deal["guid"] in seen:
            continue
        for rule in rules:
            if match_rule(deal, rule):
                hits.append({**deal, "rule": rule["name"],
                             "found_at": datetime.now(TZ).isoformat()})
                break  # ein Deal muss nur eine Regel treffen
    return hits


# ── Telegram ───────────────────────────────────────────────────────────────
def send_telegram(hits: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram nicht konfiguriert – Push übersprungen")
        return

    lines = [f"🎯 <b>{len(hits)} neue{'r' if len(hits) == 1 else ''} Deal-Treffer</b>\n"]
    for h in hits:
        price = f"{h['price']:.2f}".replace(".", ",") + " €" if h["price"] else "Preis s. Deal"
        merchant = f" @ {html.escape(h['merchant'])}" if h["merchant"] else ""
        lines.append(
            f"▸ <b>{html.escape(h['rule'])}</b>: "
            f'<a href="{h["link"]}">{html.escape(h["title"][:90])}</a>\n'
            f"   💶 {price}{merchant}\n"
        )
    body = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Bei sehr vielen Treffern in 4000-Zeichen-Blöcke splitten
    chunks, current = [], ""
    for line in body.splitlines(keepends=True):
        if len(current) + len(line) > 4000:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": chunk,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=30)
        if resp.status_code != 200:
            print(f"WARN: Telegram-Fehler {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return
    print(f"{len(hits)} Treffer per Telegram versendet")


# ── Dashboard ──────────────────────────────────────────────────────────────
def render_dashboard(all_hits: list[dict], rules: list[dict]) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    template = (BASE / "template.html").read_text(encoding="utf-8")

    rule_rows = []
    for r in rules:
        cond = " + ".join(html.escape(k) for k in r.get("all", []))
        maxp = (f" ≤ {float(r['max_price']):.0f} €"
                if r.get("max_price") is not None else "")
        rule_rows.append(f"<li><strong>{html.escape(r['name'])}</strong>"
                         f"<span class='cond'>{cond}{maxp}</span></li>")

    hit_rows = []
    for h in reversed(all_hits):  # neueste zuerst
        try:
            when = datetime.fromisoformat(h["found_at"]).strftime("%d.%m. %H:%M")
        except (ValueError, KeyError):
            when = "–"
        price = (f"{h['price']:.2f}".replace(".", ",") + " €") if h.get("price") else "–"
        merchant = html.escape(h.get("merchant", "") or "")
        hit_rows.append(
            f"<tr><td class='when'>{when}</td>"
            f"<td><span class='rulebadge'>{html.escape(h['rule'])}</span><br>"
            f'<a href="{h["link"]}" target="_blank" rel="noopener noreferrer">'
            f"{html.escape(h['title'][:110])}</a></td>"
            f"<td class='price'>{price}<br><span class='merchant'>{merchant}</span></td></tr>"
        )

    page = (template
            .replace("<!--RULES-->", "\n".join(rule_rows))
            .replace("<!--HITS-->", "\n".join(hit_rows) if hit_rows
                     else "<tr><td colspan='3' class='empty'>Noch keine Treffer.</td></tr>")
            .replace("<!--UPDATED-->", datetime.now(TZ).strftime("%d.%m.%Y %H:%M")))
    (DOCS_DIR / "index.html").write_text(page, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Dashboard gerendert ({len(all_hits)} Treffer insgesamt)")


# ── Main ───────────────────────────────────────────────────────────────────
def main() -> int:
    cfg = yaml.safe_load((BASE / "watchlist.yaml").read_text(encoding="utf-8"))
    rules = cfg.get("rules", [])
    if not rules:
        print("Keine Regeln in watchlist.yaml – nichts zu tun")
        return 0

    seen_list = load_json(STATE_DIR / "seen.json", [])
    seen = set(seen_list)
    all_hits = load_json(STATE_DIR / "hits.json", [])

    deals = fetch_deals(cfg.get("feeds", ["hot"]))
    hits = find_hits(deals, rules, seen)
    print(f"{len(hits)} neue Treffer")

    if hits:
        send_telegram(hits)
        all_hits = (all_hits + hits)[-HITS_MAX:]
        save_json(STATE_DIR / "hits.json", all_hits)

    # Alle gesehenen Deals merken (auch Nicht-Treffer -> keine Doppel-Prüfung)
    seen_list = (seen_list + [d["guid"] for d in deals if d["guid"] not in seen])[-SEEN_MAX:]
    save_json(STATE_DIR / "seen.json", seen_list)

    render_dashboard(all_hits, rules)
    return 0


if __name__ == "__main__":
    sys.exit(main())
