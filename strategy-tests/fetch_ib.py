"""Download historical candles from Interactive Brokers (IB Gateway / TWS) into data/ib/<SYMBOL>_<period>.csv.

Same CSV format as fetch_candles.py (tastytrade), so backtest_rsi.py runs on it unchanged:
    python fetch_ib.py SPY QQQ --period 1m --years 2
    python backtest_rsi.py --data-dir data/ib --interval 1m --exit mid --cost-bps 1

More examples:
    python fetch_ib.py AAPL MSFT --period 1d --years 20
    python fetch_ib.py SPY --period 1d --what ADJUSTED_LAST          # split- and dividend-adjusted
    python fetch_ib.py ES:CME:202612 --sec-type FUT --period 5m       # one futures contract (also expired)
    python fetch_ib.py EURUSD --sec-type CASH --period 1h --all-hours

Connects read-only; no orders, no account data. IB Gateway must be running with the API enabled
(Configure > Settings > API). Defaults: 127.0.0.1:4001 (Gateway live), override with --host/--port
or IB_HOST / IB_PORT / IB_CLIENT_ID. Requests are paged backwards from today and paced to stay below
IB's limit of 60 historical requests per 10 minutes.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time as _time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd

from fetch_candles import EXCHANGE_TZ, is_intraday, merge_archive, repair_ohlc

DATA_DIR = Path(__file__).parent / "data" / "ib"

# Our period names (as in fetch_candles.py) -> IB barSizeSetting.
BAR_SIZES = {
    "1s": "1 secs", "5s": "5 secs", "10s": "10 secs", "15s": "15 secs", "30s": "30 secs",
    "1m": "1 min", "2m": "2 mins", "3m": "3 mins", "5m": "5 mins", "10m": "10 mins",
    "15m": "15 mins", "20m": "20 mins", "30m": "30 mins",
    "1h": "1 hour", "2h": "2 hours", "3h": "3 hours", "4h": "4 hours", "8h": "8 hours",
    "1d": "1 day", "1w": "1 week", "1mo": "1 month",
}

# Errors that mean "no (more) data" rather than a broken request.
NO_DATA_CODES = {162, 165, 366}
# Status messages without meaning for a single request (market data farm connected etc.).
INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158}


def bar_size(period: str) -> str:
    try:
        return BAR_SIZES[period]
    except KeyError:
        raise ValueError(f"Unbekannte Periode {period!r}, erlaubt: {', '.join(BAR_SIZES)}") from None


def default_chunk(period: str) -> str:
    """Duration per request. IB accepts more, but a few thousand bars per answer keeps requests quick."""
    if period == "1s":
        return "1800 S"
    if period.endswith("s"):
        return "1 D"
    if period in ("1m", "2m", "3m"):
        return "1 M"
    if period.endswith("m") and not period.endswith("mo"):
        return "6 M"
    if period.endswith("h"):
        return "1 Y"
    return "10 Y"


def duration_for_span(start: datetime, end: datetime) -> str:
    """Single-request duration covering [start, end], for requests IB only allows to end 'now'."""
    days = max(1, math.ceil((end - start).total_seconds() / 86400))
    return f"{math.ceil(days / 365)} Y" if days > 365 else f"{days} D"


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def make_contract(spec: str, sec_type: str = "STK", currency: str = "USD"):
    """'SPY' / 'SPY:ARCA' (stock), 'ES:CME:202612' (future), 'ES:CME' (CONTFUT), 'SPX:CBOE' (index), 'EURUSD' (CASH)."""
    from ib_async import ContFuture, Forex, Future, Index, Stock

    parts = spec.split(":")
    sym = parts[0].upper()
    exchange = parts[1].upper() if len(parts) > 1 and parts[1] else ""
    sec_type = sec_type.upper()
    if sec_type == "STK":
        # SMART routing; a given exchange becomes primaryExchange to disambiguate the listing.
        return Stock(sym, "SMART", currency, primaryExchange=exchange)
    if sec_type == "FUT":
        if len(parts) < 3 or not exchange:
            raise ValueError(f"Futures brauchen SYMBOL:BÖRSE:VERFALL, z. B. ES:CME:202612 (nicht {spec!r})")
        return Future(sym, parts[2], exchange, currency=currency, includeExpired=True)
    if sec_type == "CONTFUT":
        return ContFuture(sym, exchange, currency=currency)
    if sec_type == "IND":
        return Index(sym, exchange, currency)
    if sec_type == "CASH":
        return Forex(sym)
    raise ValueError(f"Unbekannter --sec-type {sec_type!r}")


def needs_single_request(sec_type: str, what: str) -> bool:
    """IB rejects an explicit endDateTime for continuous futures and ADJUSTED_LAST."""
    return sec_type.upper() == "CONTFUT" or what.upper() == "ADJUSTED_LAST"


def safe_name(spec: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", spec.upper()).strip("_")


def csv_path(spec: str, period: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / f"{safe_name(spec)}_{period}.csv"


# ---------------------------------------------------------------------------
# Paging and pacing
# ---------------------------------------------------------------------------

class Pacer:
    """Keeps at most `max_requests` per rolling `window` seconds and `min_gap` seconds between requests.

    IB's hard limit is 60 historical requests per 10 minutes (plus: no 6 requests for the same
    contract within 2 seconds). The defaults stay a little below that.
    """

    def __init__(self, max_requests: int = 55, window: float = 600.0, min_gap: float = 1.0,
                 clock: Callable[[], float] = _time.monotonic, sleep: Callable[[float], None] = _time.sleep):
        self.max_requests, self.window, self.min_gap = max_requests, window, min_gap
        self.clock, self.sleep = clock, sleep
        self.sent: deque[float] = deque()

    def wait(self) -> float:
        """Block until the next request is allowed, record it, return the seconds waited."""
        now = self.clock()
        while self.sent and now - self.sent[0] >= self.window:
            self.sent.popleft()
        delay = 0.0
        if self.sent:
            delay = max(delay, self.sent[-1] + self.min_gap - now)
        if len(self.sent) >= self.max_requests:
            delay = max(delay, self.sent[0] + self.window - now)
        if delay > 0:
            self.sleep(delay)
            now = self.clock()
            while self.sent and now - self.sent[0] >= self.window:
                self.sent.popleft()
        self.sent.append(now)
        return max(delay, 0.0)


def bar_time(value) -> datetime:
    """IB bar date (UTC datetime for intraday with formatDate=2, plain date for daily+) -> aware UTC datetime."""
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.to_pydatetime()


@dataclass
class IBError:
    req_id: int
    code: int
    message: str


class RequestFailed(RuntimeError):
    pass


def robust_request(call: Callable[[datetime | None, str], Sequence], errors: list[IBError],
                   sleep: Callable[[float], None], retries: int = 2, pacing_pause: float = 60.0):
    """Wrap one historical request with retries.

    An empty answer is only "no more data" if IB said so (or sent another error, which the caller
    reports). Empty without any message is an ib_async timeout and is retried; a pacing violation
    waits `pacing_pause` seconds first. Raises RequestFailed when all attempts time out.
    """
    def request(end, duration):
        for attempt in range(retries + 1):
            errors.clear()
            bars = list(call(end, duration))
            if bars:
                return bars
            if any("pacing" in e.message.lower() for e in errors):
                sleep(pacing_pause)
                continue
            if errors:
                return []
            if attempt < retries:
                sleep(5.0)
        raise RequestFailed(f"keine Antwort nach {retries + 1} Versuchen (Timeout)")
    return request


def fetch_history(request: Callable[[datetime | None, str], Sequence], start: datetime, chunk: str,
                  pacer: Pacer | None = None, single: bool = False, now: datetime | None = None,
                  progress: Callable[[int, datetime], None] | None = None) -> tuple[list, str | None]:
    """Collect bars from `start` up to now by paging backwards.

    `request(end, duration)` returns IB BarData oldest first; end=None means "up to now".
    Paging stops once `start` is reached, IB returns nothing, or the oldest bar stops moving back.
    Returns (bars oldest first, None or the reason the history is cut short). A cut-short history
    is still contiguous: it covers the most recent part.
    """
    now = now or datetime.now(timezone.utc)
    if single:
        if pacer:
            pacer.wait()
        try:
            bars = list(request(None, duration_for_span(start, now)))
        except RequestFailed as exc:
            return [], str(exc)
        return [b for b in bars if bar_time(b.date) >= start], None

    chunks: list[list] = []
    end: datetime | None = None
    oldest: datetime | None = None
    problem = None
    while True:
        if pacer:
            pacer.wait()
        try:
            bars = list(request(end, chunk))
        except RequestFailed as exc:
            problem = str(exc)
            break
        if not bars:
            break
        first = bar_time(bars[0].date)
        if oldest is not None and first >= oldest:
            break  # no progress: IB has nothing older
        chunks.append(bars)
        oldest = end = first
        if progress:
            progress(sum(len(c) for c in chunks), first)
        if first <= start:
            break
    out = [b for c in reversed(chunks) for b in c]
    return [b for b in out if bar_time(b.date) >= start], problem


def to_frame(bars: Sequence, period: str) -> pd.DataFrame:
    """IB BarData -> CSV layout of fetch_candles.py: time index, open/high/low/close/volume/vwap/trades."""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame({
        "time": [bar_time(b.date) for b in bars],
        "open": [b.open for b in bars], "high": [b.high for b in bars],
        "low": [b.low for b in bars], "close": [b.close for b in bars],
        "volume": [b.volume for b in bars], "vwap": [b.average for b in bars],
        "trades": [b.barCount for b in bars],
    })
    for col in ("open", "high", "low", "close", "volume", "vwap", "trades"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # IB sends -1 where a field does not exist (e.g. volume for MIDPOINT).
    for col in ("volume", "vwap", "trades"):
        df.loc[df[col] < 0, col] = float("nan")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if is_intraday(period):
        df["time"] = df["time"].dt.tz_convert(EXCHANGE_TZ).dt.tz_localize(None)
    else:
        df["time"] = df["time"].dt.tz_convert(None).dt.normalize()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("time").drop_duplicates("time", keep="last")
    df = df.dropna(axis=1, how="all")
    return df.set_index("time")


# ---------------------------------------------------------------------------
# IB session
# ---------------------------------------------------------------------------

def connect(host: str, port: int, client_id: int, timeout: float = 10):
    from ib_async import IB
    from ib_async.ib import StartupFetch

    logging.getLogger("ib_async").setLevel(logging.CRITICAL)  # errors are reported below, per symbol
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=True, fetchFields=StartupFetch(0))
    return ib


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="+", help="z. B. SPY AAPL:NASDAQ ES:CME:202612 EURUSD")
    p.add_argument("--period", default="1d", choices=list(BAR_SIZES), metavar="PERIODE",
                   help=f"Kerzenperiode: {', '.join(BAR_SIZES)} (Standard: 1d)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--years", type=float, default=10, help="Historie in Jahren (Standard: 10)")
    group.add_argument("--from", dest="from_date", help="Startdatum YYYY-MM-DD")
    p.add_argument("--sec-type", default="STK", choices=["STK", "FUT", "CONTFUT", "IND", "CASH"],
                   help="Instrumenttyp (Standard: STK)")
    p.add_argument("--currency", default="USD")
    p.add_argument("--what", default=None,
                   help="whatToShow: TRADES (Standard, Splits bereinigt), ADJUSTED_LAST (auch Dividenden), "
                        "MIDPOINT (Standard für CASH), BID_ASK ...")
    p.add_argument("--all-hours", action="store_true",
                   help="Intraday inkl. Pre-/After-Market (Standard: nur reguläre Handelszeit)")
    p.add_argument("--chunk", help="Dauer je Abfrage, z. B. '1 W' oder '2 M' (Standard je nach Periode)")
    p.add_argument("--append", action="store_true",
                   help="An vorhandene CSV anhängen; lädt nur ab dem Ende des Archivs nach")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Zielordner (Standard: data/ib)")
    p.add_argument("--host", default=os.environ.get("IB_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("IB_PORT", 4001)),
                   help="4001 Gateway live, 4002 Gateway paper, 7496 TWS live, 7497 TWS paper")
    p.add_argument("--client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", 17)))
    p.add_argument("--timeout", type=float, default=180, help="Max. Sekunden je Abfrage (Standard: 180)")
    args = p.parse_args(argv)

    bar = bar_size(args.period)
    intraday = is_intraday(args.period)
    what = (args.what or ("MIDPOINT" if args.sec_type == "CASH" else "TRADES")).upper()
    use_rth = not args.all_hours
    single = needs_single_request(args.sec_type, what)
    chunk = args.chunk or default_chunk(args.period)
    if args.from_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=365.25 * args.years)

    try:
        ib = connect(args.host, args.port, args.client_id)
    except Exception as exc:
        print(f"Keine Verbindung zu IB unter {args.host}:{args.port} ({exc!r}).\n"
              "Läuft IB Gateway/TWS, ist die API aktiviert und der Port richtig?")
        return 2

    errors: list[IBError] = []

    def on_error(req_id, code, message, *_):
        if code not in INFO_CODES:
            errors.append(IBError(req_id, code, message))

    ib.errorEvent += on_error
    pacer = Pacer(sleep=ib.sleep)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    mode = "eine Abfrage bis jetzt" if single else f"Blöcke à {chunk}"
    print(f"IB verbunden ({args.host}:{args.port}, nur lesend), {bar} {what}"
          f"{'' if use_rth else ' inkl. Pre-/After-Market'}, {mode}")

    failed = 0
    try:
        for spec in args.symbols:
            errors.clear()
            try:
                contract = make_contract(spec, args.sec_type, args.currency)
                qualified = ib.qualifyContracts(contract)
            except ValueError as exc:
                print(f"  {spec}: {exc}")
                failed += 1
                continue
            if not qualified or qualified[0] is None or not contract.conId:
                why = "; ".join(f"{e.code} {e.message}" for e in errors) or "mehrdeutig oder unbekannt"
                print(f"  {spec}: Kontrakt nicht gefunden ({why})")
                failed += 1
                continue

            path = csv_path(spec, args.period, args.data_dir)
            sym_start = start
            if args.append and path.exists():
                last = pd.read_csv(path, usecols=["time"], parse_dates=["time"])["time"].max()
                if pd.notna(last):
                    last = last.tz_localize(EXCHANGE_TZ if intraday else "UTC", ambiguous=True,
                                            nonexistent="shift_forward").tz_convert("UTC")
                    sym_start = max(start, last.to_pydatetime() - timedelta(days=1))

            head = ib.reqHeadTimeStamp(contract, what, use_rth, formatDate=2)
            if isinstance(head, datetime):
                head = bar_time(head)
                if head > sym_start:
                    print(f"  {spec}: IB-Historie beginnt erst {head:%Y-%m-%d}")
                    sym_start = head

            def call(end, duration, contract=contract):
                return ib.reqHistoricalData(contract, end or "", duration, bar, what, use_rth,
                                            formatDate=2, timeout=args.timeout)

            def progress(n, oldest):
                print(f"\r  {spec}: {n} Kerzen, zurück bis {oldest:%Y-%m-%d} ...", end="", flush=True)

            request = robust_request(call, errors, ib.sleep)
            bars, problem = fetch_history(request, sym_start, chunk, pacer, single=single, progress=progress)
            print("\r" + " " * 70 + "\r", end="")
            df = to_frame(bars, args.period)
            if problem:
                print(f"  {spec}: WARNUNG: Abruf abgebrochen ({problem}), Historie unvollständig")
            if df.empty:
                hard = [e for e in errors if e.code not in NO_DATA_CODES]
                why = "; ".join(f"{e.code} {e.message}" for e in (hard or errors)) or "keine Daten erhalten"
                print(f"  {spec}: {why}")
                failed += 1
                continue

            df, notes = repair_ohlc(df)
            fetched = df
            if args.append:
                df, added, gap = merge_archive(path, fetched)
                print(f"  {spec}: {len(fetched)} Kerzen abgerufen, {added} neu -> {path.name}: {len(df)} Kerzen "
                      f"{df.index[0]:%Y-%m-%d %H:%M} .. {df.index[-1]:%Y-%m-%d %H:%M}")
                if gap:
                    print(f"     WARNUNG: {gap}")
            else:
                print(f"  {spec}: {len(df)} Kerzen {df.index[0]:%Y-%m-%d %H:%M} .. "
                      f"{df.index[-1]:%Y-%m-%d %H:%M} -> {path.name}")
            df.to_csv(path)
            if notes:
                print(f"     {len(notes)} Schlusskurse außerhalb der Kerzenspanne korrigiert")
    finally:
        ib.disconnect()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
