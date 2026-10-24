"""How far does price run in your favour after an RSI entry before it comes back to the entry price?

For every signal (RSI crosses below `lower` -> long, above `upper` -> short) the entry is the next
bar's open. From there the script walks forward within the session and measures:

  * MFE  - maximum favourable excursion (bp and $) reached BEFORE price trades back to the entry
           price, i.e. the best exit a trader could have had before a break-even stop would hit
  * how many minutes until break-even, and the share of trades that never come back that session

Conventions (1m bars, intrabar order unknown):
  * break-even = first bar AFTER the entry bar whose low (long) / high (short) touches the entry price
  * MFE counts the entry bar and every bar before the break-even bar; the break-even bar itself is
    excluded (its high might have come before or after the touch - conservative choice)
  * a trade that never returns to entry is measured until the session's last bar

The same measurement for every possible entry minute (baseline) shows whether RSI signals give
bigger moves than entering at random.

Examples:
    python excursion_rsi.py                       # SPY QQQ, 1m, RSI(14) 20/80
    python excursion_rsi.py SPY --lower 30 --upper 70
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_rsi as bt
from indicators import rsi

LONG, SHORT = 1, -1


def excursions(df: pd.DataFrame, signal_bars: np.ndarray, direction: int) -> pd.DataFrame:
    """Measure favourable excursion before break-even for entries at the open after each signal bar."""
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    days = df.index.normalize().to_numpy()
    # Index of the last bar of each bar's session.
    last_of_day = pd.Series(np.arange(len(df)), index=df.index).groupby(days).transform("max").to_numpy()

    rows = []
    for t in signal_bars:
        e_bar = t + 1
        if e_bar >= len(df) or days[e_bar] != days[t]:
            continue
        entry = opens[e_bar]
        end = last_of_day[e_bar]
        seg = slice(e_bar, end + 1)
        if direction == LONG:
            fav = (highs[seg] - entry) / entry
            touch = lows[seg] <= entry
        else:
            fav = (entry - lows[seg]) / entry
            touch = highs[seg] >= entry
        touch[0] = False  # the entry bar opens at the entry price by definition
        hit = np.flatnonzero(touch)
        stop = hit[0] if len(hit) else len(fav)  # bars [0, stop) are before break-even
        window = fav[:stop] if stop > 0 else fav[:1]
        peak = int(np.argmax(window))
        rows.append({
            "signal_time": df.index[t], "entry_time": df.index[e_bar], "entry": entry,
            "side": "long" if direction == LONG else "short",
            "mfe_bp": max(window.max(), 0.0) * 10_000,
            "mfe_usd": max(window.max(), 0.0) * entry,
            "minutes_to_peak": peak,
            "returned_to_entry": bool(len(hit)),
            "minutes_to_breakeven": int(hit[0]) if len(hit) else np.nan,
        })
    return pd.DataFrame(rows)


def summarize(ex: pd.DataFrame, ladder: list[float]) -> dict:
    if ex.empty:
        return {"n": 0}
    m = ex["mfe_bp"]
    row = {"n": len(ex),
           "p25": m.quantile(0.25), "median": m.median(), "p75": m.quantile(0.75), "p90": m.quantile(0.90),
           "median_usd": ex["mfe_usd"].median(),
           "back_to_entry": ex["returned_to_entry"].mean(),
           "median_min_to_be": ex["minutes_to_breakeven"].median()}
    for x in ladder:
        row[f">={x:g}bp"] = (m >= x).mean()
    return row


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="*", default=["SPY", "QQQ"])
    p.add_argument("--interval", default="1m")
    p.add_argument("--data-dir", type=Path, default=bt.DATA_DIR)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--lower", type=float, default=20)
    p.add_argument("--upper", type=float, default=80)
    p.add_argument("--session", default="09:30-16:00")
    p.add_argument("--entry-window", default="09:35-15:45")
    p.add_argument("--ladder", default="1,2,3,5,10,20", help="Schwellen in bp für 'Anteil erreicht'")
    args = p.parse_args(argv)

    ladder = [float(x) for x in args.ladder.split(",")]
    session, window = bt.parse_window(args.session), bt.parse_window(args.entry_window)
    out_dir = bt.OUTPUT_DIR / f"excursion_rsi{args.rsi_period}_{args.lower:g}-{args.upper:g}_{args.interval}_" \
                              f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    results, all_events = [], []
    for sym in args.symbols:
        path = args.data_dir / f"{sym}_{args.interval}.csv"
        if not path.exists():
            print(f"{sym}: keine Daten ({path.name})")
            continue
        df = bt.load_ohlc(path, session=session)
        r = rsi(df["close"], args.rsi_period)
        buy, sell = bt.crossings(r, args.lower, args.upper)
        clock = df.index.time
        ok = (clock >= window[0]) & (clock < window[1])
        all_bars = np.flatnonzero(ok)
        groups = {
            ("RSI-Kauf", LONG): np.flatnonzero(buy.to_numpy() & ok),
            ("RSI-Verkauf", SHORT): np.flatnonzero(sell.to_numpy() & ok),
            ("Zufall Long", LONG): all_bars,
            ("Zufall Short", SHORT): all_bars,
        }
        for (name, direction), bars in groups.items():
            ex = excursions(df, bars, direction)
            ex.insert(0, "symbol", sym)
            ex.insert(1, "group", name)
            all_events.append(ex)
            results.append({"symbol": sym, "group": name, **summarize(ex, ladder)})
        n_days = len(np.unique(df.index.normalize()))
        print(f"{sym}: {n_days} Handelstage, Ø-Kurs {df['close'].mean():.2f}")

    events = pd.concat(all_events, ignore_index=True)
    for name in ("RSI-Kauf", "RSI-Verkauf", "Zufall Long", "Zufall Short"):
        results.append({"symbol": "gesamt", "group": name, **summarize(events[events.group == name], ladder)})
    table = pd.DataFrame(results)
    table.to_csv(out_dir / "summary.csv", index=False)
    events[~events.group.str.startswith("Zufall")].to_csv(out_dir / "signals.csv", index=False)

    print(f"\nBewegung in Trade-Richtung bis zur Rückkehr zum Einstiegskurs  "
          f"[RSI({args.rsi_period}) {args.lower:g}/{args.upper:g}, {args.interval}, Einstieg {args.entry_window}]")
    head = (f"   {'Symbol':<7}{'Gruppe':<13}{'N':>6}{'25%':>7}{'Median':>8}{'75%':>7}{'90%':>7}{'Med $':>7}"
            f"{'zurück':>8}{'Min→BE':>8}  " + "".join(f"{'≥' + format(x, 'g') + 'bp':>7}" for x in ladder))
    print(head)
    for _, s in table.iterrows():
        if not s.n:
            continue
        print(f"   {s.symbol:<7}{s.group:<13}{int(s.n):>6}{s.p25:>7.1f}{s['median']:>8.1f}{s.p75:>7.1f}"
              f"{s.p90:>7.1f}{s.median_usd:>7.2f}{s.back_to_entry * 100:>7.0f}%{s.median_min_to_be:>8.0f}  "
              + "".join(f"{s[f'>={x:g}bp'] * 100:>6.0f}%" for x in ladder))
    print("   Werte in bp (1 bp = 0,01 %). 'zurück' = Anteil, der in derselben Sitzung wieder den Einstieg erreicht;"
          " 'Min→BE' = Median Minuten bis dahin.")
    print(f"\nErgebnisse: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
