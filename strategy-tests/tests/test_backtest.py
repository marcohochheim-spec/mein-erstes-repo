import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtest_rsi as bt  # noqa: E402
from dxlink import _parse_compact, CANDLE_FIELDS  # noqa: E402
from fetch_candles import to_frame  # noqa: E402
from indicators import rsi  # noqa: E402

# Wilder RSI reference example (StockCharts "Relative Strength Index" ChartSchool table).
WILDER_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def make_ohlc(close, start="2020-01-01"):
    close = np.asarray(close, float)
    idx = pd.date_range(start, periods=len(close), freq="B")
    opens = np.concatenate([[close[0]], close[:-1]])  # open = previous close
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, close) + 0.1,
                         "low": np.minimum(opens, close) - 0.1, "close": close}, index=idx)


def test_rsi_matches_wilder_reference():
    # StockCharts lists 70.53, 66.32, 66.55, 69.41, 66.36, 57.97 but rounds the averages to two
    # decimals (0.24 / 0.10); unrounded (0.2386 / 0.0996) every value is ~0.06 lower.
    r = rsi(pd.Series(WILDER_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    expected = [70.464, 66.250, 66.481, 69.347, 66.295, 57.915]
    assert r.iloc[14:].tolist() == pytest.approx(expected, abs=0.001)


def test_rsi_extremes():
    assert rsi(pd.Series(np.arange(1, 40.0)), 14).iloc[-1] == 100
    assert rsi(pd.Series(np.arange(40, 1.0, -1)), 14).iloc[-1] == 0


def test_crossings_fire_only_on_the_cross():
    r = pd.Series([50, 25, 15, 10, 30, 85, 90, 70])
    buy, sell = bt.crossings(r, 20, 80)
    assert buy.tolist() == [False, False, True, False, False, False, False, False]
    assert sell.tolist() == [False, False, False, False, False, True, False, False]


def test_simulate_fills_at_next_open_no_lookahead():
    df = make_ohlc([10, 11, 12, 13, 14, 15])
    r = pd.Series([50, 15, 30, 40, 85, 60], index=df.index, dtype=float)
    entry = pd.Series([False, True, False, False, False, False], index=df.index)
    trades, strat, pos = bt.simulate(df, r, entry, bt.LONG, exit_mode="opposite", lower=20, upper=80,
                                     hold=10, max_hold=None, cost=0.0)
    assert len(trades) == 1
    t = trades[0]
    assert t.signal_date == df.index[1]
    assert t.entry_date == df.index[2] and t.entry_price == df["open"].iloc[2]   # next open
    assert t.exit_date == df.index[5] and t.exit_price == df["open"].iloc[5]     # open after RSI>80
    assert t.net_return == pytest.approx(df["open"].iloc[5] / df["open"].iloc[2] - 1)
    # Per-bar returns compound to the trade return when costs are zero (long side).
    assert (1 + strat).prod() - 1 == pytest.approx(t.net_return)
    assert pos.tolist() == [0, 0, 1, 1, 1, 0]


def test_simulate_costs_and_days_exit():
    df = make_ohlc([10, 10, 10, 10, 10, 10, 10])
    r = pd.Series(50.0, index=df.index)
    entry = pd.Series([True] + [False] * 6, index=df.index)
    trades, _, _ = bt.simulate(df, r, entry, bt.LONG, exit_mode="days", lower=20, upper=80,
                               hold=3, max_hold=None, cost=0.001)
    assert trades[0].bars_held == 3
    assert trades[0].net_return == pytest.approx(-0.002)


def test_short_side_profits_from_falling_price():
    df = make_ohlc([20, 19, 18, 17, 16, 15])
    r = pd.Series([50, 85, 70, 60, 15, 30], index=df.index, dtype=float)
    entry = pd.Series([False, True, False, False, False, False], index=df.index)
    trades, strat, _ = bt.simulate(df, r, entry, bt.SHORT, exit_mode="opposite", lower=20, upper=80,
                                   hold=10, max_hold=None, cost=0.0)
    assert trades[0].side == "short" and trades[0].net_return > 0
    # Equity path must equal the static short's P&L, not a daily-rebalanced short.
    assert (1 + strat).prod() - 1 == pytest.approx(trades[0].net_return)


def test_end_to_end_on_synthetic_mean_reverting_data(tmp_path, monkeypatch):
    rng = np.random.default_rng(7)
    n = 2500
    x = np.zeros(n)
    for i in range(1, n):  # strongly mean-reverting log price
        x[i] = 0.9 * x[i - 1] + rng.normal(0, 0.03)
    df = make_ohlc(100 * np.exp(x))
    df.index.name = "time"
    df.to_csv(tmp_path / "SYN_1d.csv")
    monkeypatch.setattr(bt, "OUTPUT_DIR", tmp_path / "out")

    assert bt.main(["--data-dir", str(tmp_path), "--rsi-period", "2", "--lower", "10",
                    "--upper", "90", "--exit", "mid", "--no-plots"]) == 0
    run = next((tmp_path / "out").iterdir())
    events = pd.read_csv(run / "event_study.csv")
    buy5 = events[(events.signal == "buy") & (events.horizon == 5)].iloc[0]
    sell5 = events[(events.signal == "sell") & (events.horizon == 5)].iloc[0]
    # On mean-reverting data, oversold must outperform and overbought must underperform.
    assert buy5.excess > 0 and sell5.excess < 0
    assert len(pd.read_csv(run / "trades.csv")) > 50


def make_intraday(closes_per_session, start="2025-03-03"):
    """Consecutive business days, 1m bars from 09:30; open = previous close within a session."""
    frames = []
    for day, closes in zip(pd.bdate_range(start, periods=len(closes_per_session)), closes_per_session):
        closes = np.asarray(closes, float)
        idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), periods=len(closes), freq="1min")
        opens = np.concatenate([[closes[0]], closes[:-1]])
        frames.append(pd.DataFrame({"open": opens, "high": np.maximum(opens, closes),
                                    "low": np.minimum(opens, closes), "close": closes}, index=idx))
    df = pd.concat(frames)
    df.index.name = "time"
    return df


