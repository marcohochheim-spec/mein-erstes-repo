import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orderflow as of  # noqa: E402
import orderflow_study as study  # noqa: E402

TICK = 0.25


# ---------------------------------------------------------------------------
# Synthetic ATAS export: levels first, bars aggregated from them (like ATAS does)
# ---------------------------------------------------------------------------

def synthetic_export(n_sessions=6, bars_per_session=240, edge=0.0, seed=1):
    """1m bars in sessions separated by a 2h break. With `edge` > 0 the next bar's drift follows the
    sign of this bar's delta (planted momentum the study should find)."""
    rng = np.random.default_rng(seed)
    price = 5000.0
    bar_rows, lvl_rows = [], []
    b = 0
    prev_delta_sign = 0
    for s in range(n_sessions):
        t0 = pd.Timestamp("2026-09-01 08:00") + pd.Timedelta(days=s)
        for k in range(bars_per_session):
            drift = edge * prev_delta_sign
            steps = rng.choice([-1, 0, 1], size=8, p=[0.33 - drift, 0.34, 0.33 + drift])
            path = price + np.cumsum(np.concatenate([[0], steps])) * TICK
            lo, hi = path.min(), path.max()
            prices = np.round(np.arange(lo, hi + TICK / 2, TICK), 2)
            buy_bias = rng.uniform(0.2, 0.8)
            bids = rng.poisson(20 * (1 - buy_bias), len(prices))
            asks = rng.poisson(20 * buy_bias, len(prices))
            for p, bd, ak in zip(prices, bids, asks):
                lvl_rows.append((b, p, bd, ak, bd + ak))
            vol, ask, bid = int(bids.sum() + asks.sum()), int(asks.sum()), int(bids.sum())
            delta = ask - bid
            poc = prices[np.argmax(bids + asks)]
            bar_rows.append(dict(bar=b, time=t0 + pd.Timedelta(minutes=k), last_time=t0 + pd.Timedelta(minutes=k, seconds=59),
                                 open=path[0], high=hi, low=lo, close=path[-1], volume=vol, ask=ask, bid=bid,
                                 delta=delta, max_delta=max(delta, 0) + 5, min_delta=min(delta, 0) - 5,
                                 ticks=vol // 2, poc_price=poc, poc_volume=(bids + asks).max(),
                                 vah=hi, val=lo, tick_size=TICK))
            prev_delta_sign = np.sign(delta)
            price = path[-1]
            b += 1
    bars = pd.DataFrame(bar_rows)
    levels = pd.DataFrame(lvl_rows, columns=["bar", "price", "bid", "ask", "volume"])
    return bars, levels


def write_export(tmp_path, bars, levels, stem="ES_1m_test"):
    bp = tmp_path / f"{stem}_bars.csv"
    bars.to_csv(bp, index=False, date_format="%Y-%m-%d %H:%M:%S.%f")
    levels.to_csv(tmp_path / f"{stem}_levels.csv", index=False)
    return bp


@pytest.fixture(scope="module")
def export():
    bars, levels = synthetic_export()
    df = bars.set_index("time")
    return df, levels


# ---------------------------------------------------------------------------
# Signal replicas vs. line-by-line ports of OrderFlowSignalStrategy.Signals.cs
# ---------------------------------------------------------------------------

def cs_delta_imbalance(df, bar, ratio, min_vol, confirm):
    def classify(i):
        c = df.iloc[i]
        if c.volume < min_vol:
            return 0
        if c.bid <= 0 and c.ask <= 0:
            return 0
        if c.bid > 0 and c.ask / c.bid >= ratio:
            return 1
        if c.ask > 0 and c.bid / c.ask >= ratio:
            return -1
        return 0
    if bar - confirm + 1 < 0:
        return 0
    direction = 0
    for i in range(confirm):
        d = classify(bar - i)
        if d == 0:
            return 0
        if direction == 0:
            direction = d
        elif direction != d:
            return 0
    return direction


def cs_cum_delta(df, bar, lookback, min_delta, max_move):
    if bar - 2 * lookback < 0:
        return 0
    recent = df.delta.iloc[bar - lookback + 1: bar + 1].sum()
    prior = df.delta.iloc[bar - 2 * lookback + 1: bar - lookback + 1].sum()
    move = df.close.iloc[bar] - df.close.iloc[bar - lookback]
    change = recent - prior
    if move <= max_move and change >= min_delta:
        return 1
    if move >= -max_move and change <= -min_delta:
        return -1
    return 0


def cs_absorption(df, bar, min_vol, max_move, min_delta):
    c = df.iloc[bar]
    if c.volume < min_vol or abs(c.close - c.open) > max_move:
        return 0
    return 1 if c.delta >= min_delta else -1 if c.delta <= -min_delta else 0


