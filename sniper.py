#!/usr/bin/env python3
"""
Deal-Sniper v2 (serverlos via GitHub Actions)

Neu in v2:
  - Temperatur-Filter: Treffer unter min_temp (mydealz-Votes) werden verworfen
  - Preis-Gedächtnis: pro Regel wird jeder Treffer-Preis geloggt; der Push zeigt
    den 30-Tage-Bestpreis zum Vergleich
  - Ruhezeiten: zwischen 23 und 7 Uhr kein Push; Treffer landen in einer
    Pending-Queue und kommen gebündelt am Morgen

Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
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
PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€")
TEMP_RE = re.compile(r'"temperature"\s*:\s*(-?\d+)')

SEEN_MAX = 3000
HITS_MAX = 150
PRICE_HISTORY_DAYS = 60
QUIET_START, QUIET_END = 23, 7   # Ruhezeit: 23:00–07:00


# ── State-Helfer ───────────────────────────────────────────────────────────
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
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fetch_deals(feed_names: list[str]) -> list[dict]:
    deals, run_seen = [], set()
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
                    "guid": guid, "title": title, "link": link,
                    "price": parse_price(summary) or parse_price(title),
                    "merchant": merchant,
                    "haystack": f"{title} {summary}".lower(),
                })
    print(f"{len(deals)} Deals aus {len(feed_names)} Feeds geladen")
    return deals


def fetch_temperature(client: httpx.Client, link: str) -> int | None:
    """Deal-Seite laden und Temperatur (Votes) aus dem eingebetteten JSON ziehen.
    Erster Treffer im Dokument = der Deal selbst."""
    try:
        resp = client.get(link)
        resp.raise_for_status()
        m = TEMP_RE.search(resp.text)
        return int(m.group(1)) if m else None
    except Exception as exc:
        print(f"WARN: Temperatur für {link[:50]} nicht lesbar: {exc}", file=sys.stderr)
        return None


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


def find_hits(deals: list[dict], rules: list[dict], seen: set[str],
              default_min_temp: int) -> list[dict]:
    hits = []
    temp_client = None
    for deal in deals:
        if deal["guid"] in seen:
            continue
        for rule in rules:
            if not match_rule(deal, rule):
                continue
            # Temperatur nur für tatsächliche Treffer nachladen (spart Requests)
            min_temp = int(rule.get("min_temp", default_min_temp))
            temp = None
            if min_temp > 0:
                if temp_client is None:
                    temp_client = httpx.Client(timeout=15, headers=UA,
                                               follow_redirects=True)
                temp = fetch_temperature(temp_client, deal["link"])
                # fail-open: Temperatur nicht lesbar -> Treffer trotzdem melden
                if temp is not None and temp < min_temp:
                    print(f"  verworfen ({temp}° < {min_temp}°): {deal['title'][:50]}")
                    break
            hits.append({**deal, "rule": rule["name"], "temp": temp,
                         "found_at": datetime.now(TZ).isoformat()})
            break
    if temp_client:
        temp_client.close()
    return hits


# ── Preis-Gedächtnis ───────────────────────────────────────────────────────
def update_price_memory(hits: list[dict]) -> dict:
    """Loggt Treffer-Preise pro Regel und liefert den 30-Tage-Bestpreis zurück."""
    prices = load_json(STATE_DIR / "prices.json", {})
    cutoff_keep = (datetime.now(TZ) - timedelta(days=PRICE_HISTORY_DAYS)).isoformat()
    cutoff_best = (datetime.now(TZ) - timedelta(days=30)).isoformat()
    best30: dict[str, float] = {}

    for h in hits:
        if h["price"] is not None:
            prices.setdefault(h["rule"], []).append(
                {"ts": h["found_at"], "price": h["price"]})

    for rule, entries in list(prices.items()):
        entries[:] = [e for e in entries if e["ts"] >= cutoff_keep][-500:]
        recent = [e["price"] for e in entries if e["ts"] >= cutoff_best]
        if recent:
            best30[rule] = min(recent)

    save_json(STATE_DIR / "prices.json", prices)
    return best30


# ── Ruhezeiten ─────────────────────────────────────────────────────────────
def in_quiet_hours(now: datetime) -> bool:
    return now.hour >= QUIET_START or now.hour < QUIET_END


# ── Telegram ───────────────────────────────────────────────────────────────
def send_telegram(hits: list[dict], best30: dict, bundled: bool) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram nicht konfiguriert – Push übersprungen")
        return False

    head = ("🌅 <b>Über Nacht aufgelaufen:</b> " if bundled else "🎯 ") + \
           f"<b>{len(hits)} Deal-Treffer</b>\n"
    lines = [head]
    for h in hits:
        price = (f"{h['price']:.2f}".replace(".", ",") + " €") if h.get("price") else "Preis s. Deal"
        merchant = f" @ {html.escape(h['merchant'])}" if h.get("merchant") else ""
        temp = f" · 🔥{h['temp']}°" if h.get("temp") is not None else ""
        best = best30.get(h["rule"])
        bestline = ""
        if best is not None and h.get("price") is not None:
            tag = "🏆 neuer Bestpreis!" if h["price"] <= best else \
                  f"(Bestpreis 30 T: {f'{best:.2f}'.replace('.', ',')} €)"
            bestline = f"\n   {tag}"
        lines.append(
            f"▸ <b>{html.escape(h['rule'])}</b>: "
            f'<a href="{h["link"]}">{html.escape(h["title"][:90])}</a>\n'
            f"   💶 {price}{merchant}{temp}{bestline}\n")

    body = "\n".join(lines)
    chunks, current = [], ""
    for line in body.splitlines(keepends=True):
        if len(current) + len(line) > 4000:
            chunks.append(current); current = ""
        current += line
    if current.strip():
        chunks.append(current)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunks:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": chunk,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=30)
        if resp.status_code != 200:
            print(f"WARN: Telegram-Fehler {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return False
    print(f"{len(hits)} Treffer per Telegram versendet")
    return True


# ── Dashboard ──────────────────────────────────────────────────────────────
def render_dashboard(all_hits: list[dict], rules: list[dict], best30: dict) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    template = (BASE / "template.html").read_text(encoding="utf-8")

    rule_rows = []
    for r in rules:
        cond = " + ".join(html.escape(k) for k in r.get("all", []))
        maxp = (f" ≤ {float(r['max_price']):.0f} €"
                if r.get("max_price") is not None else "")
        best = best30.get(r["name"])
        bestp = (f"<span class='best'>Bestpreis 30 T: "
                 f"{f'{best:.2f}'.replace('.', ',')} €</span>" if best else "")
        rule_rows.append(f"<li><strong>{html.escape(r['name'])}</strong>"
                         f"<span class='cond'>{cond}{maxp}</span>{bestp}</li>")

    hit_rows = []
    for h in reversed(all_hits):
        try:
            when = datetime.fromisoformat(h["found_at"]).strftime("%d.%m. %H:%M")
        except (ValueError, KeyError):
            when = "–"
        price = (f"{h['price']:.2f}".replace(".", ",") + " €") if h.get("price") else "–"
        merchant = html.escape(h.get("merchant", "") or "")
        temp = f" · {h['temp']}°" if h.get("temp") is not None else ""
        hit_rows.append(
            f"<tr><td class='when'>{when}</td>"
            f"<td><span class='rulebadge'>{html.escape(h['rule'])}{temp}</span><br>"
            f'<a href="{h["link"]}" target="_blank" rel="noopener noreferrer">'
            f"{html.escape(h['title'][:110])}</a></td>"
            f"<td class='price'>{price}<br><span class='merchant'>{merchant}</span></td></tr>")

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
    default_min_temp = int(cfg.get("min_temp", 0))

    seen_list = load_json(STATE_DIR / "seen.json", [])
    seen = set(seen_list)
    all_hits = load_json(STATE_DIR / "hits.json", [])
    pending = load_json(STATE_DIR / "pending.json", [])

    deals = fetch_deals(cfg.get("feeds", ["hot"]))
    hits = find_hits(deals, rules, seen, default_min_temp)
    print(f"{len(hits)} neue Treffer")

    best30 = update_price_memory(hits)

    if hits:
        all_hits = (all_hits + hits)[-HITS_MAX:]
        save_json(STATE_DIR / "hits.json", all_hits)

    now = datetime.now(TZ)
    to_send = pending + hits
    if to_send:
        if in_quiet_hours(now):
            print(f"Ruhezeit ({now:%H:%M}) – {len(to_send)} Treffer in Pending-Queue")
            save_json(STATE_DIR / "pending.json", to_send)
        else:
            if send_telegram(to_send, best30, bundled=bool(pending)):
                save_json(STATE_DIR / "pending.json", [])
            else:
                save_json(STATE_DIR / "pending.json", to_send)

    seen_list = (seen_list + [d["guid"] for d in deals if d["guid"] not in seen])[-SEEN_MAX:]
    save_json(STATE_DIR / "seen.json", seen_list)

    render_dashboard(all_hits, rules, best30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
