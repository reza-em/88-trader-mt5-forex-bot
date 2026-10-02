import numpy as np
import pandas as pd
import pytest

from indicators import atr, ema, rsi, true_range


def test_ema_constant_series():
    s = pd.Series([5.0] * 50)
    assert ema(s, 10).dropna().round(9).eq(5.0).all()


def test_ema_known_values():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    e = ema(s, 3)  # alpha = 0.5, seeded with first value
    assert e.iloc[2] == pytest.approx(2.25)   # 1 -> 1.5 -> 2.25
    assert e.iloc[3] == pytest.approx(3.125)
    assert np.isnan(e.iloc[0]) and np.isnan(e.iloc[1])  # min_periods


def test_rsi_extremes_and_range():
    up = pd.Series(np.arange(1.0, 60.0))
    assert rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(60.0, 1.0, -1.0))
    assert rsi(down, 14).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    flat = pd.Series([1.0] * 40)
    assert rsi(flat, 14).iloc[-1] == 50.0
    rnd = pd.Series(np.cumsum(np.random.default_rng(0).normal(size=300)) + 100)
    r = rsi(rnd, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_rsi_wilder_reference():
    # classic textbook series (Wilder/StockCharts) - RSI(14) of the 15th close ~ 70.46
    close = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28])
    assert rsi(close, 14).iloc[14] == pytest.approx(70.46, abs=0.1)


def test_true_range_and_atr():
    h = pd.Series([10.0, 12.0, 11.0]); l = pd.Series([9.0, 10.0, 8.0]); c = pd.Series([9.5, 11.0, 9.0])
    tr = true_range(h, l, c)
    assert list(tr) == [1.0, 2.5, 3.0]   # bar2: |12-9.5|=2.5 ; bar3: |8-11|=3
    a = atr(h, l, c, 2)                   # Wilder: seed = mean(1, 2.5) = 1.75 ; next = (1.75 + 3) / 2
    assert a.iloc[1] == pytest.approx(1.75)
    assert a.iloc[2] == pytest.approx(2.375)


def test_bad_period():
    with pytest.raises(ValueError):
        ema(pd.Series([1.0]), 0)
