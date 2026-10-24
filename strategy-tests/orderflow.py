"""Order flow data from ATAS: loading, features, replicas of the OrderFlowAutoBot signals, bracket simulation.

Input is what the ATAS indicator `OrderFlowExporter` (ATAS-OrderFlowBot/Exporter) writes:
  *_bars.csv    one row per closed bar: OHLC, volume, ask/bid volume, delta, max/min delta, ticks, POC, value area
  *_levels.csv  footprint: one row per bar and price with bid/ask volume

Conventions (same as the bot): a signal is known at the close of bar t and filled at the open of bar t+1.
Prices stay in instrument units ("points"); results are reported in ticks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

LONG, SHORT = 1, -1

# Defaults of OrderFlowSignalStrategy.Parameters.cs (prices in points, volumes in contracts).
BOT_DEFAULTS = {
    "delta_imbalance": {"ratio": 3.0, "min_volume": 50, "confirm_bars": 1},
    "cum_delta_divergence": {"lookback": 20, "min_delta": 200.0, "max_move": 0.0},
    "absorption": {"min_volume": 500, "max_move": 1.0, "min_delta_abs": 100},
    "poc_proximity": {"lookback": 50, "proximity": 2.0},
}
BOT_RISK_DEFAULTS = {"sl_ticks": 20, "tp_ticks": 30, "cooldown": 5, "be_trigger": 20, "be_offset": 2}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def levels_path_for(bars_path: Path) -> Path:
    return bars_path.with_name(bars_path.name.replace("_bars.csv", "_levels.csv"))


def load_export(bars_path: Path, levels_path: Path | None = None, tz: str | None = None,
                start: str | None = None, end: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Read an OrderFlowExporter file pair. ATAS writes candle times in UTC; `tz` converts them (e.g. Europe/Berlin)."""
    bars = pd.read_csv(bars_path, parse_dates=["time", "last_time"])
    if tz:
        for col in ("time", "last_time"):
            bars[col] = bars[col].dt.tz_localize("UTC").dt.tz_convert(tz).dt.tz_localize(None)
    bars = bars.sort_values("bar").drop_duplicates("bar", keep="last")
    if start:
        bars = bars[bars["time"] >= pd.Timestamp(start)]
    if end:
        bars = bars[bars["time"] < pd.Timestamp(end) + pd.Timedelta(days=1)]
    bars = bars.set_index("time")

    levels = None
    levels_path = levels_path or levels_path_for(bars_path)
    if levels_path.exists():
        levels = pd.read_csv(levels_path, usecols=["bar", "price", "bid", "ask", "volume"])
        levels = levels[levels["bar"].isin(bars["bar"])].sort_values(["bar", "price"], ignore_index=True)
    return bars, levels


def tick_size(bars: pd.DataFrame) -> float:
    ts = bars["tick_size"].replace(0, np.nan).dropna()
    if len(ts):
        return float(ts.iloc[-1])
    diffs = np.diff(np.unique(bars[["open", "high", "low", "close"]].to_numpy().ravel()))
    return float(diffs[diffs > 0].min())


def session_ids(index: pd.DatetimeIndex, gap_minutes: float = 60) -> np.ndarray:
    """A new session starts after a pause longer than `gap_minutes` (futures: daily maintenance break)."""
    gaps = index.to_series().diff() > pd.Timedelta(minutes=gap_minutes)
    return gaps.cumsum().to_numpy()


