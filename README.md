# PropHedge Terminal

Web-App, die eine Prop-Firm-Evaluation (MT5) mit einem Vantage-Live-Konto (MT5) hedged.
Ein Klick auf „Position öffnen“ öffnet per Market Order gleichzeitig:

- Prop-Konto: Position in zufälliger Richtung (50/50 Long/Short), Bracket SL/TP mit Abstand `d` (RR 1:1)
- Live-Konto: Gegenposition, Volumen = Prop-Lots × Hedge Ratio (über $-Wert pro Punkt normalisiert), SL/TP gespiegelt

## Installation (Windows, weil das MetaTrader5-Python-Paket nur unter Windows läuft)

1. Python 3.10+ installieren.
2. **Zwei getrennte MT5-Installationen** anlegen, z. B. `C:\MT5\PropFirm` und `C:\MT5\Vantage`
   (Installer jeweils in einen eigenen Ordner, oder Ordner kopieren und im Portable-Modus nutzen).
   Ein MT5-Terminal kann nur ein Konto gleichzeitig bedienen, deshalb läuft pro Konto ein eigener Worker-Prozess.
3. In beiden Terminals einmal manuell einloggen, unter *Extras → Optionen → Expert Advisors* „Algo Trading erlauben“ aktivieren
   und das Symbol in die Marktübersicht holen.
4. `pip install -r requirements.txt`
5. `config.example.json` nach `config.json` kopieren und ausfüllen:
   - `mode`: `"mt5"` für echte Konten, `"mock"` für die Simulation
   - `terminal_path`, `login`, `server` je Konto (Servername steht im MT5-Login-Dialog)
   - `symbol`: exakter Symbolname je Broker (z. B. `NAS100` vs. `NAS100.r`), das kann sich unterscheiden
   - Passwörter besser per Umgebungsvariable `PROP_PASSWORD` / `LIVE_PASSWORD` statt in der Datei
6. `python run.py` und `http://127.0.0.1:8000` öffnen.

## Regeln und Größenberechnung

- Consistency Limit = Profit Target × Consistency-Faktor. Die Verlustseite wird gegen den Daily Drawdown geprüft,
  die Gewinnseite gegen das Consistency Limit.
- Realisierter Tages-P&L kommt direkt aus der MT5-Deal-History des Prop-Kontos (auch Trades außerhalb der App zählen).
  Tageswechsel = `reset_hour` in Serverzeit des Prop-Brokers.
- Offenes Risiko/Chance = Abstand Entry→SL bzw. Entry→TP aller offenen Prop-Positionen.
  Eine Prop-Position ohne SL/TP blockiert neue Entries.
- Spielraum Verlustseite `Daily Drawdown + realisiert − offenes Risiko`, Gewinnseite
  `Consistency Limit − realisiert − offene Chance`, Max Loss `Max Loss + (Balance − Startkapital) − offenes Risiko`
  (statischer Drawdown).
- Lots = kleinster dieser Spielräume × `Anteil vom Restbudget` ÷ (d × Wert pro Punkt pro Lot + Kommission), begrenzt
  durch Margin und Volumen-Step des Symbols.
- Margin kommt vom Broker (`order_calc_margin`), enthält also den echten Hebel des Symbols. Ein Hedge darf auf jedem
  Konto höchstens `margin_max_use_pct` % der freien Margin belegen. Zusätzlich muss das Margin-Level im schlechtesten
  Fall (neue und offene Positionen am Stop, kurz vor dem Auslösen) über Margin-Call- und Stop-Out-Level des Brokers
  bleiben. Passt das nicht, sucht die App die größte passende Position; Prop und Live schrumpfen gemeinsam.
- Kommission (pro Lot, Hin + Rück) wird je Konto fest eingerechnet. Exakt gelernt wird sie aus jeder geschlossenen
  Position (alle Deals), pro Asset. Ist noch kein Wert bekannt, schätzt die App ihn beim Entry (Entry-Kommission × 2).
