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
| `TELEGRAM_BOT_TOKEN` | Token eines eigenen Deal-Sniper-Bots (getrennter Bot empfohlen) |
| `TELEGRAM_CHAT_ID` | Deine Chat-ID; dem Bot vorher mindestens eine Nachricht senden |

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

---

## Neu in v3

- **Strukturierter Preis:** Preis kommt jetzt bevorzugt aus dem RSS-Feld
  `pepper_merchant` (zuverlässiger als Regex aus dem Text).
- **Regeln per Telegram:** Schreib dem Bot direkt:
  - `/watch RTX 4070 max 480` – neue Regel
  - `/watch 2tb nvme max 95 min_temp 50` – mit Preis- und Temperatur-Filter
  - `/unwatch RTX 4070` – Regel löschen
  - `/rules` – alle Chat-Regeln anzeigen
  - `/status` – letzter Lauf, Regeln, geprüfte Deals und Pending-Queue
  - `/help` – Hilfe
  Chat-Regeln liegen in `state/dynamic_rules.json` und laufen zusätzlich zu
  den YAML-Regeln. Nur Nachrichten aus deinem Chat werden akzeptiert.

**Hinweis:** Nutzt `getUpdates` wie der KI-Digest – bei gemeinsamem Bot
kollidieren beide. Am besten getrennte Bots verwenden.


## Update v4: Status, Preis-Charts und zuverlässigere Actions

- **`/status`-Befehl:** Zeigt den letzten abgeschlossenen Lauf, Anzahl aktiver
  YAML-/Chat-Regeln, geprüfte Deals, neue Treffer und wartende Nacht-Treffer.
- **30-Tage-Preisverlauf:** Jede Regel erhält im GitHub-Pages-Dashboard eine
  kompakte SVG-Kurve aus `state/prices.json` – ohne Chart-Bibliothek.
- **Telegram-Preflight:** Der Workflow prüft vor jedem Lauf Bot-Token und
  Chat-ID über Telegram. Fehlende oder ungültige Secrets machen den Lauf rot,
  statt einen grünen Lauf ohne Nachricht zu erzeugen.
- **Sicherer State-Commit:** Änderungen werden zuerst committet und danach per
  Rebase synchronisiert. So gehen parallele State-Änderungen nicht still verloren.

### Telegram-Test

1. Dem Deal-Sniper-Bot in Telegram `/start` senden.
2. GitHub: **Actions → Deal-Sniper → Run workflow**.
3. Im Lauf muss **Telegram-Konfiguration prüfen** grün werden.
4. Danach `/status` an den Bot senden; spätestens beim nächsten Sniper-Lauf
   beantwortet er den Befehl.
