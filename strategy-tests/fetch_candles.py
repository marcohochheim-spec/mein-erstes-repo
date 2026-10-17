"""Download historical candles from tastytrade (DXLink) into data/<SYMBOL>_<period>.csv.

Examples:
    python fetch_candles.py AAPL MSFT SPY --period 1d --years 10
    python fetch_candles.py SPY --period 1h --from 2025-01-01
    python fetch_candles.py SPY QQQ --period 1m --years 1      # intraday, regular trading hours only

Intraday timestamps are stored in US exchange time (America/New_York); daily ones as dates.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from dxlink import fetch_candles
from tasty_auth import get_quote_token

DATA_DIR = Path(__file__).parent / "data"
EXCHANGE_TZ = "America/New_York"


def is_intraday(period: str) -> bool:
    return period.endswith(("s", "m", "h")) and not period.endswith("mo")


def stream_symbol(symbol: str, period: str, all_hours: bool) -> str:
    """DXLink candle symbol; tho=true restricts intraday candles to regular trading hours."""
    attrs = f"={period}" + (",tho=true" if is_intraday(period) and not all_hours else "")
    return f"{symbol}{{{attrs}}}"


def csv_path(symbol: str, period: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol).strip("_")
    return DATA_DIR / f"{safe}_{period}.csv"


def repair_ohlc(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Fix closes that fall outside the bar's own low-high range.

    Seen in tastytrade daily data: on the last bar before a split the close is left unadjusted
    (e.g. AAPL 2020-08-28: 499.23 inside a 124.58-126.44 bar), and around ex-dividend dates the close
    sits slightly outside the range. A close that fits the range after dividing by a whole split
    ratio is rescaled; any remaining excursion is clamped to the range.
    """
    df = df.copy()
    notes = []
    for ts in df.index[(df["close"] > df["high"]) | (df["close"] < df["low"])]:
        lo, hi, c = df.at[ts, "low"], df.at[ts, "high"], df.at[ts, "close"]
        ratio = c / ((lo + hi) / 2)
        factor = round(ratio) if ratio >= 1.5 else (1 / round(1 / ratio) if ratio <= 1 / 1.5 else 1)
        fixed = c / factor if factor != 1 else c
        if factor != 1 and lo * 0.98 <= fixed <= hi * 1.02:
            how = f"Split-Faktor {factor:g}" if factor > 1 else f"Split-Faktor 1/{round(1 / factor)}"
        else:
            fixed, how = min(max(c, lo), hi), "auf Tagesspanne begrenzt"
        fixed = min(max(fixed, lo), hi)
        df.at[ts, "close"] = fixed
        notes.append(f"{ts:%Y-%m-%d %H:%M} close {c:.4g} -> {fixed:.4g} ({how})")
    return df, notes