def test_intraday_position_is_closed_at_session_end():
    df = make_intraday([[10, 11, 12, 13], [20, 21, 22, 23]])
    sessions = df.index.normalize().to_numpy()
    r = pd.Series(50.0, index=df.index)
    entry = pd.Series([True] + [False] * 7, index=df.index)
    trades, strat, pos = bt.simulate(df, r, entry, bt.LONG, exit_mode="opposite", lower=20, upper=80,
                                     hold=10, max_hold=None, cost=0.0, sessions=sessions)
    t = trades[0]
    assert t.entry_price == 10 and t.exit_price == 13          # open of bar 1 = close of bar 0
    assert t.exit_date == df.index[3] and not t.still_open     # close of the session's last bar
    assert pos.tolist() == [0, 1, 1, 1, 0, 0, 0, 0]            # nothing held overnight
    assert (1 + strat).prod() - 1 == pytest.approx(13 / 10 - 1)


def test_intraday_signal_on_last_bar_is_not_carried_overnight():
    df = make_intraday([[10, 11, 12], [20, 21, 22]])
    sessions = df.index.normalize().to_numpy()
    entry = pd.Series([False, False, True, False, False, False], index=df.index)
    trades, _, _ = bt.simulate(df, pd.Series(50.0, index=df.index), entry, bt.LONG, exit_mode="mid",
                               lower=20, upper=80, hold=10, max_hold=None, cost=0.0, sessions=sessions)
    assert trades == []