@pytest.mark.parametrize("ratio,min_vol,confirm", [(1.5, 0, 1), (1.3, 60, 2), (1.5, 40, 3)])
def test_delta_imbalance_matches_bot(export, ratio, min_vol, confirm):
    df, _ = export
    got = of.sig_delta_imbalance(df, ratio, min_vol, confirm)
    exp = [cs_delta_imbalance(df, i, ratio, min_vol, confirm) for i in range(len(df))]
    assert got.tolist() == exp
    assert np.any(got != 0)


@pytest.mark.parametrize("lookback,min_delta,max_move", [(5, 60, 0.0), (10, 100, 0.5)])
def test_cum_delta_divergence_matches_bot(export, lookback, min_delta, max_move):
    df, _ = export
    got = of.sig_cum_delta_divergence(df, lookback, min_delta, max_move)
    exp = [cs_cum_delta(df, i, lookback, min_delta, max_move) for i in range(len(df))]
    assert got.tolist() == exp
    assert np.any(got != 0)


def test_absorption_matches_bot(export):
    df, _ = export
    got = of.sig_absorption(df, 100, 0.5, 20)
    exp = [cs_absorption(df, i, 100, 0.5, 20) for i in range(len(df))]
    assert got.tolist() == exp
    assert np.any(got != 0)


def test_rolling_poc_matches_brute_force(export):
    df, levels = export
    df = df.iloc[:150]
    poc = of.rolling_poc(df, levels, 20)
    for i in (19, 20, 21, 77, 149):
        if i < 20:
            assert np.isnan(poc[i])
            continue
        win = levels[levels.bar.between(df.bar.iloc[i - 19], df.bar.iloc[i])]
        vol = win.groupby("price")["volume"].sum()
        assert vol[poc[i]] == vol.max()


def test_combine_votes():
    a, b, c = np.array([1, 1, -1, 0, 1]), np.array([1, -1, -1, 0, 0]), np.array([0, 0, 1, 0, 0])
    assert of.combine_votes([a, b, c], 1).tolist() == [1, 0, -1, 0, 1]
    assert of.combine_votes([a, b, c], 2).tolist() == [1, 0, -1, 0, 0]


# ---------------------------------------------------------------------------
# Footprint features
# ---------------------------------------------------------------------------

def test_footprint_stacked_imbalance_and_unfinished_auction():
    # bar 0: prices 100.00 .. 101.00. Ask[p] vs Bid[p-1 tick] >= 3 at 100.25, 100.50, 100.75 -> stacked 3.
    # Lowest level has bid and ask -> unfinished low; highest has no bid -> finished high.
    lv = pd.DataFrame({
        "bar": [0] * 5,
        "price": [100.0, 100.25, 100.5, 100.75, 101.0],
        "bid": [10, 2, 3, 4, 0],
        "ask": [5, 30, 6, 12, 5],
        "volume": [15, 32, 9, 16, 5],
    })
    fp = of.footprint_features(lv, TICK, ratio=3)
    r = fp.loc[0]
    assert r.imb_buy == 3 and r.stacked_buy == 3   # 30>=30, 6>=6, 12>=12 ; 101.0: 5 < 12
    assert r.imb_sell == 0
    assert r.unfinished_low == 1 and r.unfinished_high == 0


def test_footprint_runs_do_not_cross_bars():
    lv = pd.DataFrame({"bar": [0, 0, 1, 1], "price": [1.0, 1.25, 1.5, 1.75],
                       "bid": [1, 0, 1, 0], "ask": [0, 9, 0, 9], "volume": [1, 9, 1, 9]})
    fp = of.footprint_features(lv, TICK, ratio=3)
    assert fp["stacked_buy"].tolist() == [1, 1]


# ---------------------------------------------------------------------------
# Forward outcomes and bracket simulation
# ---------------------------------------------------------------------------

def bars_from(o, h, l, c, sessions=None):
    idx = pd.date_range("2026-01-05 09:00", periods=len(o), freq="1min")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx), \
        np.zeros(len(o), int) if sessions is None else np.asarray(sessions)


def test_forward_ticks_stays_in_session():
    df, _ = bars_from([10, 11, 12, 13], [10, 11, 12, 13], [10, 11, 12, 13], [10, 11, 12, 13])
    f = of.forward_ticks(df, 2, 1.0, np.array([0, 0, 0, 1]))
    assert f.iloc[0] == 12 - 11
    assert np.isnan(f.iloc[1])  # bar 3 is in the next session