def session_last_bar(sessions: np.ndarray) -> np.ndarray:
    """Position of the last bar of each bar's session."""
    pos = np.arange(len(sessions))
    return pd.Series(pos).groupby(sessions).transform("max").to_numpy()


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def footprint_features(levels: pd.DataFrame, tick: float, ratio: float = 3.0, min_volume: float = 1) -> pd.DataFrame:
    """Per bar: diagonal imbalances (ask[p] vs bid[p-1 tick]), longest stacked run, unfinished auctions."""
    lv = levels
    bar = lv["bar"].to_numpy()
    price = lv["price"].to_numpy(float)
    bid = lv["bid"].to_numpy(float)
    ask = lv["ask"].to_numpy(float)

    adj_prev = np.zeros(len(lv), bool)
    adj_prev[1:] = (bar[1:] == bar[:-1]) & np.isclose(price[1:] - price[:-1], tick)
    adj_next = np.append(adj_prev[1:], False)
    bid_below = np.where(adj_prev, np.roll(bid, 1), np.nan)
    ask_above = np.where(adj_next, np.roll(ask, -1), np.nan)

    with np.errstate(invalid="ignore"):
        buy = adj_prev & (ask >= min_volume) & (ask > 0) & (ask >= ratio * bid_below)
        sell = adj_next & (bid >= min_volume) & (bid > 0) & (bid >= ratio * ask_above)

    def longest_run(flag: np.ndarray) -> pd.Series:
        cont = np.zeros(len(flag), bool)
        cont[1:] = flag[1:] & flag[:-1] & adj_prev[1:]
        run_id = np.cumsum(~cont)
        runs = pd.DataFrame({"bar": bar, "run": run_id, "f": flag}).groupby(["bar", "run"])["f"].sum()
        return runs.groupby(level="bar").max()

    g = pd.DataFrame({"bar": bar, "buy": buy, "sell": sell}).groupby("bar")
    out = pd.DataFrame({
        "imb_buy": g["buy"].sum(),
        "imb_sell": g["sell"].sum(),
        "stacked_buy": longest_run(buy),
        "stacked_sell": longest_run(sell),
    })
    first = lv.groupby("bar").head(1).set_index("bar")   # lowest price of each bar
    last = lv.groupby("bar").tail(1).set_index("bar")    # highest price of each bar
    out["unfinished_low"] = ((first["bid"] > 0) & (first["ask"] > 0)).astype(int)
    out["unfinished_high"] = ((last["bid"] > 0) & (last["ask"] > 0)).astype(int)
    return out