- Unterschiedliche Kontowährungen: Live-Beträge werden für Hedge-Lots, Ratios, Ausgleich und Netto über den
  aktuellen Kurs (z. B. EURUSD, auf einem der Broker gesucht) in die Prop-Währung umgerechnet.
- Der Kostenpuffer erhöht das angenommene SL-Risiko zusätzlich (Slippage beim Stop).
- Ein Entry wird abgelehnt, wenn SL oder TP eine der Regeln brechen würde.

## Ausführung

- Beide Market Orders werden parallel gesendet. Schlägt eine Seite fehl, wird die gefüllte Gegenseite sofort geschlossen.
- Nach dem Fill werden die Prop-SL/TP auf den tatsächlichen Fill-Preis gesetzt, damit der Abstand exakt `d` bleibt.
- Live-SL/TP werden gespiegelt (abschaltbar): Live-SL = Prop-TP, Live-TP = Prop-SL, exakt auf demselben Preis-Level.
  Mit S = (Spread Prop + Spread Live) / 2: bei Prop-SL löst Prop zuerst aus und Live gewinnt d − 2S; bei Prop-TP
  löst der Live-SL zuerst aus (Live verliert d + S), Prop wird zum Markt geschlossen und bekommt d − S.
  Ist das Spiegeln ungültig, gilt ±`d` um den eigenen Fill.
- SL/TP aus ATR (Regeln, abschaltbar): Abstand = ATR(Perioden) des Prop-Symbols im gewählten Zeitrahmen × Faktor,
  auf den Tick gerundet, Kerzen 60 s gecacht. Das Risiko pro Trade bleibt gleich, nur die Lots passen sich an. Der
  feste Abstand bleibt gespeichert und gilt wieder, sobald ATR aus ist. `max_lots` begrenzt die Prop-Lots (0 = aus),
  damit ein sehr kleiner Abstand keine riesige Position ergibt.
- Live-Lots (`hedge_mode`, Regeln → „Live-Lots“). Kosten über `cost_max_pct` % von d sperren den Entry.
  - **Kosten als Gebühr** (Standard): Live bewegt sich mit dem Kurs genau Ratio × so viel wie Prop (reines
    Volumenverhältnis). Spread und Kommissionen sind eine feste Gebühr pro Trade auf beiden Ausgängen, genau wie
    `f_E`/`f_L` im Simulator. Nichts wird nachgeholt, daher wird der Pass-Pfad nicht teurer als h × Ziel + Gebühren.
    Die App zeigt beim nächsten Hedge f_E und f_L zum Eintragen in den Simulator.
  - **Prop-SL exakt**: bei Prop-SL ist der Live-Gewinn netto genau Ratio × Prop-Verlust netto; alle Kosten landen
    auf dem Pass-Pfad. Der Ratio-Ausgleich holt Abweichungen im SL-Fall nach und verstärkt sie nach Prop-TPs.
  - **MSE**: kleinster mittlerer quadratischer Fehler der (kumulierten) Ratio über Prop-SL und Prop-TP.
- Ratio-Ausgleich (abschaltbar, nur „Prop-SL exakt“ und „MSE“): Ziel ist Live-P&L = −Ratio × Prop-P&L über alle
  geschlossenen Hedges der Challenge (seit „Neue Challenge“) bzw. des Tages, begrenzt auf
  ±`ratio_correction_max_pct` % der Live-Lots ohne Ausgleich.
- Schließt ein Leg (SL/TP), schließt die App die Gegenseite automatisch (abschaltbar), weil Spreads/Feeds der Broker abweichen.
- NY-Session-Schluss (Standard 17:00 New York, Sommer-/Winterzeit automatisch): `Entries sperren` Minuten vorher sind
  keine neuen Hedges möglich, `Alles schließen` Minuten vorher schließt die App **alle** Positionen auf beiden Konten
  (auch extern eröffnete), damit keine Overnight-Fees anfallen. Pending Orders bleiben stehen.
