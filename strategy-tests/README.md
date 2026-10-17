# strategy-tests

Backtests von Handelsstrategien auf historischen Kursdaten von tastytrade (DXLink).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Zugangsdaten kommen ausschließlich aus Umgebungsvariablen (dieselben wie beim tastytrade-MCP-Server):
`TASTYTRADE_CLIENT_ID`, `TASTYTRADE_CLIENT_SECRET`, `TASTYTRADE_REFRESH_TOKEN`.
Optional `TASTYTRADE_ENV=sandbox` (der Sandbox liefert allerdings keine Marktdaten).

## Ablauf

1. **Kerzen laden** – speichert `data/<SYMBOL>_<periode>.csv`:
   ```powershell
   .\.venv\Scripts\python.exe fetch_candles.py AAPL MSFT SPY QQQ --period 1d --years 10
   ```
2. **RSI-Backtest** – schreibt `output/<lauf>/` mit `event_study.csv`, `summary.csv`, `trades.csv`, `charts/`:
   ```powershell
   .\.venv\Scripts\python.exe backtest_rsi.py                      # alle Symbole in data/, RSI(14) 20/80
   .\.venv\Scripts\python.exe backtest_rsi.py SPY --lower 30 --upper 70 --exit mid
   .\.venv\Scripts\python.exe backtest_rsi.py --rsi-period 2 --lower 10 --upper 90 --exit bars --hold 5
   .\.venv\Scripts\python.exe backtest_rsi.py --end 2019-12-31     # In-Sample …
   .\.venv\Scripts\python.exe backtest_rsi.py --start 2020-01-01   # … und Out-of-Sample getrennt prüfen
   ```

## Intraday (z. B. 1-Minuten-Kerzen)

```powershell
.\.venv\Scripts\python.exe fetch_candles.py SPY QQQ AAPL --period 1m --years 1
.\.venv\Scripts\python.exe backtest_rsi.py --interval 1m --exit mid --cost-bps 1
```

- Abgerufen wird nur die reguläre Handelszeit (`tho=true`), mit `--all-hours` auch Pre- und After-Market.
  Die Zeitstempel sind US-Börsenzeit (New York). Wie viel 1m-Historie DXLink liefert, zeigt der Abruf an.
- `--session 09:30-16:00`: Nur Kerzen in diesem Fenster werden verwendet.
- `--entry-window 09:35-15:45`: Einstiege sind nur in diesem Fenster erlaubt. Die ersten Minuten sind gesperrt,
  weil die Kurslücke über Nacht den RSI zur Eröffnung verzerrt.
- Jede Position wird spätestens zum Schlusskurs der letzten Kerze des Tages glattgestellt, über Nacht wird nichts gehalten.
- Die Event-Study-Horizonte sind Minuten (`1,5,15,30,60`) und enden nie im nächsten Handelstag.
- **Break-even-Kosten**: Das sind die Kosten je Seite in bp, bei denen der durchschnittliche Trade bei null landet.
  Im Minutenbereich ist das die entscheidende Kennzahl, weil Spread und Slippage den Großteil der Bewegung auffressen.
  Am besten mit mehreren `--cost-bps`-Werten testen.

### Minuten-Archiv

DXLink liefert je Abfrage nur die jüngsten Kerzen. Die Historie reicht je nach Periode so weit zurück:
1m ≈ 8.000 Kerzen (~20 Handelstage), 5m/15m ≈ 3 Monate, 1h ≈ 9 Monate, 1d über 10 Jahre.
Eine längere Minuten-Historie entsteht deshalb nur durch regelmäßiges Anhängen:

- `fetch_candles.py ... --append` führt neue Kerzen mit der vorhandenen CSV zusammen. Bei gleichen Zeitstempeln
  gewinnt der neue Wert. Gibt es keine Überlappung mehr, warnt der Abruf vor einer Lücke.
- `collect_intraday.ps1` hängt die 1m-Kerzen von SPY und QQQ an, legt vorher eine Sicherung in `data/backup/` an
  und protokolliert nach `logs/collect_JJJJ-MM.log`.
- Die Windows-Aufgabe **„strategy-tests 1m-Archiv“** startet das Skript Mo–Fr um 23:00 (nach US-Handelsschluss).
  War der PC zu dem Zeitpunkt aus, holt Windows den Lauf nach. Weil DXLink ~20 Handelstage vorhält,
  entsteht nur dann eine Lücke, wenn der PC rund drei Wochen lang nicht lief.
  Die Aufgabe läuft nur, solange du angemeldet bist, denn sie braucht die Benutzer-Umgebungsvariablen.

```powershell
Start-ScheduledTask -TaskName "strategy-tests 1m-Archiv"          # sofort ausführen
Get-ScheduledTaskInfo -TaskName "strategy-tests 1m-Archiv"        # letzter/nächster Lauf
Unregister-ScheduledTask -TaskName "strategy-tests 1m-Archiv"     # entfernen
```

Weitere Symbole: in `collect_intraday.ps1` den Standardwert von `$Symbols` erweitern.

## Methodik

- **Signale** sind Kreuzungen: Kauf, wenn der RSI unter `--lower` schließt; Verkauf, wenn er über `--upper` schließt.
  Das Signal entsteht zum Schlusskurs, ausgeführt wird zur **Eröffnung der nächsten Kerze** (kein Look-ahead).
- **Event Study:** Rendite von der Eröffnung nach dem Signal bis zum Schlusskurs nach N Kerzen,
  verglichen mit derselben Rendite über *alle* Tage. Ein Kaufsignal taugt etwas, wenn die Überrendite > 0 ist,
  ein Verkaufssignal, wenn sie < 0 ist; |t| > 2 ist ein grober Hinweis auf statistische Auffälligkeit.
- **Strategie:** Long bei Kaufsignal, Short bei Verkaufssignal (`--side`). Ausstieg per `--exit`:
  `opposite` (Long raus bei RSI > upper, Short raus bei RSI < lower), `mid` (RSI kreuzt 50) oder
  `bars` (nach `--hold` Kerzen; `days` ist ein Alias).
  Kosten und Slippage je Seite über `--cost-bps` (Standard 5). Ein Short, dessen Kurs sich mehr als
  verdoppelt, gilt als Totalverlust.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```
