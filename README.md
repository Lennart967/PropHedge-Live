# PropHedge Terminal

Web-App, die eine Prop-Firm-Evaluation (MT5) gegen ein Live-Konto (MT5) hedgt. „Position öffnen“ sendet
gleichzeitig eine Prop-Position in zufälliger Richtung mit SL/TP im Abstand `d` (RR 1:1) und eine Gegenposition
auf dem Live-Konto (Prop-Lots × Hedge Ratio, SL/TP auf denselben Preis-Leveln gespiegelt).
Dazu ein Simulator für die Wahl der Hedge Ratios.

## Installation (Windows, das MetaTrader5-Paket gibt es nur dort)

1. Python 3.10+ und zwei getrennte MT5-Installationen (eine pro Konto). In beiden einloggen, „Algo Trading“
   aktivieren und das Symbol in die Marktübersicht holen.
2. `pip install -r requirements.txt`
3. `config.example.json` nach `config.json` kopieren und ausfüllen (`mode`: `mt5` oder `mock`, Terminal-Pfad,
   Login, Server, Symbol je Konto). Passwörter besser per `PROP_PASSWORD` / `LIVE_PASSWORD`.
4. `python run.py` und http://127.0.0.1:8000 öffnen.

`config.json` und die Datenbanken (`*.db`) sind per `.gitignore` ausgeschlossen.

## Funktionen

- **Positionsgröße:** kleinster Spielraum aus Daily Drawdown, Consistency Rule und Max Loss (statisch oder EOD
  trailing), mit Sicherheitspuffer vor den echten Limits, Risiko-Deckel pro Trade, Max. Lots und Margin-Prüfung
  (inkl. Stop-Out im schlechtesten Fall). Kurz vor dem Profit-Ziel nur so groß, dass der TP das Ziel erreicht.
- **SL/TP:** fester Abstand oder ATR × Faktor; Vergleich beider Varianten und ATR-Linien im Chart.
- **Live-Lots:** „Kosten als Gebühr“ (Standard, gleiche Kursbewegung × Ratio, wie im Simulator), „Prop-SL exakt“
  oder „MSE“, die letzten beiden mit optionalem Ratio-Ausgleich.
- **Kosten:** Kommissionen werden aus den Deals gelernt; Kosten über `cost_max_pct` % von d sperren den Entry.
- **Ausführung:** Schlägt eine Seite fehl, wird die andere sofort geschlossen. Schließt ein Leg, schließt die App die
  Gegenseite. Nur ein Hedge gleichzeitig. Optional automatisches Schließen vor NY-Close und vor dem Wochenende.
- **Neue Challenge:** setzt Ratio-Zählung, Trailing-Startpunkt und gelernte Kommissionen zurück.

## Simulator

Modell aus dem PropHedge-Paper (Gambler's Ruin, Pfad-PnL, EV, Sortino; `app/simulator.py`), mit EOD-Trailing per
Monte-Carlo, 2-Step-Challenges, Profit-Split, Payout-Quote q und Kapitalbedarf. Modi: Optimieren (Heatmap),
eigene Ratios, Funded neu bestimmen. Zeigt je Phase den Ratio-Bereich, in dem alle Ausgänge netto ≥ 0 sind.
Konfigurationen lassen sich speichern und vergleichen.

Kosten: `Prop Eval/Funded` = selbst bezahlte Gebühren (meist 0), `Live-Konto (bei h = 1)` = Prop-Kommission +
Live-Spread/-Kommission pro Trade bei Ratio 1; die App zeigt den Wert unter „Neuer Hedge“.

## Grenzen

- Die Prop-Kommission verschiebt im Simulator nicht die Pass-Wahrscheinlichkeit; EVs sind daher leicht zu hoch.
- Swaps und Slippage bei News sind nicht modelliert.
- Erst im Mock-Modus, dann mit Demo-Konten testen.
