"""Historical data helpers for backtests/tests.

  * download_yahoo(): REAL hourly data from Yahoo Finance via `yfinance` (max ~730 days of H1).
  * synthetic_ohlc(): SYNTHETIC random-walk data. It is EXAMPLE DATA ONLY - it is not real prices and
    results on it mean nothing. It exists so unit tests run offline and deterministically.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("88trader.history")

YAHOO = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X", "USDCHF": "USDCHF=X",
         "USDCAD": "USDCAD=X", "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X",
         "XAUUSD": "GC=F"}  # GC=F is COMEX gold FUTURES, only a proxy for spot XAUUSD
PROXY_NOTE = {"XAUUSD": "Yahoo 'GC=F' = gold futures (proxy, not Alpari spot XAUUSD)"}

SYNTHETIC_LABEL = "SYNTHETIC EXAMPLE DATA (random walk, NOT real prices)"


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)
    df = df[["open", "high", "low", "close"]].astype(float).dropna()
    df = df[(df["high"] >= df["low"]) & (df["close"] > 0)]
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def download_yahoo(symbol: str, period: str = "730d", interval: str = "1h") -> pd.DataFrame:
    import yfinance as yf  # imported lazily (dev/backtest only)
    t = YAHOO[symbol]
    raw = yf.download(t, period=period, interval=interval, progress=False, auto_adjust=False, threads=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"no data returned for {t}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return clean(raw)


def load_or_download(symbol: str, cache_dir: str | Path = "data/history", refresh: bool = False) -> pd.DataFrame:
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"{symbol}_H1_yahoo.csv"
    if f.exists() and not refresh:
        return clean(pd.read_csv(f, index_col=0, parse_dates=True))
    df = download_yahoo(symbol)
    df.to_csv(f)
    return df


def synthetic_ohlc(symbol: str = "EURUSD", bars: int = 6000, seed: int = 1, start: str = "2024-01-01",
                   drift: float = 0.0, vol: Optional[float] = None, trendiness: float = 0.02) -> pd.DataFrame:
    """SYNTHETIC hourly bars (Mon-Fri). Example data only. `trendiness` adds slow regime drift."""
    rng = np.random.default_rng(seed)
    base = {"EURUSD": 1.10, "GBPUSD": 1.27, "USDJPY": 140.0, "XAUUSD": 2000.0}.get(symbol, 1.0)
    vol = vol if vol is not None else base * 0.0004
    idx = pd.date_range(start, periods=int(bars * 7 / 5) + 48, freq="1h", tz="UTC")
    idx = idx[idx.weekday < 5][:bars]
    regime = np.zeros(bars)
    r = 0.0
    for i in range(bars):
        r = 0.98 * r + rng.normal(0, trendiness * vol)
        regime[i] = r
    steps = rng.normal(drift, vol, bars) + regime
    close = base + np.cumsum(steps)
    close = np.maximum(close, base * 0.2)
    open_ = np.concatenate([[base], close[:-1]])
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, vol * 0.5, bars))
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.5, bars))
    df = pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close}, index=idx)
    df.index.name = "time"
    return df
