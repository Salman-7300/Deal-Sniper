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


# ── Telegram-Commands: Regeln per Chat verwalten ───────────────────────────
def load_dynamic_rules() -> list[dict]:
    return load_json(STATE_DIR / "dynamic_rules.json", [])


def save_dynamic_rules(rules: list[dict]) -> None:
    save_json(STATE_DIR / "dynamic_rules.json", rules)


def tg_send_plain(token: str, chat_id: str, text: str) -> None:
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}, timeout=20)
    except Exception as exc:
        print(f"WARN: Antwort nicht gesendet: {exc}", file=sys.stderr)


def parse_watch_command(text: str) -> dict | None:
    """/watch RTX 4070 max 480 min_temp 50 -> Regel-Dict.
    'max <preis>' und 'min_temp <n>' sind optionale Suffixe."""
    parts = text.split()
    if not parts or parts[0].lower() not in ("/watch", "/watch@"):
        return None
    tokens = parts[1:]
    max_price = None
    min_temp = None
    keywords = []
    i = 0
    while i < len(tokens):
        low = tokens[i].lower()
        if low == "max" and i + 1 < len(tokens):
            try:
                max_price = float(tokens[i + 1].replace(",", ".").replace("€", ""))
            except ValueError:
                pass
            i += 2
        elif low in ("min_temp", "temp") and i + 1 < len(tokens):
            try:
                min_temp = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            keywords.append(tokens[i])
            i += 1
    if not keywords:
        return None
    rule = {"name": " ".join(keywords), "all": [k.lower() for k in keywords], "none": []}
    if max_price is not None:
        rule["max_price"] = max_price
    if min_temp is not None:
        rule["min_temp"] = min_temp
    return rule