- „Alles schließen“ schließt **alle** Positionen und löscht alle Pending Orders auf beiden Konten (zweimal klicken).

## Kontowerte

Das Panel „Konten“ zeigt Equity, Balance, offenen und heute realisierten P&L beider Konten.
Im Mock-Modus lassen sich Balance und „Realisiert heute“ über „Werte setzen“ eintragen. Der Mock-Zustand
(Balance, Deals, offene Positionen, Preis) liegt in der DB und übersteht einen Neustart.
Im MT5-Modus kommen die Werte direkt vom Broker.

## Simulator (Reiter oben)

Geschlossenes Modell aus dem PropHedge-Paper (Gambler's Ruin, zwei Phasen, Pfad-PnL, Erwartungswert, Sortino),
Code in `app/simulator.py`, reproduziert die Zahlen des Papers exakt (h_E 0,25 / h_F 0,65, EV 269,67 $).

- **Optimieren:** Raster über h_E und h_F, Heatmap (Farbe nach Rang), Optimum markiert. Klick übernimmt einen Punkt.
- **Eigene Ratios:** beliebige h_E / h_F auswerten.
- **Funded neu bestimmen:** h_E ist fest (z. B. die tatsächlich gehedgte Ratio, „Aus Terminal“ übernimmt die
  laufende Ratio), optimiert wird nur h_F. Mit Status „Eval bestanden“ wird ab dem bestandenen Stand gerechnet,
  optional mit dem tatsächlichen Stand nach der Eval.
- **2-Step-Challenges:** Step 1 und Step 2 mit eigenem Ziel, Drawdown, Drawdown-Art und eigener Hedge Ratio (h₁, h₂),
  danach Funded. Die Heatmap zeigt h₁ × h₂ mit dem jeweils besten h_F. Option „Gebühr mit 1. Payout erstattet“.
  Raster bei 2-Step mindestens 0,025 (mit EOD trailing 0,05), weil drei Ratios optimiert werden.
- **Konfigurationen speichern und vergleichen:** Name eingeben, „Speichern“ (in der DB, Tabelle settings,
  Schlüssel `sim_configs`). Rechts die Liste mit EV, Sortino, ROI, Kapitalbedarf, Breakeven-Auszahlung und
  Payout-Wahrscheinlichkeit, bester Wert je Kennzahl markiert, sortierbar. Klick lädt die Konfiguration.
- Der Simulator rechnet in einem eigenen Prozess, damit lange Monte-Carlo-Läufe die Trading-Schleifen nicht
  blockieren. Kommen während einer Rechnung neue Anfragen, wird nur die neueste gerechnet.
- Bei negativem Erwartungswert wird statt Sortino die Abwärtsstreuung minimiert (Sortino kehrt sich dort um).
- Gegenüber dem Notebook ergänzt/korrigiert: Profit-Split φ, Auszahlungsquote q (verweigerte Payouts), Kapitalbedarf
  Π_min statt Nominal-ROI, Live-Gebühren in der Funded-Phase nur bei h_F > 0, optional exakte Gebühren je Pfad.

## Datenbank

SQLite (`prophedge.db`): `trades` (ein Hedge), `positions` (ein Leg pro Konto, mit `trading_day`, Ticket, Entry, SL/TP,
P&L, Schließgrund), `settings` (Regeln), `events` (Audit-Log: Entries, Ablehnungen, Panic, Orphan-Closes).

## Grenzen

- Trailing Drawdown wird nicht modelliert (nur statischer Max Loss).
- Der Chart zeigt den Feed des Prop-Brokers; Live-SL/TP liegen auf dem Vantage-Feed und können leicht abweichen.
- Währungsumrechnung nutzt den aktuellen Kurs, auch für ältere geschlossene Hedges.
- Erst im Mock-Modus und dann mit Demo-Konten bei beiden Brokern testen, bevor echtes Geld fließt.
