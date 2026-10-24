import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from ib_async import BarData

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch_ib as fi  # noqa: E402

UTC = timezone.utc


def minute_bars(start: datetime, n: int) -> list[BarData]:
    return [BarData(date=start + timedelta(minutes=i), open=100 + i, high=101 + i, low=99 + i,
                    close=100.5 + i, volume=1000, average=100.2 + i, barCount=10) for i in range(n)]


class FakeIB:
    """Serves `bars` like reqHistoricalData: up to `per_request` bars ending at `end` (inclusive or not)."""

    def __init__(self, bars, per_request=100, inclusive=False):
        self.bars, self.per_request, self.inclusive = bars, per_request, inclusive
        self.calls = []

    def __call__(self, end, duration):
        self.calls.append((end, duration))
        if end is None:
            pool = self.bars
        elif self.inclusive:
            pool = [b for b in self.bars if b.date <= end]
        else:
            pool = [b for b in self.bars if b.date < end]
        return pool[-self.per_request:]


T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def test_paging_collects_full_history_in_order():
    bars = minute_bars(T0, 950)
    fake = FakeIB(bars, per_request=100)
    out, problem = fi.fetch_history(fake, T0 - timedelta(days=1), "1 M")
    assert problem is None
    assert [b.date for b in out] == [b.date for b in bars]
    assert fake.calls[0][0] is None                      # first request ends "now"
    assert fake.calls[1][0] == bars[-100].date            # next one ends at the oldest bar so far


def test_paging_stops_at_start_and_trims():
    bars = minute_bars(T0, 950)
    start = bars[500].date
    fake = FakeIB(bars, per_request=100)
    out, _ = fi.fetch_history(fake, start, "1 M")
    assert out[0].date == start and out[-1].date == bars[-1].date
    assert len(fake.calls) == 5                           # 900..949, ..., 450..549 reaches the start


def test_paging_with_inclusive_end_dedupes_in_frame():
    bars = minute_bars(T0, 250)
    out, _ = fi.fetch_history(FakeIB(bars, per_request=100, inclusive=True), T0 - timedelta(days=1), "1 M")
    df = fi.to_frame(out, "1m")
    assert len(df) == 250 and df.index.is_unique and df.index.is_monotonic_increasing


def test_paging_stops_without_progress():
    bars = minute_bars(T0, 50)
    stuck = lambda end, duration: bars  # noqa: E731  ignores end -> would loop forever
    out, problem = fi.fetch_history(stuck, T0 - timedelta(days=1), "1 M")
    assert len(out) == 50 and problem is None


def test_single_request_uses_now_and_span_duration():
    bars = minute_bars(T0, 10)
    fake = FakeIB(bars)
    out, _ = fi.fetch_history(fake, T0 - timedelta(days=800), "1 M", single=True, now=T0 + timedelta(days=1))
    assert len(out) == 10
    assert fake.calls == [(None, "3 Y")]


def test_timeout_keeps_recent_part_and_reports():
    bars = minute_bars(T0, 300)
    fake = FakeIB(bars, per_request=100)
    calls = {"n": 0}

    def flaky(end, duration):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise fi.RequestFailed("keine Antwort")
        return fake(end, duration)

    out, problem = fi.fetch_history(flaky, T0 - timedelta(days=1), "1 M")
    assert [b.date for b in out] == [b.date for b in bars[-100:]]
    assert problem == "keine Antwort"


# ---------------------------------------------------------------------------
# robust_request
# ---------------------------------------------------------------------------

def test_robust_request_retries_silent_timeouts():
    errors, slept, answers = [], [], [[], [], ["bar"]]
    req = fi.robust_request(lambda e, d: answers.pop(0), errors, slept.append, retries=2)
    assert req(None, "1 D") == ["bar"]
    assert slept == [5.0, 5.0]


def test_robust_request_raises_after_all_timeouts():
    req = fi.robust_request(lambda e, d: [], [], lambda s: None, retries=1)
    with pytest.raises(fi.RequestFailed):
        req(None, "1 D")


def test_robust_request_no_data_error_is_end_of_history():
    errors, slept = [], []

    def call(end, duration):
        errors.append(fi.IBError(1, 162, "Historical Market Data Service error message:HMDS query returned no data"))
        return []

    req = fi.robust_request(call, errors, slept.append)
    assert req(None, "1 D") == [] and slept == []
    assert errors[0].code == 162                          # kept for the caller's message


