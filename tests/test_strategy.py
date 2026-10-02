from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from config import Config
from strategy import compute_features, evaluate, in_session, min_bars, signal_at

P = Config().get("strategy")
P = dict(P)


def make(prices, hl=0.0006):
    idx = pd.date_range("2025-01-06", periods=len(prices), freq="1h", tz="UTC")
    c = pd.Series(prices, index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + hl, "low": np.minimum(o, c) - hl, "close": c})


def uptrend_with_pullback():
    up = np.linspace(1.00, 1.20, 320)
    pull = up[-1] - np.linspace(0, 0.012, 14)        # sharp pullback (RSI drops below 40)
    rec = pull[-1] + np.linspace(0.0005, 0.004, 4)    # bounce
    return np.concatenate([up, pull, rec])


def test_buy_signal_found_in_uptrend_pullback():
    df = make(uptrend_with_pullback())
    f = compute_features(df, P)
    sigs = [(i, signal_at(f, i, P, 0.00001)) for i in range(250, len(f))]
    buys = [s for i, s in sigs if s and s.side == "buy"]
    assert buys, "expected a pullback buy"
    s = buys[0]
    assert s.sl_distance == pytest.approx(P["atr_sl_mult"] * s.atr)
    assert s.tp_distance == pytest.approx(2.0 * s.sl_distance)   # 1:2 RR
    assert not any(s and s.side == "sell" for _, s in sigs)


def test_sell_signal_mirrored_downtrend():
    p = uptrend_with_pullback()
    df = make(2.2 - p)    # mirror
    f = compute_features(df, P)
    sells = [signal_at(f, i, P, 0.00001) for i in range(250, len(f))]
    assert any(s and s.side == "sell" for s in sells)
    assert not any(s and s.side == "buy" for s in sells)


def test_no_signal_in_flat_market():
    rng = np.random.default_rng(1)
    prices = 1.1 + 0.0002 * np.sin(np.arange(600) / 5) + rng.normal(0, 0.00005, 600)
    f = compute_features(make(prices), P)
    assert all(signal_at(f, i, P, 0.00001) is None for i in range(250, 600))


def test_min_atr_filter():
    df = make(uptrend_with_pullback())
    f = compute_features(df, P)
    p2 = dict(P, min_atr_points=100000)
    assert all(signal_at(f, i, p2, 0.00001) is None for i in range(250, len(f)))


def test_evaluate_ignores_forming_bar_and_needs_history():
    df = make(uptrend_with_pullback())
    assert evaluate(df.iloc[:100], P, 0.00001) is None
    f = compute_features(df, P)
    idx = [i for i in range(250, len(f)) if (s := signal_at(f, i, P, 0.00001)) and s.side == "buy"][0]
    # evaluate() drops the LAST row, so feeding bars[:idx+2] must evaluate bar idx
    sig = evaluate(df.iloc[: idx + 2], P, 0.00001)
    assert sig is not None and sig.bar_time == df.index[idx]
    # ...and a crazy forming bar must not change the decision
    d2 = df.iloc[: idx + 2].copy(); d2.iloc[-1] = [9, 9, 0.1, 0.1]
    assert evaluate(d2, P, 0.00001).side == "buy"


def test_ema_cross_mode():
    p = dict(P, mode="ema_cross")
    # long uptrend, then dip and recovery => fast crosses above slow again
    up = np.linspace(1.0, 1.3, 330)
    dip = up[-1] - np.linspace(0, 0.03, 25)
    rec = dip[-1] + np.linspace(0, 0.06, 40)
    f = compute_features(make(np.concatenate([up, dip, rec])), p)
    sigs = [signal_at(f, i, p, 0.00001) for i in range(260, len(f))]
    assert any(s and s.side == "buy" for s in sigs)


def test_sessions():
    mon = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
    assert in_session(mon, ["07:00-16:00"], [0, 1, 2, 3, 4])
    assert not in_session(mon.replace(hour=16), ["07:00-16:00"], [0, 1, 2, 3, 4])   # end exclusive
    assert not in_session(mon.replace(hour=6, minute=59), ["07:00-16:00"])
    assert not in_session(datetime(2026, 3, 7, 10, 0, tzinfo=timezone.utc), ["07:00-16:00"], [0, 1, 2, 3, 4])  # Saturday
    assert in_session(mon.replace(hour=23), ["22:00-03:00"])      # wraps midnight
    assert in_session(mon.replace(hour=2), ["22:00-03:00"])
    assert not in_session(mon.replace(hour=12), ["22:00-03:00"])
    assert in_session(mon.replace(hour=12), ["07:00-08:00", "11:00-13:00"])