def test_intraday_entry_window_blocks_entries():
    df = make_intraday([[10, 11, 12, 13, 14]])
    entry = pd.Series([True, False, True, False, False], index=df.index)
    entry_ok = np.array([False, True, True, True, True])
    trades, _, _ = bt.simulate(df, pd.Series(50.0, index=df.index), entry, bt.LONG, exit_mode="opposite",
                               lower=20, upper=80, hold=10, max_hold=None, cost=0.0,
                               sessions=df.index.normalize().to_numpy(), entry_ok=entry_ok)
    assert [t.signal_date for t in trades] == [df.index[2]]


def test_forward_returns_stop_at_session_boundary():
    df = make_intraday([[10, 11, 12], [20, 21, 22]])
    fwd = bt.forward_returns(df, 2, df.index.normalize().to_numpy())
    assert fwd.iloc[0] == pytest.approx(12 / 10 - 1)            # open of bar 1 -> close of bar 2
    assert fwd.iloc[1:3].isna().all()                          # would reach into the next day


def test_load_ohlc_session_filter(tmp_path):
    idx = pd.to_datetime(["2025-03-03 09:29", "2025-03-03 09:30", "2025-03-03 15:59", "2025-03-03 16:00"])
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=pd.Index(idx, name="time"))
    df.to_csv(tmp_path / "X_1m.csv")
    loaded = bt.load_ohlc(tmp_path / "X_1m.csv", session=bt.parse_window("09:30-16:00"))
    assert [t.strftime("%H:%M") for t in loaded.index] == ["09:30", "15:59"]


def test_end_to_end_intraday(tmp_path, monkeypatch):
    rng = np.random.default_rng(3)
    sessions = []
    for _ in range(30):
        x = np.zeros(390)
        for i in range(1, 390):
            x[i] = 0.8 * x[i - 1] + rng.normal(0, 0.001)
        sessions.append(100 * np.exp(x))
    make_intraday(sessions).to_csv(tmp_path / "SYN_1m.csv")
    monkeypatch.setattr(bt, "OUTPUT_DIR", tmp_path / "out")

    assert bt.main(["--data-dir", str(tmp_path), "--interval", "1m", "--exit", "mid",
                    "--rsi-period", "2", "--lower", "10", "--upper", "90"]) == 0
    run = next((tmp_path / "out").iterdir())
    trades = pd.read_csv(run / "trades.csv", parse_dates=["entry_date", "exit_date"])
    assert len(trades) > 100
    assert (trades.entry_date.dt.date == trades.exit_date.dt.date).all()   # never overnight
    assert (trades.entry_date.dt.strftime("%H:%M") >= "09:36").all()       # signal >= 09:35 -> fill next bar
    assert (run / "charts" / "SYN.png").exists()


def test_repair_ohlc_split_and_dividend_glitches():
    from fetch_candles import repair_ohlc
    df = pd.DataFrame({"open": [126.00, 591.36, 50.0], "high": [126.44, 593.00, 51.0],
                       "low": [124.58, 585.85, 49.0], "close": [499.23, 584.1345, 50.5]},
                      index=pd.to_datetime(["2020-08-28", "2024-12-19", "2025-01-02"]))
    fixed, notes = repair_ohlc(df)
    assert fixed["close"].iloc[0] == pytest.approx(499.23 / 4)      # unadjusted pre-split close
    assert fixed["close"].iloc[1] == 585.85                          # clamped into the bar
    assert fixed["close"].iloc[2] == 50.5                            # healthy bar untouched
    assert len(notes) == 2 and "Split-Faktor 4" in notes[0]


def test_merge_archive(tmp_path):
    from fetch_candles import merge_archive
    t = pd.date_range("2026-10-01 09:30", periods=5, freq="1min", name="time")
    old = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [1, 2, 3, 4, 5.0]}, index=t)
    path = tmp_path / "SPY_1m.csv"
    old.to_csv(path)

    # Overlap: last archived bar was fetched mid-minute and gets replaced; two bars are new.
    new = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [4, 5.5, 6, 7.0]},
                       index=pd.date_range("2026-10-01 09:33", periods=4, freq="1min", name="time"))
    merged, added, gap = merge_archive(path, new)
    assert added == 2 and gap is None
    assert merged["close"].tolist() == [1, 2, 3, 4, 5.5, 6, 7]

    # No overlap -> warning about a possible hole.
    later = new.copy()
    later.index = later.index + pd.Timedelta(days=30)
    _, _, gap = merge_archive(path, later)
    assert gap and "Lücke" in gap

    # No archive yet -> everything is new.
    merged, added, gap = merge_archive(tmp_path / "QQQ_1m.csv", new)
    assert added == 4 and gap is None