def to_frame(raw: list[dict], period: str) -> pd.DataFrame:
    df = pd.DataFrame(raw)
    if df.empty:
        return df
    for col in ("open", "high", "low", "close", "volume", "vwap", "impVolatility", "openInterest"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("time").drop_duplicates("time", keep="last")
    if is_intraday(period):
        # US exchange time without offset, so sessions (09:30-16:00) are plain clock times.
        df["time"] = df["time"].dt.tz_convert(EXCHANGE_TZ).dt.tz_localize(None)
    else:
        df["time"] = df["time"].dt.tz_convert(None).dt.normalize()
    out = df.rename(columns={"impVolatility": "iv", "openInterest": "oi"})
    cols = [c for c in ("time", "open", "high", "low", "close", "volume", "vwap", "iv", "oi") if c in out]
    return out[cols].set_index("time")


def merge_archive(path: Path, new: pd.DataFrame) -> tuple[pd.DataFrame, int, str | None]:
    """Merge freshly fetched candles into an existing CSV archive.

    Overlapping timestamps take the new values (a bar fetched mid-session is replaced once it is
    complete). Returns (merged, rows added, warning if the new block does not overlap the archive).
    """
    if not path.exists():
        return new, len(new), None
    old = pd.read_csv(path, parse_dates=["time"], index_col="time")
    merged = pd.concat([old, new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    warning = None
    if len(old) and len(new) and new.index[0] > old.index[-1]:
        warning = (f"Lücke möglich: Archiv endet {old.index[-1]:%Y-%m-%d %H:%M}, neue Daten beginnen "
                   f"{new.index[0]:%Y-%m-%d %H:%M} – der Abruf lag zu lange zurück.")
    return merged, len(merged) - len(old), warning


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="+", help="Streamer-Symbole, z. B. AAPL SPY /ESZ26:XCME")
    p.add_argument("--period", default="1d", help="Kerzenperiode: 1m, 5m, 1h, 1d, 1w ... (Standard: 1d)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--years", type=float, default=10, help="Historie in Jahren (Standard: 10)")
    group.add_argument("--from", dest="from_date", help="Startdatum YYYY-MM-DD")
    p.add_argument("--all-hours", action="store_true",
                   help="Intraday inkl. Pre-/After-Market (Standard: nur reguläre Handelszeit)")
    p.add_argument("--timeout", type=float, default=None,
                   help="Max. Sekunden je Symbol (Standard: 120 täglich, 900 intraday)")
    p.add_argument("--append", action="store_true",
                   help="An vorhandene CSV anhängen statt sie zu ersetzen (Archiv über DXLink-Grenzen hinaus)")
    args = p.parse_args(argv)
    timeout = args.timeout or (900 if is_intraday(args.period) else 120)

    if args.from_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=365.25 * args.years)
    from_ms = int(start.timestamp() * 1000)

    qt = get_quote_token()
    what = "neueste Kerzen fürs Archiv" if args.append else f"ab {start:%Y-%m-%d}"
    print(f"DXLink verbunden ({qt.level}), lade {what} ...")
    DATA_DIR.mkdir(exist_ok=True)

    failed = 0
    for sym in args.symbols:
        try:
            raw = fetch_candles(qt.dxlink_url, qt.token, stream_symbol(sym, args.period, args.all_hours),
                                from_ms, total_timeout=timeout)
        except Exception as exc:  # one symbol failing must not stop the others
            print(f"  {sym}: Abruf fehlgeschlagen: {exc}")
            failed += 1
            continue
        df = to_frame(raw, args.period)
        if df.empty:
            print(f"  {sym}: keine Daten erhalten")
            failed += 1
            continue
        df, notes = repair_ohlc(df)
        path = csv_path(sym, args.period)
        fetched = df
        if args.append:
            df, added, gap = merge_archive(path, fetched)
            print(f"  {sym}: {len(fetched)} Kerzen abgerufen, {added} neu -> Archiv {path.name}: {len(df)} Kerzen "
                  f"{df.index[0]:%Y-%m-%d %H:%M} .. {df.index[-1]:%Y-%m-%d %H:%M}")
            if gap:
                print(f"     WARNUNG: {gap}")
        else:
            print(f"  {sym}: {len(df)} Kerzen {df.index[0]:%Y-%m-%d %H:%M} .. {df.index[-1]:%Y-%m-%d %H:%M}"
                  f" -> {path.name}")
        df.to_csv(path)
        big = [n for n in notes if "Split" in n]
        if notes:
            print(f"     {len(notes)} Schlusskurse außerhalb der Tagesspanne korrigiert"
                  f" ({len(big)} davon Split-Fehler)")
            for n in big + [n for n in notes if "Split" not in n][:3]:
                print(f"       {n}")
        if not args.append and df.index[0] > start.replace(tzinfo=None) + timedelta(days=7):
            print(f"     Hinweis: Historie beginnt erst {df.index[0]:%Y-%m-%d} – mehr liefert DXLink für diese Periode nicht.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