def process_commands(token: str, chat_id: str, static_rule_count: int = 0) -> None:
    """Liest neue Nachrichten und verarbeitet /watch, /unwatch, /rules, /status, /help.
    Nur Nachrichten aus dem konfigurierten Chat werden akzeptiert."""
    state = load_json(STATE_DIR / "cmd_offset.json", {"offset": 0})
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": state["offset"] + 1,
                                 "allowed_updates": '["message"]', "timeout": 0},
                         timeout=30)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as exc:
        print(f"WARN: Command-Abruf fehlgeschlagen: {exc}", file=sys.stderr)
        return

    rules = load_dynamic_rules()
    changed = False
    for upd in updates:
        state["offset"] = max(state["offset"], upd["update_id"])
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue  # nur dein eigener Chat darf Regeln ändern
        text = (msg.get("text") or "").strip()
        low = text.lower()

        if low.startswith("/watch"):
            rule = parse_watch_command(text)
            if not rule:
                tg_send_plain(token, chat_id,
                    "⚠️ Format: <code>/watch RTX 4070 max 480 min_temp 50</code>")
                continue
            rules = [r for r in rules if r["name"].lower() != rule["name"].lower()]
            rules.append(rule)
            changed = True
            extra = []
            if "max_price" in rule:
                extra.append(f"≤ {rule['max_price']:.0f} €")
            if "min_temp" in rule:
                extra.append(f"≥ {rule['min_temp']}°")
            tg_send_plain(token, chat_id,
                f"✅ Regel gespeichert: <b>{html.escape(rule['name'])}</b>"
                + (f" ({', '.join(extra)})" if extra else ""))

        elif low.startswith("/unwatch"):
            target = text[len("/unwatch"):].strip().lower()
            before = len(rules)
            rules = [r for r in rules if r["name"].lower() != target]
            if len(rules) < before:
                changed = True
                tg_send_plain(token, chat_id, f"🗑 Regel entfernt: <b>{html.escape(target)}</b>")
            else:
                tg_send_plain(token, chat_id,
                    f"Keine Regel namens „{html.escape(target)}“ gefunden. "
                    "<code>/rules</code> zeigt alle.")

        elif low.startswith("/rules"):
            if not rules:
                tg_send_plain(token, chat_id, "Keine per Chat angelegten Regeln. "
                    "Neu: <code>/watch &lt;begriffe&gt; max &lt;preis&gt;</code>")
            else:
                lines = ["<b>Deine Chat-Regeln:</b>"]
                for r in rules:
                    extra = []
                    if "max_price" in r:
                        extra.append(f"≤ {r['max_price']:.0f} €")
                    if "min_temp" in r:
                        extra.append(f"≥ {r['min_temp']}°")
                    lines.append(f"• <b>{html.escape(r['name'])}</b>"
                                 + (f" ({', '.join(extra)})" if extra else ""))
                tg_send_plain(token, chat_id, "\n".join(lines))

        elif low.startswith("/status"):
            run = load_json(STATE_DIR / "run_status.json", {})
            pending_count = len(load_json(STATE_DIR / "pending.json", []))
            last = run.get("finished_at") or "noch kein abgeschlossener Lauf"
            try:
                last = datetime.fromisoformat(last).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
            except (ValueError, TypeError):
                pass
            tg_send_plain(
                token,
                chat_id,
                "<b>Deal-Sniper Status</b>\n"
                f"• Letzter Lauf: <b>{html.escape(str(last))}</b>\n"
                f"• Aktive Regeln: <b>{static_rule_count + len(rules)}</b> "
                f"({static_rule_count} YAML + {len(rules)} Chat)\n"
                f"• Geprüfte Deals zuletzt: <b>{run.get('deals_checked', 0)}</b>\n"
                f"• Neue Treffer zuletzt: <b>{run.get('new_hits', 0)}</b>\n"
                f"• Wartende Nacht-Treffer: <b>{pending_count}</b>\n"
                f"• Gesamt gespeicherte Treffer: "
                f"<b>{len(load_json(STATE_DIR / 'hits.json', []))}</b>",
            )

        elif low.startswith("/help") or low.startswith("/start"):
            tg_send_plain(
                token,
                chat_id,
                "<b>Deal-Sniper Befehle:</b>\n"
                "<code>/watch RTX 4070 max 480</code> – Regel anlegen\n"
                "<code>/watch 2tb nvme max 95 min_temp 50</code> – mit Filtern\n"
                "<code>/unwatch RTX 4070</code> – Regel löschen\n"
                "<code>/rules</code> – alle Chat-Regeln anzeigen\n"
                "<code>/status</code> – letzten Lauf und Queue prüfen",
            )

    save_json(STATE_DIR / "cmd_offset.json", state)
    if changed:
        save_dynamic_rules(rules)
        print(f"Chat-Regeln aktualisiert: {len(rules)} aktiv")


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
                struct_price = None
                pm = getattr(entry, "pepper_merchant", None)
                if isinstance(pm, dict):
                    merchant = pm.get("name", "")
                    # Strukturierter Preis ist zuverlässiger als Regex aus dem Text
                    struct_price = parse_price(pm.get("price", "") or "")
                deals.append({
                    "guid": guid, "title": title, "link": link,
                    "price": struct_price or parse_price(summary) or parse_price(title),
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


# ── Preisverlauf-Sparkline ─────────────────────────────────────────────────
def sparkline_svg(entries: list[dict], days: int = 30) -> str:
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    points = [e for e in entries if e.get("ts", "") >= cutoff and isinstance(e.get("price"), (int, float))]
    if len(points) < 2:
        return "<div class='chart-empty'>Noch zu wenig Preisdaten</div>"
    vals = [float(e["price"]) for e in points[-60:]]
    lo, hi = min(vals), max(vals)
    width, height, pad = 220, 48, 4
    span = hi - lo or 1.0
    coords = []
    for i, value in enumerate(vals):
        x = pad + i * (width - pad * 2) / max(1, len(vals) - 1)
        y = pad + (hi - value) * (height - pad * 2) / span
        coords.append(f"{x:.1f},{y:.1f}")
    last = vals[-1]
    return (f"<div class='spark-wrap'><svg class='spark' viewBox='0 0 {width} {height}' "
            f"role='img' aria-label='Preisverlauf der letzten {days} Tage'>"
            f"<polyline points='{' '.join(coords)}'/></svg>"
            f"<span>{lo:.2f}–{hi:.2f} € · zuletzt {last:.2f} €</span></div>")


# ── Dashboard ──────────────────────────────────────────────────────────────
def render_dashboard(all_hits: list[dict], rules: list[dict], best30: dict) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    template = (BASE / "template.html").read_text(encoding="utf-8")
    prices = load_json(STATE_DIR / "prices.json", {})

    rule_rows = []
    for r in rules:
        cond = " + ".join(html.escape(k) for k in r.get("all", []))
        maxp = (f" ≤ {float(r['max_price']):.0f} €"
                if r.get("max_price") is not None else "")
        best = best30.get(r["name"])
        bestp = (f"<span class='best'>Bestpreis 30 T: "
                 f"{f'{best:.2f}'.replace('.', ',')} €</span>" if best else "")
        chart = sparkline_svg(prices.get(r["name"], []))
        rule_rows.append(f"<li><strong>{html.escape(r['name'])}</strong>"
                         f"<span class='cond'>{cond}{maxp}</span>{bestp}{chart}</li>")

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
    default_min_temp = int(cfg.get("min_temp", 0))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    # Erst neue Chat-Befehle verarbeiten (kann Regeln hinzufügen/entfernen)
    if token and chat_id:
        process_commands(token, chat_id, len(rules))

    # Statische (YAML) + dynamische (per Chat) Regeln zusammenführen
    dynamic = load_dynamic_rules()
    all_rules = rules + dynamic
    if not all_rules:
        print("Keine Regeln (weder YAML noch Chat) – nichts zu tun")
        return 0
    print(f"{len(rules)} YAML-Regeln + {len(dynamic)} Chat-Regeln aktiv")

    seen_list = load_json(STATE_DIR / "seen.json", [])
    seen = set(seen_list)
    all_hits = load_json(STATE_DIR / "hits.json", [])
    pending = load_json(STATE_DIR / "pending.json", [])

    deals = fetch_deals(cfg.get("feeds", ["hot"]))
    hits = find_hits(deals, all_rules, seen, default_min_temp)
    print(f"{len(hits)} neue Treffer")

    best30 = update_price_memory(hits)

    if hits:
        all_hits = (all_hits + hits)[-HITS_MAX:]
        save_json(STATE_DIR / "hits.json", all_hits)

    now = datetime.now(TZ)
    to_send = pending + hits
    telegram_failed = False
    if to_send:
        if in_quiet_hours(now):
            print(f"Ruhezeit ({now:%H:%M}) – {len(to_send)} Treffer in Pending-Queue")
            save_json(STATE_DIR / "pending.json", to_send)
        else:
            if send_telegram(to_send, best30, bundled=bool(pending)):
                save_json(STATE_DIR / "pending.json", [])
            else:
                save_json(STATE_DIR / "pending.json", to_send)
                telegram_failed = True

    seen_list = (seen_list + [d["guid"] for d in deals if d["guid"] not in seen])[-SEEN_MAX:]
    save_json(STATE_DIR / "seen.json", seen_list)

    render_dashboard(all_hits, all_rules, best30)
    save_json(STATE_DIR / "run_status.json", {
        "finished_at": datetime.now(TZ).isoformat(),
        "deals_checked": len(deals),
        "new_hits": len(hits),
        "rules_active": len(all_rules),
        "pending": len(load_json(STATE_DIR / "pending.json", [])),
        "telegram_ok": not telegram_failed,
    })
    if telegram_failed:
        print("FEHLER: Telegram-Versand fehlgeschlagen; Treffer bleiben in pending.json", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
