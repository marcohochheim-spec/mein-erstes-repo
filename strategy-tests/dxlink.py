"""Minimal DXLink WebSocket client for historical Candle data.

Protocol (dxFeed DXLink, as used by tastytrade):
    SETUP -> AUTH -> CHANNEL_REQUEST(FEED) -> FEED_SETUP(COMPACT) -> FEED_SUBSCRIPTION(Candle, fromTime)
The server answers with a snapshot of candles (newest first), flagged via eventFlags.
"""

from __future__ import annotations

import asyncio
import json
import math
import time

import websockets

PROTOCOL_VERSION = "0.1-DXF-JS/0.3.0"
FEED_CHANNEL = 3
KEEPALIVE_SECONDS = 30

CANDLE_FIELDS = [
    "eventType", "eventSymbol", "eventFlags", "time",
    "open", "high", "low", "close", "volume", "vwap", "impVolatility", "openInterest",
]

# dxFeed eventFlags bits
TX_PENDING = 0x01
REMOVE_EVENT = 0x02
SNAPSHOT_END = 0x08
SNAPSHOT_SNIP = 0x10


class DXLinkError(RuntimeError):
    pass


def _num(value):
    """DXLink sends NaN/Infinity as strings in COMPACT mode."""
    if value is None:
        return math.nan
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return math.nan
    return value


def _parse_compact(data, fields_by_type):
    """Yield dicts from COMPACT FEED_DATA: [type, [flat values...], type, [flat values...]]."""
    i = 0
    while i + 1 < len(data):
        event_type, flat = data[i], data[i + 1]
        i += 2
        fields = fields_by_type.get(event_type)
        if not fields or not isinstance(flat, list):
            continue
        n = len(fields)
        for j in range(0, len(flat) - n + 1, n):
            yield dict(zip(fields, flat[j:j + n]))


async def _fetch(url, token, symbol, from_ms, idle_timeout, total_timeout):
    candles: dict[int, dict] = {}
    fields_by_type = {"Candle": CANDLE_FIELDS}

    async with websockets.connect(url, max_size=None, open_timeout=30) as ws:
        async def send(msg):
            await ws.send(json.dumps(msg))

        await send({"type": "SETUP", "channel": 0, "version": PROTOCOL_VERSION,
                    "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        await send({"type": "AUTH", "channel": 0, "token": token})

        started = time.monotonic()
        last_data = None
        last_keepalive = time.monotonic()
        subscribed = False

        while True:
            now = time.monotonic()
            if now - started > total_timeout:
                break
            if last_data is not None and now - last_data > idle_timeout:
                break
            if now - last_keepalive > KEEPALIVE_SECONDS:
                await send({"type": "KEEPALIVE", "channel": 0})
                last_keepalive = now

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "ERROR":
                raise DXLinkError(f"DXLink-Fehler: {msg.get('error')} {msg.get('message')}")
            if mtype == "AUTH_STATE" and msg.get("state") == "AUTHORIZED":
                await send({"type": "CHANNEL_REQUEST", "channel": FEED_CHANNEL,
                            "service": "FEED", "parameters": {"contract": "AUTO"}})
            elif mtype == "CHANNEL_OPENED" and msg.get("channel") == FEED_CHANNEL:
                await send({"type": "FEED_SETUP", "channel": FEED_CHANNEL,
                            "acceptAggregationPeriod": 0.1, "acceptDataFormat": "COMPACT",
                            "acceptEventFields": {"Candle": CANDLE_FIELDS}})
            elif mtype == "FEED_CONFIG":
                # The server may grant a different field order; honour it.
                fields_by_type.update(msg.get("eventFields") or {})
                if not subscribed:
                    await send({"type": "FEED_SUBSCRIPTION", "channel": FEED_CHANNEL, "reset": True,
                                "add": [{"type": "Candle", "symbol": symbol, "fromTime": from_ms}]})
                    subscribed = True
            elif mtype == "FEED_DATA":
                done = False
                for ev in _parse_compact(msg.get("data", []), fields_by_type):
                    raw_flags = _num(ev.get("eventFlags"))
                    flags = 0 if math.isnan(raw_flags) else int(raw_flags)
                    t = ev.get("time")
                    if isinstance(t, (int, float)) and not math.isnan(t):
                        if flags & REMOVE_EVENT:
                            candles.pop(int(t), None)
                        else:
                            candles[int(t)] = ev
                    if flags & (SNAPSHOT_END | SNAPSHOT_SNIP):
                        done = True
                last_data = time.monotonic()
                if done:
                    # Snapshot complete; a short grace period catches trailing updates.
                    idle_timeout = min(idle_timeout, 1.5)

    return list(candles.values())


def fetch_candles(url, token, symbol, from_ms, idle_timeout=5.0, total_timeout=120.0):
    """Return raw candle dicts for e.g. symbol='AAPL{=1d}' since from_ms (epoch milliseconds)."""
    return asyncio.run(_fetch(url, token, symbol, from_ms, idle_timeout, total_timeout))