def test_robust_request_waits_on_pacing_violation():
    errors, slept, n = [], [], {"i": 0}

    def call(end, duration):
        n["i"] += 1
        if n["i"] == 1:
            errors.append(fi.IBError(1, 162, "Historical data request pacing violation"))
            return []
        return ["bar"]

    req = fi.robust_request(call, errors, slept.append, pacing_pause=60)
    assert req(None, "1 D") == ["bar"] and slept == [60]


# ---------------------------------------------------------------------------
# Pacer
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_pacer_min_gap_and_window():
    clock = FakeClock()
    pacer = fi.Pacer(max_requests=3, window=60, min_gap=1, clock=clock, sleep=clock.sleep)
    waits = [pacer.wait() for _ in range(4)]
    assert waits[:3] == [0.0, 1.0, 1.0]
    assert clock.t == pytest.approx(60.0)                 # 4th waits until the 1st left the window
    assert waits[3] == pytest.approx(58.0)


def test_pacer_never_exceeds_limit():
    clock = FakeClock()
    pacer = fi.Pacer(max_requests=55, window=600, min_gap=0.5, clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(200):
        pacer.wait()
        times.append(clock.t)
    for i, t in enumerate(times):
        assert sum(1 for u in times[i:] if u < t + 600) <= 55


# ---------------------------------------------------------------------------
# Conversion and helpers
# ---------------------------------------------------------------------------

def test_to_frame_intraday_in_new_york_time():
    bars = minute_bars(datetime(2026, 7, 1, 13, 30, tzinfo=UTC), 2)   # 09:30 EDT
    df = fi.to_frame(bars, "1m")
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "vwap", "trades"]
    assert df.index[0] == pd.Timestamp("2026-07-01 09:30") and df.index.tz is None


def test_to_frame_daily_dates_and_missing_fields():
    bars = [BarData(date=date(2026, 1, d), open=1, high=2, low=0.5, close=1.5, volume=-1, average=-1, barCount=-1)
            for d in (5, 6)]
    df = fi.to_frame(bars, "1d")
    assert list(df.index) == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
    assert list(df.columns) == ["open", "high", "low", "close"]   # -1 fields (MIDPOINT) dropped


def test_csv_written_matches_backtest_loader(tmp_path):
    import backtest_rsi as bt

    df = fi.to_frame(minute_bars(datetime(2026, 7, 1, 13, 30, tzinfo=UTC), 5), "1m")
    path = fi.csv_path("SPY", "1m", tmp_path)
    df.to_csv(path)
    loaded = bt.load_ohlc(path)
    assert len(loaded) == 5 and loaded.index[0] == pd.Timestamp("2026-07-01 09:30")


@pytest.mark.parametrize("period,size,chunk", [
    ("1m", "1 min", "1 M"), ("5m", "5 mins", "6 M"), ("1h", "1 hour", "1 Y"),
    ("1d", "1 day", "10 Y"), ("1mo", "1 month", "10 Y"), ("30s", "30 secs", "1 D"), ("1s", "1 secs", "1800 S"),
])
def test_bar_size_and_chunk(period, size, chunk):
    assert fi.bar_size(period) == size and fi.default_chunk(period) == chunk


def test_unknown_period():
    with pytest.raises(ValueError):
        fi.bar_size("7m")


def test_duration_for_span():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert fi.duration_for_span(t, t + timedelta(days=10)) == "10 D"
    assert fi.duration_for_span(t, t + timedelta(days=366)) == "2 Y"


def test_make_contract_variants():
    stk = fi.make_contract("spy:arca")
    assert (stk.secType, stk.symbol, stk.exchange, stk.primaryExchange) == ("STK", "SPY", "SMART", "ARCA")
    fut = fi.make_contract("ES:CME:202612", "FUT")
    assert (fut.secType, fut.lastTradeDateOrContractMonth, fut.exchange, fut.includeExpired) == \
        ("FUT", "202612", "CME", True)
    assert fi.make_contract("ES:CME", "CONTFUT").secType == "CONTFUT"
    fx = fi.make_contract("EURUSD", "CASH")
    assert (fx.secType, fx.symbol, fx.currency) == ("CASH", "EUR", "USD")
    with pytest.raises(ValueError):
        fi.make_contract("ES", "FUT")


def test_single_request_rules_and_names(tmp_path):
    assert fi.needs_single_request("CONTFUT", "TRADES")
    assert fi.needs_single_request("STK", "adjusted_last")
    assert not fi.needs_single_request("STK", "TRADES")
    assert fi.csv_path("ES:CME:202612", "5m", tmp_path).name == "ES_CME_202612_5m.csv"
