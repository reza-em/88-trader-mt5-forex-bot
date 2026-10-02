"""Conservative trend-following strategy.

rsi_pullback (default): trade only WITH the EMA trend (EMA50 vs EMA200 on the chosen timeframe,
    price on the right side of EMA200 and EMA200 sloping the same way). Enter when RSI comes back
    from a pullback (BUY: RSI crosses back up through `rsi_buy_level`; SELL: crosses back down
    through `rsi_sell_level`).
ema_cross: fast EMA crosses slow EMA, only in the direction of the EMA50/EMA200 trend.

Stop loss = atr_sl_mult x ATR. Take profit = rr x stop distance (default 1:2).
No martingale, no grid, no averaging down. One position per symbol (enforced by RiskManager).
Signals use CLOSED bars only (the still-forming last bar is ignored).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional

import pandas as pd

from indicators import atr, ema, rsi


@dataclass
class Signal:
    side: str              # 'buy' | 'sell'
    sl_distance: float     # price units
    tp_distance: float
    atr: float
    bar_time: pd.Timestamp
    reason: str


# ----------------------------------------------------------------- sessions
def _parse(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))


def in_session(now: datetime, sessions: list[str], days: Optional[list[int]] = None) -> bool:
    """`now` must be UTC. Ranges may wrap midnight (22:00-03:00). End is exclusive."""
    if days is not None and now.weekday() not in days:
        return False
    t = now.time().replace(second=0, microsecond=0)
    for rng in sessions:
        a, b = rng.split("-")
        a, b = _parse(a), _parse(b)
        if a < b:
            if a <= t < b:
                return True
        elif t >= a or t < b:
            return True
    return False


# ----------------------------------------------------------------- signals
def min_bars(params: dict) -> int:
    return int(max(params.get("ema_slow", 200), params.get("cross_slow", 50)) + params.get("ema_slope_bars", 10) + 50)


def compute_features(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Return df + indicator columns (vectorised; usable for backtests too)."""
    f = df.copy()
    f["ema_fast"] = ema(f["close"], int(params["ema_fast"]))
    f["ema_slow"] = ema(f["close"], int(params["ema_slow"]))
    f["rsi"] = rsi(f["close"], int(params["rsi_period"]))
    f["atr"] = atr(f["high"], f["low"], f["close"], int(params["atr_period"]))
    f["x_fast"] = ema(f["close"], int(params["cross_fast"]))
    f["x_slow"] = ema(f["close"], int(params["cross_slow"]))
    return f


def signal_at(f: pd.DataFrame, i: int, params: dict, point: float) -> Optional[Signal]:
    """Evaluate the signal on closed bar i (needs features from compute_features)."""
    n_slope = int(params.get("ema_slope_bars", 10))
    if i < 1 or i - n_slope < 0:
        return None
    r, p = f.iloc[i], f.iloc[i - 1]
    need = [r["ema_fast"], r["ema_slow"], r["rsi"], r["atr"], p["rsi"], f["ema_slow"].iloc[i - n_slope]]
    if any(pd.isna(x) for x in need):
        return None
    if r["atr"] < float(params.get("min_atr_points", 0)) * point:
        return None
    slope_up = r["ema_slow"] > f["ema_slow"].iloc[i - n_slope]
    slope_dn = r["ema_slow"] < f["ema_slow"].iloc[i - n_slope]
    up = r["ema_fast"] > r["ema_slow"] and r["close"] > r["ema_slow"] and slope_up
    dn = r["ema_fast"] < r["ema_slow"] and r["close"] < r["ema_slow"] and slope_dn
    side, why = None, ""
    mode = params.get("mode", "rsi_pullback")
    if mode == "rsi_pullback":
        if up and p["rsi"] < params["rsi_buy_level"] <= r["rsi"]:
            side, why = "buy", f"uptrend + RSI back above {params['rsi_buy_level']}"
        elif dn and p["rsi"] > params["rsi_sell_level"] >= r["rsi"]:
            side, why = "sell", f"downtrend + RSI back below {params['rsi_sell_level']}"
    elif mode == "ema_cross":
        pf, ps, rf, rs_ = p["x_fast"], p["x_slow"], r["x_fast"], r["x_slow"]
        if any(pd.isna(x) for x in (pf, ps, rf, rs_)):
            return None
        if up and pf <= ps and rf > rs_:
            side, why = "buy", "EMA cross up in uptrend"
        elif dn and pf >= ps and rf < rs_:
            side, why = "sell", "EMA cross down in downtrend"
    if side is None:
        return None
    sl_d = float(params["atr_sl_mult"]) * float(r["atr"])
    return Signal(side=side, sl_distance=sl_d, tp_distance=sl_d * float(params["rr"]), atr=float(r["atr"]),
                  bar_time=f.index[i], reason=why)


def evaluate(rates: pd.DataFrame, params: dict, point: float) -> Optional[Signal]:
    """Live entry point: `rates` includes the forming last bar, which is dropped here."""
    closed = rates.iloc[:-1]
    if len(closed) < min_bars(params):
        return None
    f = compute_features(closed, params)
    return signal_at(f, len(f) - 1, params, point)
