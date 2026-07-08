# Deal-Sniper 🎯 (serverlos via GitHub Actions)

Persönlicher Schnäppchen-Jäger ohne eigenen Server: prüft alle 2 Stunden
mydealz-Feeds gegen deine Watchlist-Regeln (Keywords + Maximalpreis) und pusht
neue Treffer sofort per **Telegram**. Dazu ein **Dashboard** auf GitHub Pages
mit allen bisherigen Treffern. Kosten: 0 €.

Warum mydealz statt Geizhals/Idealo-Scraping? Die blocken Datacenter-IPs (403),
mydealz bietet offizielle RSS-Feeds – und spült ohnehin nur echte Deals unter
Marktpreis hoch, inklusive Preis und Händler.

---

## Einrichtung (~5 Minuten – wie beim KI-Digest)

### 1. Repo anlegen & pushen
Neues GitHub-Repo (public für kostenloses Pages), dann:

```bash
cd deal-sniper
git init && git add -A && git commit -m "init"
git branch -M main
git remote add origin git@github.com:DEIN-USER/deal-sniper.git
git push -u origin main
```

### 2. Secrets eintragen
Settings → Secrets and variables → Actions:

| Secret | Wert |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Denselben Bot wie beim KI-Digest nutzen – einfach Token nochmal eintragen |
| `TELEGRAM_CHAT_ID` | Deine Chat-ID (auch identisch zum Digest) |

### 3. Pages aktivieren
Settings → Pages → Branch `main`, Ordner `/docs`.

### 4. Watchlist anpassen & ersten Lauf starten
`watchlist.yaml` direkt auf GitHub editieren (Beispiel-Regeln sind drin),
dann Actions → „Deal-Sniper" → **Run workflow**.

---

## Regeln schreiben

```yaml
- name: "2TB NVMe SSD"        # Anzeigename (Telegram + Dashboard)
  all: ["2tb", "nvme"]        # ALLE Begriffe müssen vorkommen
  none: ["extern", "gehäuse"] # KEINER davon darf vorkommen (optional)
  max_price: 95               # Preis-Obergrenze in € (optional)
```

- Matching ist case-insensitive über Titel + Beschreibung.
- Ohne `max_price` zählt nur der Text (nützlich für seltene Produkte).
- Mit `max_price` werden Deals ohne erkennbaren Preis übersprungen.
- `feeds:` steuert, welche mydealz-Bereiche geprüft werden – Gruppen-Slug aus
  der URL nehmen (`mydealz.de/gruppe/ssd` → `ssd`), `hot` = Startseite.

## Hinweise

- **Intervall:** alle 2 h (im Workflow anpassbar). GitHub-Cron ist nicht
  sekundengenau – Verzögerungen von ein paar Minuten sind normal.
- **Dedupe:** Jeder Deal wird nur einmal gemeldet (`state/seen.json` merkt
  sich ~3000 IDs und wird automatisch committet).
- Der Workflow committet bei jedem Lauf mit Änderungen → Repo bleibt aktiv,
  GitHub pausiert den Cron nicht.

---

## Neu in v2

- **Temperatur-Filter:** `min_temp: 20` (global oder pro Regel) verwirft Treffer
  mit weniger Community-Votes – killt Fehlalarme. Wird nur für tatsächliche
  Treffer nachgeladen; ist die Seite nicht lesbar, wird der Treffer trotzdem
  gemeldet (fail-open).
- **Preis-Gedächtnis:** Jeder Treffer-Preis wird pro Regel geloggt
  (`state/prices.json`, 60 Tage). Der Push zeigt „Bestpreis 30 T: X €" bzw.
  „🏆 neuer Bestpreis!", das Dashboard den Bestpreis pro Regel.
- **Ruhezeiten:** Zwischen 23 und 7 Uhr kein Push – Treffer sammeln sich in
  `state/pending.json` und kommen morgens gebündelt („Über Nacht aufgelaufen").
- **Fehler-Alarm:** Schlägt der Workflow fehl, bekommst du eine
  Telegram-Nachricht mit Link zum Log.
