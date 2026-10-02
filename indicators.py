"""Indicators implemented with pandas/numpy only (no TA library needed)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (standard, adjust=False, seeded with first value)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder(values: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: seeded with the simple average of the first `period` valid values,
    then avg = (prev*(period-1) + x) / period."""
    v = values.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) >= period:
        start = valid[0] + period - 1
        out[start] = v[valid[0]:start + 1].mean()
        for i in range(start + 1, len(v)):
            out[i] = (out[i - 1] * (period - 1) + v[i]) / period
    return pd.Series(out, index=values.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Returns 100 when there are no losses, 0 when no gains."""
    if period < 1:
        raise ValueError("period must be >= 1")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder(gain, period)
    avg_loss = wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(~((avg_loss == 0.0) & (avg_gain > 0.0)), 100.0)
    out = out.where(~((avg_loss == 0.0) & (avg_gain == 0.0)), 50.0)
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    # first bar has no previous close: fall back to high-low
    tr.iloc[0] = (high.iloc[0] - low.iloc[0]) if len(tr) else np.nan
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_range(high, low, close)
    return wilder(tr, period)