def test_simulate_target_stop_sameBar_and_session_end():
    o = [100, 100, 100, 100, 100, 100, 100, 100]
    h = [100, 101, 103, 100, 104, 100, 100, 100]
    l = [100, 99, 100, 97, 96, 100, 100, 100]
    c = [100, 100, 102, 98, 100, 100, 100, 101]
    df, sess = bars_from(o, h, l, c)
    sig = np.array([1, 0, 0, 0, 0, 0, 1, 0])
    tr = of.simulate(df, sig, 1.0, sess, sl_ticks=2, tp_ticks=3, cost_ticks=0)
    # entry 100 at bar 1; bar 2 high 103 -> target
    assert (tr[0].exit_reason, tr[0].ticks, tr[0].bars_held) == ("target", 3, 2)
    # entry bar 7 = last bar -> closed at session end
    assert tr[1].exit_reason == "session_end" and tr[1].ticks == 1

    sig = np.array([0, 0, 0, 1, 0, 0, 0, 0])   # entry 100 at bar 4: high 104 and low 96 -> stop first
    tr = of.simulate(df, sig, 1.0, sess, sl_ticks=2, tp_ticks=3, cost_ticks=0.5)
    assert tr[0].exit_reason == "stop" and tr[0].ticks == -2.5


def test_simulate_short_gap_breakeven_and_cooldown():
    o = [50, 50, 49, 47, 53, 50, 50, 50]
    h = [50, 50, 49, 48, 53, 50, 50, 50]
    l = [50, 49, 47, 47, 53, 50, 50, 50]
    c = [50, 50, 48, 47, 53, 50, 50, 50]
    df, sess = bars_from(o, h, l, c)
    sig = np.array([-1, -1, -1, 0, 0, -1, 0, 0])
    # short 50 at bar 1; bar 2 low 47 = +3 -> breakeven trigger 3, stop -> 50-1=49 from bar 3;
    # bar 3: high 48 < 49; bar 4 opens 53 above stop -> gap fill at 53
    tr = of.simulate(df, sig, 1.0, sess, sl_ticks=5, tp_ticks=10, cost_ticks=0, be_trigger=3, be_offset=1,
                     cooldown=1)
    assert tr[0].exit_reason == "stop_gap" and tr[0].ticks == -3
    # signals at bar 1,2 are inside the trade; cooldown 1 after exit bar 4 -> bar 5 is blocked too
    assert len(tr) == 1


def test_trade_stats():
    tr = [of.Trade(0, pd.Timestamp(0), 1, 0, 0, 1, "x", t) for t in (4, -2, 4, -2)]
    st = of.trade_stats(tr)
    assert st["win_rate"] == 0.5 and st["total_ticks"] == 4 and st["profit_factor"] == 2
    assert st["max_dd_ticks"] == 2


# ---------------------------------------------------------------------------
# Loading and full study
# ---------------------------------------------------------------------------

def test_load_export_converts_utc(tmp_path):
    bars, levels = synthetic_export(n_sessions=1, bars_per_session=5)
    bp = write_export(tmp_path, bars, levels)
    df, lv = of.load_export(bp, tz="Europe/Berlin")
    assert df.index[0] == pd.Timestamp("2026-09-01 10:00")  # UTC+2 in September
    assert lv is not None and set(lv.bar) == set(df.bar)
    assert of.tick_size(df) == TICK


def test_session_ids():
    idx = pd.DatetimeIndex(["2026-01-05 09:00", "2026-01-05 09:01", "2026-01-05 11:30", "2026-01-05 11:31"])
    assert of.session_ids(idx, 60).tolist() == [0, 0, 1, 1]


def test_study_finds_planted_delta_momentum(tmp_path):
    bars, levels = synthetic_export(n_sessions=8, bars_per_session=300, edge=0.12, seed=7)
    bp = write_export(tmp_path, bars, levels)
    out = tmp_path / "out"
    assert study.main([str(bp), "--out", str(out), "--horizons", "1,3,5", "--main-horizon", "1",
                       "--min-n", "20"]) == 0
    for f in ("feature_ic.csv", "feature_quintiles.csv", "signals_bot_defaults.csv", "param_grid.csv",
              "bracket_grid.csv", "excursions.csv", "summary.md", "feature_ic.png"):
        assert (out / f).exists(), f
    ic = pd.read_csv(out / "feature_ic.csv").set_index("feature")
    # next-bar drift follows the sign of delta -> delta features must have clearly positive IC in both samples
    assert ic.loc["delta_pct", "ic_is_1"] > 0.1 and ic.loc["delta_pct", "ic_oos_1"] > 0.1
    grid = pd.read_csv(out / "param_grid.csv")
    assert grid["robust"].any()
    assert "Orderflow-Studie" in (out / "summary.md").read_text(encoding="utf-8")
