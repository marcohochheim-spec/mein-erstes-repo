"""RSI extreme-value backtest: is RSI < lower a buy signal, RSI > upper a sell signal?

Two views on the same signals:
  1. Event study  - forward returns after each signal vs. the unconditional return of all days.
  2. Strategy     - trade the signals (long and/or short) with costs, compare with buy & hold.

Signals are crossings: RSI closes below `lower` after being at/above it (buy), or above `upper`
after being at/below it (sell). Signals form at the close of bar t and are filled at the
open of bar t+1, so no future information is used.

Examples:
    python backtest_rsi.py AAPL MSFT SPY
    python backtest_rsi.py --lower 30 --upper 70 --exit mid
    python backtest_rsi.py SPY --rsi-period 2 --lower 10 --upper 90 --exit bars --hold 5 --start 2015-01-01
    python backtest_rsi.py SPY QQQ --interval 1m --exit mid --cost-bps 1     # intraday

Intraday (--interval 1m, 5m, 1h ...): only bars within --session are used, entries only within
--entry-window, and every position is closed at the close of the session's last bar (no overnight risk).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, asdict, fields
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from fetch_candles import is_intraday
from indicators import rsi

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

LONG, SHORT = 1, -1


# ---------------------------------------------------------------------------
# Data and signals
# ---------------------------------------------------------------------------

def load_ohlc(path: Path, start: str | None = None, end: str | None = None,
              session: tuple[time, time] | None = None) -> pd.DataFrame:
    """Load a candle CSV; `session` keeps only bars starting within [from, to) clock time."""
    df = pd.read_csv(path, parse_dates=["time"], index_col="time").sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
    if session:
        clock = df.index.time
        df = df[(clock >= session[0]) & (clock < session[1])]
    return df.dropna(subset=["open", "close"])


def parse_window(text: str) -> tuple[time, time]:
    """'09:30-16:00' -> (time(9, 30), time(16, 0))."""
    a, b = text.split("-")
    return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())


def crossings(r: pd.Series, lower: float, upper: float) -> tuple[pd.Series, pd.Series]:
    """Buy = RSI crosses below `lower`; sell = RSI crosses above `upper`."""
    prev = r.shift(1)
    buy = (r < lower) & (prev >= lower)
    sell = (r > upper) & (prev <= upper)
    return buy.fillna(False), sell.fillna(False)


# ---------------------------------------------------------------------------
# Event study
# ---------------------------------------------------------------------------

def forward_returns(df: pd.DataFrame, horizon: int, sessions: np.ndarray | None = None) -> pd.Series:
    """Return from the next bar's open (entry) to the close `horizon` bars after the signal bar.

    With `sessions`, windows that would reach into the next session are dropped (NaN).
    """
    fwd = df["close"].shift(-horizon) / df["open"].shift(-1) - 1
    if sessions is not None:
        s = pd.Series(sessions, index=df.index)
        fwd = fwd.where(s.shift(-horizon) == s)
    return fwd


def event_stats(signal_returns: np.ndarray, base_returns: np.ndarray) -> dict:
    s, b = signal_returns[~np.isnan(signal_returns)], base_returns[~np.isnan(base_returns)]
    n = len(s)
    row = {"n_signals": n, "mean": np.nan, "median": np.nan, "hit_rate": np.nan,
           "base_mean": b.mean() if len(b) else np.nan,
           "base_hit_rate": (b > 0).mean() if len(b) else np.nan,
           "excess": np.nan, "t_stat": np.nan}
    if n:
        row.update(mean=s.mean(), median=np.median(s), hit_rate=(s > 0).mean())
        row["excess"] = row["mean"] - row["base_mean"]
        if n > 1 and len(b) > 1:
            se = np.sqrt(s.var(ddof=1) / n + b.var(ddof=1) / len(b))
            row["t_stat"] = row["excess"] / se if se > 0 else np.nan
    return row


# ---------------------------------------------------------------------------
# Strategy simulation
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    side: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    bars_held: int
    gross_return: float
    net_return: float
    still_open: bool


def session_ends(sessions: np.ndarray | None, n: int) -> np.ndarray:
    """True on the last bar of each session (intraday); all False without sessions (daily)."""
    if sessions is None:
        return np.zeros(n, bool)
    return np.append(sessions[1:] != sessions[:-1], True)


def simulate(df: pd.DataFrame, r: pd.Series, entry: pd.Series, direction: int, *,
             exit_mode: str, lower: float, upper: float, hold: int, max_hold: int | None,
             cost: float, symbol: str = "", sessions: np.ndarray | None = None,
             entry_ok: np.ndarray | None = None) -> tuple[list[Trade], pd.Series, pd.Series]:
    """Simulate one side. Returns (trades, per-bar strategy returns, per-bar position).

    With `sessions` (one label per bar, e.g. the trading date) the simulation is intraday:
    no signal on a session's last bar is carried into the next session, and any open position
    is closed at the close of the session's last bar.
    """
    opens, closes = df["open"].to_numpy(float), df["close"].to_numpy(float)
    rv, sig = r.to_numpy(float), entry.to_numpy(bool)
    idx = df.index
    n = len(df)
    last_bar = session_ends(sessions, n)
    can_enter = np.ones(n, bool) if entry_ok is None else np.asarray(entry_ok, bool)
    pos = np.zeros(n)
    bar_ret = np.zeros(n)
    costs = np.zeros(n)
    trades: list[Trade] = []

    def should_exit(t: int, held: int) -> bool:
        if max_hold and held >= max_hold:
            return True
        if exit_mode in ("days", "bars"):
            return held >= hold
        level = 50.0 if exit_mode == "mid" else (upper if direction == LONG else lower)
        return rv[t] > level if direction == LONG else rv[t] < level

    def record(signal_bar: int, entry_bar: int, end: int, exit_px: float, exit_bar: int,
               still_open: bool = False) -> None:
        """Position is held on bars [entry_bar, end); exit_bar dates the fill and carries its cost."""
        pos[entry_bar:end] = direction
        # Mark the position at each open (last mark: exit fill). Valuing against the entry price
        # keeps a short as one static position instead of a daily-rebalanced one.
        marks = np.append(opens[entry_bar:end], exit_px)
        value = np.maximum(1 + direction * (marks / marks[0] - 1), 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            seg = np.where(value[:-1] > 0, value[1:] / value[:-1] - 1, 0.0)
        bar_ret[entry_bar:end] = seg
        costs[entry_bar] += cost
        costs[exit_bar] += cost
        # A short whose price more than doubles is wiped out, even if it would have recovered.
        gross = -1.0 if value.min() <= 0 else direction * (exit_px / opens[entry_bar] - 1)
        trades.append(Trade(symbol, "long" if direction == LONG else "short",
                            idx[signal_bar], idx[entry_bar], opens[entry_bar],
                            idx[exit_bar], exit_px, end - entry_bar, gross, gross - 2 * cost,
                            still_open))

    in_pos, signal_bar, entry_bar = False, -1, -1
    for t in range(n):  # decide at close of t, fill at open of t+1
        if not in_pos:
            if t < n - 1 and sig[t] and can_enter[t] and not last_bar[t]:
                in_pos, signal_bar, entry_bar = True, t, t + 1
        elif last_bar[t]:  # flat into the session close
            record(signal_bar, entry_bar, t + 1, closes[t], t)
            in_pos = False
        elif t < n - 1 and should_exit(t, t - entry_bar + 1):
            record(signal_bar, entry_bar, t + 1, opens[t + 1], t + 1)
            in_pos = False
    if in_pos:
        record(signal_bar, entry_bar, n, closes[-1], n - 1, still_open=True)

    strat = pd.Series(bar_ret - costs, index=idx)
    return trades, strat, pd.Series(pos, index=idx)


def perf(returns: pd.Series) -> dict:
    equity = (1 + returns).cumprod()
    years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] - 1
    return {"total_return": total,
            "cagr": (1 + total) ** (1 / years) - 1 if total > -1 else -1.0,
            "max_drawdown": (equity / equity.cummax() - 1).min()}


def trade_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": np.nan, "avg_trade": np.nan, "median_trade": np.nan,
                "profit_factor": np.nan, "avg_bars": np.nan, "breakeven_cost_bps": np.nan}
    rets = np.array([t.net_return for t in trades])
    wins, losses = rets[rets > 0].sum(), -rets[rets < 0].sum()
    return {"trades": len(rets), "win_rate": (rets > 0).mean(), "avg_trade": rets.mean(),
            "median_trade": np.median(rets),
            "profit_factor": wins / losses if losses > 0 else np.inf,
            "avg_bars": np.mean([t.bars_held for t in trades]),
            # Cost per side at which the average trade would earn exactly nothing.
            "breakeven_cost_bps": np.mean([t.gross_return for t in trades]) / 2 * 10_000}


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def plot_symbol(path: Path, symbol: str, df: pd.DataFrame, r: pd.Series, trades: list[Trade],
                equity: dict[str, pd.Series], lower: float, upper: float,
                detail_from: pd.Timestamp | None = None) -> None:
    """Price + RSI (optionally only from `detail_from`, for intraday) above the full equity curve."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if detail_from is not None:
        df, r = df[df.index >= detail_from], r[r.index >= detail_from]
        trades = [t for t in trades if t.entry_date >= detail_from]

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(13, 10),
                                        gridspec_kw={"height_ratios": [3, 1.2, 1.8]})
    ax2.sharex(ax1)
    if detail_from is not None:
        # Intraday: bar-number axis, so nights and weekends don't stretch the chart.
        bar_of = pd.Series(np.arange(len(df)), index=df.index)
        x = bar_of.to_numpy()
        day_starts = np.flatnonzero(np.r_[True, df.index.normalize()[1:] != df.index.normalize()[:-1]])
        for ax in (ax1, ax2):
            for d in day_starts[1:]:
                ax.axvline(d, color="#bbb", lw=0.6)
        ax2.set_xticks(day_starts, [f"{df.index[d]:%Y-%m-%d}" for d in day_starts])
        xpos = lambda ts: bar_of[ts]  # noqa: E731
    else:
        x, xpos = df.index, (lambda ts: ts)
    ax1.plot(x, df["close"], color="#444", lw=0.9, label="Close")
    for side, color, marker in (("long", "#1a9850", "^"), ("short", "#d73027", "v")):
        st = [t for t in trades if t.side == side and t.exit_date in df.index]
        if st:
            ax1.scatter([xpos(t.entry_date) for t in st], [t.entry_price for t in st],
                        color=color, marker=marker, s=40, zorder=3, label=f"{side} Einstieg")
            ax1.scatter([xpos(t.exit_date) for t in st], [t.exit_price for t in st],
                        color=color, marker="x", s=30, zorder=3, label=f"{side} Ausstieg")
    span = "" if detail_from is None else f" (Ausschnitt ab {detail_from:%Y-%m-%d})"
    ax1.set_title(f"{symbol} – RSI-Signale{span}")
    ax1.legend(loc="upper left", fontsize=8)

    ax2.plot(x, r.to_numpy(), color="#5e3c99", lw=0.8)
    ax2.axhline(lower, color="#1a9850", ls="--", lw=0.8)
    ax2.axhline(upper, color="#d73027", ls="--", lw=0.8)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel(r.name.upper())

    for label, eq in equity.items():
        ax3.plot(eq.index, eq, lw=1, label=label)
    ax3.set_yscale("log")
    ax3.set_ylabel("Equity (log, gesamter Zeitraum)")
    ax3.legend(loc="upper left", fontsize=8)

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def fmt_pct(x, digits: int = 2) -> str:
    return "   n/a" if pd.isna(x) else f"{x * 100:6.{digits}f}%"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="*", help="Symbole (Standard: alle CSVs in data/ zur Periode)")
    p.add_argument("--interval", default="1d", help="Kerzenperiode der CSV-Dateien (Standard: 1d)")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--lower", type=float, default=20, help="Kaufsignal unter diesem RSI (Standard: 20)")
    p.add_argument("--upper", type=float, default=80, help="Verkaufssignal über diesem RSI (Standard: 80)")
    p.add_argument("--side", choices=["long", "short", "both"], default="both")
    p.add_argument("--exit", dest="exit_mode", choices=["opposite", "mid", "bars", "days"],
                   default="opposite",
                   help="opposite: long raus bei RSI>upper, short raus bei RSI<lower; "
                        "mid: Ausstieg bei RSI über/unter 50; bars/days: nach --hold Kerzen")
    p.add_argument("--hold", type=int, default=10, help="Haltedauer in Kerzen für --exit bars")
    p.add_argument("--max-hold", type=int, default=None, help="Optionale maximale Haltedauer in Kerzen")
    p.add_argument("--cost-bps", type=float, default=5, help="Kosten+Slippage je Seite in Basispunkten")
    p.add_argument("--horizons", default=None,
                   help="Event-Study-Horizonte in Kerzen (Standard: 1,5,10,20 täglich; 1,5,15,30,60 intraday)")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--session", default="09:30-16:00",
                   help="Intraday: nur Kerzen in diesem Zeitfenster (US-Börsenzeit), 'all' = alles")
    p.add_argument("--entry-window", default="09:35-15:45",
                   help="Intraday: Einstiege nur in diesem Zeitfenster; Positionen schließen immer zum Sitzungsende")
    p.add_argument("--plot-sessions", type=int, default=3,
                   help="Intraday: so viele letzte Handelstage im Kurs-/RSI-Chart zeigen")
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args(argv)
    intraday = is_intraday(args.interval)
    session = parse_window(args.session) if intraday and args.session != "all" else None
    entry_window = parse_window(args.entry_window) if intraday else None

    if args.symbols:
        files = {s: args.data_dir / f"{s}_{args.interval}.csv" for s in args.symbols}
    else:
        files = {f.stem.rsplit("_", 1)[0]: f for f in sorted(args.data_dir.glob(f"*_{args.interval}.csv"))}
    missing = [s for s, f in files.items() if not f.exists()]
    if missing:
        print(f"Keine Daten für: {', '.join(missing)} – erst fetch_candles.py ausführen.")
    files = {s: f for s, f in files.items() if f.exists()}
    if not files:
        return 1

    horizons = [int(h) for h in (args.horizons or ("1,5,15,30,60" if intraday else "1,5,10,20")).split(",")]
    cost = args.cost_bps / 10_000
    sides = [LONG, SHORT] if args.side == "both" else [LONG if args.side == "long" else SHORT]
    run = (f"rsi{args.rsi_period}_{args.lower:g}-{args.upper:g}_{args.exit_mode}_{args.interval}_"
           f"{datetime.now():%Y%m%d-%H%M%S}")
    out = OUTPUT_DIR / run
    (out / "charts").mkdir(parents=True, exist_ok=True)

    all_trades: list[Trade] = []
    summary_rows, pooled = [], {(k, h): [] for k in ("buy", "sell", "base") for h in horizons}
    n_sessions = 0

    for sym, path in files.items():
        df = load_ohlc(path, args.start, args.end, session)
        if len(df) <= args.rsi_period + 2:
            print(f"{sym}: zu wenige Kerzen ({len(df)})")
            continue
        r = rsi(df["close"], args.rsi_period)
        buy, sell = crossings(r, args.lower, args.upper)
        sessions = df.index.normalize().to_numpy() if intraday else None
        entry_ok = None
        if entry_window:
            clock = df.index.time
            entry_ok = (clock >= entry_window[0]) & (clock < entry_window[1])
        n_sessions += len(np.unique(sessions)) if intraday else 0

        for h in horizons:
            fwd = forward_returns(df, h, sessions).to_numpy()
            pooled[("buy", h)].append(fwd[buy.to_numpy()])
            pooled[("sell", h)].append(fwd[sell.to_numpy()])
            pooled[("base", h)].append(fwd)

        bh = perf(pd.Series(np.append(df["open"].to_numpy()[1:] / df["open"].to_numpy()[:-1] - 1,
                                      df["close"].iloc[-1] / df["open"].iloc[-1] - 1), index=df.index))
        equity = {"Buy & Hold": df["close"] / df["open"].iloc[0]}
        sym_trades = []
        for direction in sides:
            trades, strat, pos = simulate(
                df, r, buy if direction == LONG else sell, direction,
                exit_mode=args.exit_mode, lower=args.lower, upper=args.upper,
                hold=args.hold, max_hold=args.max_hold, cost=cost, symbol=sym,
                sessions=sessions, entry_ok=entry_ok)
            sym_trades += trades
            label = "Long" if direction == LONG else "Short"
            equity[f"RSI {label}"] = (1 + strat).cumprod()
            summary_rows.append({"symbol": sym, "side": label.lower(), "bars": len(df),
                                 "from": df.index[0].date(), "to": df.index[-1].date(),
                                 **trade_stats(trades), **perf(strat),
                                 "exposure": (pos != 0).mean(),
                                 "bh_total_return": bh["total_return"], "bh_cagr": bh["cagr"],
                                 "bh_max_drawdown": bh["max_drawdown"]})
        all_trades += sym_trades
        if not args.no_plots:
            detail_from = None
            if intraday:
                days = np.unique(sessions)
                detail_from = pd.Timestamp(days[-min(max(args.plot_sessions, 1), len(days))])
            plot_symbol(out / "charts" / f"{sym}.png", sym, df, r, sym_trades, equity,
                        args.lower, args.upper, detail_from)

    # --- Event study (pooled over all symbols) ---
    event_rows = []
    for kind in ("buy", "sell"):
        for h in horizons:
            row = event_stats(np.concatenate(pooled[(kind, h)]), np.concatenate(pooled[("base", h)]))
            event_rows.append({"signal": kind, "horizon": h, **row})
    events = pd.DataFrame(event_rows)
    summary = pd.DataFrame(summary_rows)
    trades_df = pd.DataFrame([asdict(t) for t in all_trades], columns=[f.name for f in fields(Trade)])

    events.to_csv(out / "event_study.csv", index=False)
    summary.to_csv(out / "summary.csv", index=False)
    trades_df.to_csv(out / "trades.csv", index=False)

    mode = f"Intraday {args.interval}, Sitzung {args.session}, Einstieg {args.entry_window}" if intraday \
        else f"{args.interval}-Kerzen"
    print(f"\nRSI({args.rsi_period})  Kauf < {args.lower:g}  Verkauf > {args.upper:g}  "
          f"Exit: {args.exit_mode}  Kosten: {args.cost_bps:g} bp/Seite  Symbole: {len(files)}  [{mode}]")
    print("\n1) Event Study – Rendite nach Signal vs. alle Kerzen (gepoolt, Horizont in Kerzen)")
    print(f"   {'Signal':<7}{'Horiz.':>7}{'N':>8}{'Ø Rend.':>10}{'Treffer':>9}{'Ø Basis':>10}"
          f"{'Überrend.':>11}{'t-Wert':>8}")
    digits = 3 if intraday else 2
    for _, e in events.iterrows():
        print(f"   {e.signal:<7}{e.horizon:>7}{e.n_signals:>8}{fmt_pct(e['mean'], digits):>10}"
              f"{fmt_pct(e.hit_rate):>9}{fmt_pct(e.base_mean, digits):>10}{fmt_pct(e.excess, digits):>11}"
              f"{e.t_stat:>8.2f}")
    print("   Kauf valide: Überrendite > 0.  Verkauf valide: Überrendite < 0.  |t| > 2 ≈ statistisch auffällig.")

    print("\n2) Strategie je Symbol")
    print(f"   {'Symbol':<10}{'Seite':<7}{'Trades':>7}{'Treffer':>9}{'Ø Trade':>9}{'PF':>6}"
          f"{'Gesamt':>10}{'MaxDD':>9}{'Exposure':>9}{'B&H':>10}")
    for _, s in summary.iterrows():
        pf = "  n/a" if pd.isna(s.profit_factor) else f"{min(s.profit_factor, 99):5.2f}"
        print(f"   {s.symbol:<10}{s.side:<7}{s.trades:>7}{fmt_pct(s.win_rate):>9}"
              f"{fmt_pct(s.avg_trade):>9}{pf:>6}{fmt_pct(s.total_return):>10}"
              f"{fmt_pct(s.max_drawdown):>9}{fmt_pct(s.exposure):>9}{fmt_pct(s.bh_total_return):>10}")

    if all_trades:
        print("\n3) Alle Trades gepoolt")
        for side in ("long", "short"):
            st = trade_stats([t for t in all_trades if t.side == side])
            if st["trades"]:
                per_day = f"  {st['trades'] / n_sessions:.1f} Trades/Tag/Symbol" if n_sessions else ""
                print(f"   {side:<6} Trades {st['trades']:>5}  Treffer {fmt_pct(st['win_rate'])}  "
                      f"Ø {fmt_pct(st['avg_trade'], digits)}  Median {fmt_pct(st['median_trade'], digits)}  "
                      f"PF {min(st['profit_factor'], 99):.2f}  Ø Haltedauer {st['avg_bars']:.1f} Kerzen{per_day}")
                be = st["breakeven_cost_bps"]
                note = ("schon ohne Kosten im Minus" if be <= 0
                        else f"darüber verliert der Ø-Trade Geld; angesetzt: {args.cost_bps:g} bp")
                print(f"          Break-even-Kosten: {be:.2f} bp je Seite ({note})")
    print(f"\nErgebnisse: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