def test_excursion_before_breakeven():
    from excursion_rsi import excursions, LONG, SHORT
    idx = pd.date_range("2026-10-01 09:30", periods=6, freq="1min")
    #            signal  entry   up     peak   back<=100  later high (must not count)
    df = pd.DataFrame({"open":  [99.0, 100.0, 100.2, 100.4, 100.3, 99.9],
                       "high":  [99.5, 100.3, 100.5, 100.9, 100.4, 101.5],
                       "low":   [98.5, 99.9,  100.1, 100.3, 100.05, 99.8],
                       "close": [99.2, 100.2, 100.4, 100.3, 100.0, 101.0]}, index=idx)
    ex = excursions(df, np.array([0]), LONG).iloc[0]
    # Entry 100.0 at bar 1; entry bar's own low (99.9) is ignored; bar 4 low 100.05 stays above entry,
    # bar 5 low 99.8 touches -> break-even at bar 5. Peak = high 100.9 at bar 3 -> 90 bp.
    assert ex.entry == 100.0
    assert ex.mfe_bp == pytest.approx(90)
    assert ex.minutes_to_peak == 2 and ex.minutes_to_breakeven == 4 and ex.returned_to_entry

    # Short from the same entry: bar 2 high 100.5 >= 100 -> break-even immediately; MFE = entry bar only.
    ex = excursions(df, np.array([0]), SHORT).iloc[0]
    assert ex.mfe_bp == pytest.approx(10)          # entry bar low 99.9
    assert ex.minutes_to_breakeven == 1


def test_excursion_no_return_and_session_end():
    from excursion_rsi import excursions, LONG
    df = make_intraday([[100, 100.5, 101, 102], [90, 91]])
    ex = excursions(df, np.array([0, 3]), LONG)
    assert len(ex) == 1                             # signal on the session's last bar is skipped
    row = ex.iloc[0]
    assert not row.returned_to_entry and np.isnan(row.minutes_to_breakeven)
    assert row.mfe_bp == pytest.approx(200)         # high 102.0 vs entry 100 (high = max(open, close))


def test_stream_symbol():
    from fetch_candles import stream_symbol
    assert stream_symbol("SPY", "1d", False) == "SPY{=1d}"
    assert stream_symbol("SPY", "1m", False) == "SPY{=1m,tho=true}"
    assert stream_symbol("SPY", "1m", True) == "SPY{=1m}"
    assert stream_symbol("SPY", "1mo", False) == "SPY{=1mo}"


def test_parse_compact_and_frame():
    data = ["Candle", ["Candle", "AAPL{=1d}", 0, 1704067200000, 1, 2, 0.5, 1.5, 1000, 1.2, "NaN", "NaN",
                       "Candle", "AAPL{=1d}", 8, 1704153600000, "NaN", "NaN", "NaN", "NaN", 0, "NaN", "NaN", "NaN",
                       "Candle", "AAPL{=1d}", 0, 1703980800000, 1, 2, 0.5, 1.4, 900, 1.1, "NaN", "NaN"]]
    events = list(_parse_compact(data, {"Candle": CANDLE_FIELDS}))
    assert len(events) == 3 and events[0]["close"] == 1.5
    df = to_frame(events, "1d")
    assert list(df.index.strftime("%Y-%m-%d")) == ["2023-12-31", "2024-01-01"]  # NaN candle dropped, sorted
