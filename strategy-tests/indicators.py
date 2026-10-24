"""Technical indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI: seed with a simple average of the first `period` changes, then smooth by 1/period."""
    values = close.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) <= period:
        return pd.Series(out, index=close.index, name=f"rsi{period}")

    delta = np.diff(values)
    gains = np.clip(delta, 0, None)
    losses = np.clip(-delta, 0, None)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return pd.Series(out, index=close.index, name=f"rsi{period}")


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
