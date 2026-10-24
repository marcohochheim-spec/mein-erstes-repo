# OrderFlowAutoBot – ATAS Orderflow-Strategie

Vollautomatisierte `ChartStrategy` für die [ATAS](https://atas.net)-Plattform. Kombiniert vier
unabhängig ein-/ausschaltbare Orderflow-Signalmodule und lässt praktisch jeden Schwellenwert,
jede Risiko-Einstellung und jedes Zeitfenster über die ATAS-Einstellungsoberfläche verändern –
gedacht als Werkstatt zum Entwickeln/Testen eigener Einstiegssignale, nicht als fertig
kalibrierte Strategie.

## ⚠️ Wichtiger Hinweis

- **Erfolgreich gegen die echten, auf diesem Rechner installierten ATAS-DLLs kompiliert** (nicht nur
  Doku-Abgleich): `C:\Program Files (x86)\ATAS Platform\ATAS.Strategies.dll` /
  `ATAS.Indicators.dll` / `ATAS.Indicators.Technical.dll` / `ATAS.DataFeedsCore.dll`. Die daraus
  entstandene `OrderFlowAutoBot.dll` (auch unter `bin\OrderFlowAutoBot.dll` in diesem Ordner) läuft
  bereits in deinem aktiven ATAS-Datenordner. Ein echter Build ist eine stärkere Garantie als reine
  API-Verifikation per Reflection, deckt aber nicht zwingend jedes Laufzeitverhalten ab (z.B. ob
  Orders vom Broker/Connector so wie erwartet angenommen werden) – deshalb weiterhin zwingend im
  Strategy Tester und Sim-Konto prüfen, siehe unten.
- Automatisierter Handel kann zu Verlusten führen, die das eingesetzte Kapital übersteigen.
  **Erst im ATAS Strategy Tester auf historischen Daten, dann auf einem Sim-/Demo-Konto testen**,
  bevor die Strategie mit echtem Geld läuft.
- Dies ist keine Anlageberatung und keine Gewinngarantie.

## Dateien

| Datei | Inhalt |
|---|---|
| `OrderFlowSignalStrategy.cs` | Haupt-Klasse, `OnCalculate`-Loop, Signal-Kombination |
| `OrderFlowSignalStrategy.Parameters.cs` | Alle einstellbaren Parameter (in ATAS unter den Strategie-Settings sichtbar) |
| `OrderFlowSignalStrategy.Signals.cs` | Die vier Orderflow-Signalmodule |
| `OrderFlowSignalStrategy.Orders.cs` | Order-Ausführung, Bracket-Orders (SL/TP), Breakeven, Trailing, Tageslimits |
| `OrderFlowAutoBot.csproj` | Visual-Studio-Projekt zum Kompilieren als DLL |
| `Exporter/OrderFlowExporter.cs` | Indikator `OrderFlow Exporter (CSV)`: schreibt Bar- und Footprint-Daten für die Analyse (siehe unten) |
| `build.ps1` | Baut Bot oder Exporter ohne Visual Studio (alte `csc.exe`, Code muss C#-5-kompatibel sein) |
| `bin/` | Fertig gebaute `OrderFlowAutoBot.dll` und `OrderFlowExporter.dll` |

## Setup

**Die DLL ist bereits fertig gebaut und installiert** – `bin\OrderFlowAutoBot.dll` liegt schon in
deinem aktiven ATAS-Datenordner unter `C:\Users\MH\AppData\Roaming\ATAS\Indicators\OrderFlowAutoBot.dll`
(gebaut direkt gegen die auf diesem Rechner installierten ATAS-DLLs). Um sie auszuprobieren:

1. ATAS starten (bzw. falls schon offen: Chart neu laden oder die Bibliothek über das
   Refresh-Symbol im Indikatoren-/Strategien-Dialog neu einlesen lassen).
2. Chart öffnen → Rechtsklick → **"Chart-Strategien hinzufügen"** (oder das entsprechende Symbol
   in der Toolbar) → in der Liste nach `OrderFlowSignalStrategy` suchen und hinzufügen.
3. Im Einstellungsdialog der Strategie siehst du alle Parameter aus dieser README (General,
   Session & Limits, die vier Signalmodule, Risk Management). Zum ersten Ausprobieren: alle
   Signalmodule bis auf eines deaktivieren und "Min. übereinstimmende Signale" auf 1 stellen,
   dann beobachten, wann die blaue Signal-Linie im unteren Panel ausschlägt.
4. **Automatisierung ist standardmäßig NICHT aktiv**, solange du sie nicht explizit startest –
   die Strategie zeigt zunächst nur die Signale an. Erst wenn du im Strategie-Panel auf
   "Automatisierung starten" klickst, löst sie echte Orders aus.
5. **Vor jeder echten Auto-Ausführung**: den ATAS **Strategy Tester** nutzen, um die Parameter auf
   historischen Daten durchzuspielen, danach auf einem Sim-/Demo-Konto testen.

### Falls du den Code änderst und neu bauen willst

Solltest du die `.cs`-Dateien anpassen wollen: Da auf diesem Rechner weder Visual Studio noch ein
.NET-SDK installiert sind, wurde die DLL mit einem Reflection-verifizierten Referenz-Build erzeugt
(alte `csc.exe` + Referenzen direkt auf die ATAS-DLLs und die .NET-Runtime-Assemblies). Am
einfachsten für künftige Änderungen: `OrderFlowAutoBot.csproj` in Visual Studio (Community reicht,
".NET Desktop Development"-Workload) öffnen, `Strg+B`, dann die neue DLL aus
`bin\Debug\net8.0-windows` nach `C:\Users\MH\AppData\Roaming\ATAS\Indicators` kopieren
(vorhandene Datei überschreiben) und ATAS/das Chart neu laden.

## Signalmodule (alle Schwellen einstellbar)

- **Delta-Imbalance** – Ask/Bid-Volumen-Verhältnis pro Bar über X, mit Mindestvolumen-Filter und
  Anzahl an Bestätigungs-Bars in Folge.
- **Cumulative-Delta-Divergenz** – Vergleicht zwei Zeitfenster: bewegt sich der Preis kaum, während
  das kumulierte Delta stark steigt/fällt, wird das als verdeckte Kauf-/Verkaufsaktivität gewertet.
- **Absorption** – Hohes Volumen bei geringer Preisbewegung, Richtung wird über das Vorzeichen des
  Bar-Delta bestimmt.
- **POC-Nähe** – Ermittelt den Preis mit dem höchsten gehandelten Volumen (Point of Control) über
  ein Lookback-Fenster (vereinfachtes Volumenprofil aus den Footprint-Daten der Bars) und wertet
  Preisnähe dazu in Kombination mit der Delta-Richtung als Unterstützung/Widerstand.

`Min. übereinstimmende Signale` (General) legt fest, wie viele der aktivierten Module in dieselbe
Richtung zeigen müssen, damit tatsächlich eine Order ausgelöst wird – so lassen sich Module einzeln
testen (alle anderen deaktivieren, Wert auf 1) oder als Konfluenz-System kombinieren (Wert auf 2-4).

Das Standard-Ausgabe-Diagramm des Indikators zeichnet den kombinierten Signalwert (+1/-1/0) je Bar,
damit du visuell prüfen kannst, wann Signale auslösen würden – auch ohne die Automatisierung zu starten.

## Risikomanagement (alles einstellbar)

- Stop-Loss: feste Ticks **oder** ATR-Multiplikator
- Take-Profit: feste Ticks, Chance/Risiko-Verhältnis, **oder** ATR-Multiplikator
- Breakeven-Verschiebung nach X Ticks Gewinn (+ Offset)
- Optionaler Trailing-Stop
- Handelsrichtung (Long/Short/Beide), Handelszeitfenster, Cooldown nach jedem Trade,
  maximale Trades/Tag, maximaler Tagesverlust (basiert auf `TradingManager.Position.RealizedPnL`)

## API-Verifikation

Alle verwendeten ATAS-APIs wurden zunächst per .NET-Reflection und danach durch einen **echten,
erfolgreichen Compiler-Build** gegen die auf diesem Rechner installierten DLLs (`ATAS.Strategies.dll`,
`ATAS.Indicators.dll`, `ATAS.Indicators.Technical.dll`, `ATAS.DataFeedsCore.dll`) geprüft – nicht nur
gegen Doku-Beschreibungen. Dabei kamen fünf korrigierte Annahmen ans Licht:

- `ChartStrategy` hat **keinen** parameterlosen Konstruktor, sondern `ChartStrategy(bool useCandles)`
  → Aufruf über `: base(true)`.
- `OnCurrentPositionChanged()` hat **keine Parameter** – die vorherige Position wird selbst über ein
  Feld (`_previousPosition`) mitgeführt.
- `ModifyOrder(Order order, Order newOrder)` erwartet **zwei** Order-Objekte (Original + per
  `.Clone()` erzeugte, geänderte Kopie), nicht ein mutiertes Original.
- Die aus alten Community-Beispielen bekannte Klasse `ValueDataSeries` **existiert in dieser
  ATAS-Version nicht mehr**. Für die Signal-Anzeige wird stattdessen der direkte Indexer der
  Basisklasse verwendet (`this[bar] = wert;`).
- `Indicator.TickSize` ist als veraltet markiert (`[Obsolete]`) → stattdessen wird
  `InstrumentInfo.TickSize` verwendet.

Bestätigt (Property-Typen exakt, inkl. `Decimal` vs. `Int32`): `CurrentPosition`, `AveragePrice`,
`CanProcess(int bar)`, `GetCandle(int)`, `IndicatorCandle.Delta/Ask/Bid/Volume/Time`,
`GetAllPriceLevels()`, `PriceVolumeInfo.Price/Volume`, `Order.QuantityToFill`/`TriggerPrice`/`Price`/
`OCOGroup` (alle `Decimal` bzw. `string`), `OrderDirections{Buy,Sell}`,
`OrderTypes{Market,Limit,Stop,StopLimit}`, `Position.RealizedPnL`, `ATR.Period`, `RecalculateValues()`,
`Add(Indicator)`, `Calculate(int, decimal)`.

## Orderflow-Export für die Strategie-Analyse

ATAS hat keine externe Daten-API – Orderflow-Daten (Delta, Bid/Ask je Preis, POC …) gibt es nur
innerhalb von ATAS über die Indikator-API. Deshalb gibt es zusätzlich den Indikator
**`OrderFlow Exporter (CSV)`** (`Exporter/OrderFlowExporter.cs` → `bin/OrderFlowExporter.dll`). Er handelt
nicht, sondern schreibt jeden abgeschlossenen Bar des Charts in zwei CSV-Dateien:

| Datei | Inhalt |
|---|---|
| `<Instrument>_<Kennung>_<Zeit>_bars.csv` | OHLC, Volumen, Ask/Bid, Delta, Max/Min-Delta, Trades, POC, Value Area, Tickgröße |
| `<Instrument>_<Kennung>_<Zeit>_levels.csv` | Footprint: Bid/Ask/Volumen je Bar und Preis |

Zeiten sind UTC (wie in ATAS intern). Standard-Ordner: `Dokumente\ATAS_OrderFlowExport`.

1. Chart öffnen (z.B. ES 1 Minute), über die Chart-Einstellungen **genügend Tage laden** (für eine
   aussagekräftige Analyse mind. 20–60 Handelstage).
2. Indikator `OrderFlow Exporter (CSV)` hinzufügen, unter „Datei-Kennung" den Zeitrahmen eintragen
   (z.B. `1m`). Beim Hinzufügen wird die gesamte geladene Historie sofort exportiert; danach jeder neue Bar.
3. Analyse starten:

   ```powershell
   cd Y:\Claude\strategy-tests
   .venv\Scripts\python.exe orderflow_study.py "$env:USERPROFILE\Documents\ATAS_OrderFlowExport" --tz Europe/Berlin --tick-value 12.5
   ```

   Ergebnisse (inkl. `summary.md` mit Parametervorschlägen in den Einheiten dieses Bots) landen in
   `strategy-tests\output\orderflow_*`.

Neu bauen (ohne Visual Studio): `.\build.ps1 -Project Exporter -Install` bzw. `.\build.ps1 -Project Bot`.