def add_features(bars: pd.DataFrame, levels: pd.DataFrame | None, tick: float, sessions: np.ndarray,
                 window: int = 50, div_lookback: int = 10, imb_ratio: float = 3.0) -> pd.DataFrame:
    """Order flow features known at the close of each bar. Signed so that positive = 'buyers stronger'."""
    df = bars.copy()
    vol = df["volume"].replace(0, np.nan)
    rng = df["high"] - df["low"]
    df["range_ticks"] = rng / tick
    df["body_ticks"] = (df["close"] - df["open"]) / tick
    df["close_pos"] = ((df["close"] - df["low"]) / rng.replace(0, np.nan)).fillna(0.5) - 0.5
    df["delta_pct"] = df["delta"] / vol
    df["delta_z"] = df["delta"] / df["delta"].rolling(window, min_periods=window // 2).std()
    df["rel_volume"] = df["volume"] / df["volume"].rolling(window, min_periods=window // 2).median()
    # Where inside the bar the delta finished: +0.5 = closed at its high (no seller comeback), -0.5 = at its low.
    d_rng = (df["max_delta"] - df["min_delta"]).replace(0, np.nan)
    df["delta_close_pos"] = ((df["delta"] - df["min_delta"]) / d_rng).fillna(0.5) - 0.5
    if "poc_price" in df and (df["poc_price"] > 0).any():
        df["poc_pos"] = ((df["poc_price"] - df["low"]) / rng.replace(0, np.nan)).fillna(0.5) - 0.5
    # Absorption: much volume, little range. Signed by delta like the bot's module (+ = aggressive buying
    # that did not move price much). Large absolute values = strong absorption.
    med_rng = df["range_ticks"].rolling(window, min_periods=window // 2).median()
    df["absorption"] = np.sign(df["delta"]) * df["rel_volume"] / (df["range_ticks"] / med_rng).clip(lower=0.25)

    s = pd.Series(sessions, index=df.index)
    df["cvd_session"] = df["delta"].groupby(sessions).cumsum().to_numpy()
    # Delta/price divergence over `div_lookback` bars: z-scored delta sum minus z-scored price change.
    dsum = df["delta"].rolling(div_lookback).sum()
    pchg = df["close"].diff(div_lookback)
    same = s.eq(s.shift(div_lookback))
    z = lambda x: x / x.rolling(window * 4, min_periods=window).std()  # noqa: E731
    df["delta_price_div"] = (z(dsum) - z(pchg)).where(same)

    if levels is not None and len(levels):
        fp = footprint_features(levels, tick, ratio=imb_ratio)
        df = df.join(fp, on="bar")
        df["stacked_net"] = df["stacked_buy"].fillna(0) - df["stacked_sell"].fillna(0)
        df["imb_net"] = df["imb_buy"].fillna(0) - df["imb_sell"].fillna(0)
        df["unfinished_net"] = df["unfinished_high"].fillna(0) - df["unfinished_low"].fillna(0)
    return df


FEATURES = ["delta_pct", "delta_z", "delta_close_pos", "rel_volume", "absorption", "close_pos", "body_ticks",
            "poc_pos", "delta_price_div", "stacked_net", "imb_net", "unfinished_net"]


# ---------------------------------------------------------------------------
# Forward outcomes
# ---------------------------------------------------------------------------

def forward_ticks(df: pd.DataFrame, horizon: int, tick: float, sessions: np.ndarray) -> pd.Series:
    """Long result in ticks: entry at the open after the signal bar, exit at the close `horizon` bars after it.
    NaN when the window leaves the session."""
    fwd = (df["close"].shift(-horizon) - df["open"].shift(-1)) / tick
    s = pd.Series(sessions, index=df.index)
    return fwd.where(s.shift(-horizon).eq(s))


def excursions(df: pd.DataFrame, horizon: int, tick: float, sessions: np.ndarray) -> tuple[pd.Series, pd.Series]:
    """(MFE, MAE) of a long entry at the next open over the following `horizon` bars, in ticks (both >= 0)."""
    entry = df["open"].shift(-1)
    hi = df["high"][::-1].rolling(horizon, min_periods=horizon).max()[::-1].shift(-1)
    lo = df["low"][::-1].rolling(horizon, min_periods=horizon).min()[::-1].shift(-1)
    s = pd.Series(sessions, index=df.index)
    ok = s.shift(-horizon).eq(s)
    return ((hi - entry) / tick).where(ok), ((entry - lo) / tick).where(ok)


# ---------------------------------------------------------------------------
# Replicas of the bot's signal modules (OrderFlowSignalStrategy.Signals.cs) - +1 long, -1 short, 0 none
# ---------------------------------------------------------------------------

def sig_delta_imbalance(df: pd.DataFrame, ratio: float, min_volume: float, confirm_bars: int) -> np.ndarray:
    ask, bid, vol = (df[c].to_numpy(float) for c in ("ask", "bid", "volume"))
    with np.errstate(divide="ignore", invalid="ignore"):
        up = (bid > 0) & (ask / bid >= ratio)
        down = ~up & (ask > 0) & (bid / ask >= ratio)
    cls = np.where(vol < min_volume, 0, np.where(up, 1, np.where(down, -1, 0)))
    if confirm_bars <= 1:
        return cls
    s = pd.Series(cls)
    all_up = s.eq(1).rolling(confirm_bars).sum().eq(confirm_bars)
    all_down = s.eq(-1).rolling(confirm_bars).sum().eq(confirm_bars)
    return np.where(all_up, 1, np.where(all_down, -1, 0))


def sig_cum_delta_divergence(df: pd.DataFrame, lookback: int, min_delta: float, max_move: float) -> np.ndarray:
    delta = df["delta"]
    recent = delta.rolling(lookback).sum()
    prior = recent.shift(lookback)
    change = (recent - prior).to_numpy()
    move = df["close"].diff(lookback).to_numpy()
    with np.errstate(invalid="ignore"):
        long_ = (move <= max_move) & (change >= min_delta)
        short = (move >= -max_move) & (change <= -min_delta)
    out = np.where(long_, 1, np.where(short, -1, 0))
    out[: 2 * lookback] = 0  # bot needs bar - 2*lookback >= 0
    return out


def sig_absorption(df: pd.DataFrame, min_volume: float, max_move: float, min_delta_abs: float) -> np.ndarray:
    vol, delta = df["volume"].to_numpy(float), df["delta"].to_numpy(float)
    move = (df["close"] - df["open"]).abs().to_numpy()
    ok = (vol >= min_volume) & (move <= max_move)
    return np.where(ok & (delta >= min_delta_abs), 1, np.where(ok & (delta <= -min_delta_abs), -1, 0))


def rolling_poc(df: pd.DataFrame, levels: pd.DataFrame, lookback: int) -> np.ndarray:
    """POC (highest-volume price) of the last `lookback` bars including the current one; NaN before that."""
    by_bar = {b: (g["price"].to_numpy(), g["volume"].to_numpy(float)) for b, g in levels.groupby("bar")}
    bar_ids = df["bar"].to_numpy()
    vol: dict[float, float] = {}
    out = np.full(len(df), np.nan)
    for i, b in enumerate(bar_ids):
        p, v = by_bar.get(b, ((), ()))
        for px, vv in zip(p, v):
            vol[px] = vol.get(px, 0.0) + vv
        if i >= lookback:
            p0, v0 = by_bar.get(bar_ids[i - lookback], ((), ()))
            for px, vv in zip(p0, v0):
                vol[px] -= vv
        if i >= lookback and vol:  # bot: bar - lookback >= 0 (bar-indices), window = lookback bars
            out[i] = max(vol, key=vol.get)
    return out


def sig_poc_proximity(df: pd.DataFrame, poc: np.ndarray, proximity: float) -> np.ndarray:
    close, delta = df["close"].to_numpy(float), df["delta"].to_numpy(float)
    with np.errstate(invalid="ignore"):
        near = np.abs(close - poc) <= proximity
        return np.where(near & (close >= poc) & (delta > 0), 1,
                        np.where(near & (close <= poc) & (delta < 0), -1, 0))


def combine_votes(signals: list[np.ndarray], min_signals: int) -> np.ndarray:
    """Bot's EvaluateSignals: enough modules agree and more long than short votes (or vice versa)."""
    stack = np.vstack(signals)
    longs, shorts = (stack > 0).sum(0), (stack < 0).sum(0)
    return np.where((longs >= min_signals) & (longs > shorts), 1,
                    np.where((shorts >= min_signals) & (shorts > longs), -1, 0))


# ---------------------------------------------------------------------------
# Bracket simulation (one position at a time, like the bot)
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    signal_bar: int
    entry_time: pd.Timestamp
    side: int
    entry: float
    exit: float
    bars_held: int
    exit_reason: str
    ticks: float  # net of costs


def simulate(df: pd.DataFrame, signal: np.ndarray, tick: float, sessions: np.ndarray, *,
             sl_ticks: float, tp_ticks: float, cost_ticks: float = 1.0, cooldown: int = 0,
             max_bars: int | None = None, be_trigger: float | None = None, be_offset: float = 0,
             allowed: np.ndarray | None = None) -> list[Trade]:
    """Stop/target bracket from the next open. Same-bar stop+target counts as stop (conservative);
    a gap through the stop fills at the open. Breakeven moves the stop from the bar after the trigger.
    Positions are closed at the session's last close."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    last_of = session_last_bar(sessions)
    times = df.index
    n = len(df)
    trades: list[Trade] = []
    free_from = 0
    for t in np.flatnonzero(signal):
        e = t + 1
        if t < free_from or e >= n or sessions[e] != sessions[t]:
            continue
        if allowed is not None and not allowed[t]:
            continue
        d = int(signal[t])
        entry = o[e]
        stop = entry - d * sl_ticks * tick
        target = entry + d * tp_ticks * tick
        end = last_of[e] if max_bars is None else min(last_of[e], e + max_bars - 1)
        exit_px, reason, j = None, "session_end", e
        be_done = False
        while j <= end:
            if j > e and (o[j] - stop) * d <= 0:
                exit_px, reason = o[j], "stop_gap"
                break
            if (l[j] if d == LONG else h[j]) * d <= stop * d:
                exit_px, reason = stop, "stop"
                break
            if (h[j] if d == LONG else l[j]) * d >= target * d:
                exit_px, reason = target, "target"
                break
            if be_trigger and not be_done and ((h[j] if d == LONG else l[j]) - entry) * d >= be_trigger * tick:
                stop = entry + d * be_offset * tick
                be_done = True
            j += 1
        if exit_px is None:
            j = end
            exit_px, reason = c[end], ("timeout" if max_bars and end < last_of[e] else "session_end")
        trades.append(Trade(t, times[e], d, entry, exit_px, j - e + 1, reason,
                            d * (exit_px - entry) / tick - cost_ticks))
        free_from = j + 1 + cooldown
    return trades


def trade_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    x = np.array([t.ticks for t in trades])
    wins, losses = x[x > 0], x[x <= 0]
    eq = np.cumsum(x)
    return {
        "trades": len(x),
        "win_rate": (x > 0).mean(),
        "avg_ticks": x.mean(),
        "total_ticks": x.sum(),
        "profit_factor": wins.sum() / -losses.sum() if losses.sum() < 0 else np.inf,
        "max_dd_ticks": (np.maximum.accumulate(eq) - eq).max(),
        "t_stat": x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 1 and x.std(ddof=1) > 0 else np.nan,
        "long_share": np.mean([t.side == LONG for t in trades]),
    }
