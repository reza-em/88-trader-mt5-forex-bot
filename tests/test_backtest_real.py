"""Backtest over REAL downloaded data (cached under data/history). Skipped if data can't be obtained."""
import math
from pathlib import Path

import pytest

from backtest import Assumptions, default_cfg, metrics, run_backtest
from history import load_or_download, synthetic_ohlc

CACHE = Path(__file__).resolve().parent.parent / "data" / "history"


def real(sym):
    try:
        return load_or_download(sym, CACHE)
    except Exception as e:  # no internet in CI -> skip rather than fail
        pytest.skip(f"real data unavailable: {e}")


def test_backtest_on_real_eurusd_runs_and_is_sane():
    df = real("EURUSD")
    assert len(df) > 5000
    res = run_backtest({"EURUSD": df}, default_cfg(["EURUSD"], "H1"), Assumptions())
    m = metrics(res)
    assert m["trades"] > 20
    assert m["wins"] + m["losses"] == m["trades"]
    assert 0 <= m["win_rate"] <= 100 and 0 <= m["max_dd_pct"] < 100
    assert m["profit_factor"] >= 0 and not math.isnan(m["profit_factor"])
    # every simulated trade risked no more than ~0.5% of ~balance + slippage/spread allowance
    worst = min(t.profit for t in res["trades"])
    assert worst > -0.02 * 10000 * 1.0   # never anywhere near the 2% hard cap x 1
    # no overlapping positions on one symbol (one position per symbol)
    tr = sorted(res["trades"], key=lambda t: t.open_time)
    for a, b in zip(tr, tr[1:]):
        assert b.open_time >= a.close_time


def test_costs_reduce_results():
    df = real("EURUSD")
    cfgd = default_cfg(["EURUSD"], "H1")
    cheap = metrics(run_backtest({"EURUSD": df}, cfgd, Assumptions(spread_points={"EURUSD": 0.0}, slippage_points=0, sl_extra_slippage_points=0)))
    dear = metrics(run_backtest({"EURUSD": df}, cfgd, Assumptions(spread_points={"EURUSD": 24.0}, slippage_points=3)))
    assert cheap["net_pnl"] > dear["net_pnl"]


def test_no_lookahead_future_data_cannot_change_past_trades():
    df = real("EURUSD")
    cfgd = default_cfg(["EURUSD"], "H1")
    cut = len(df) - 1500
    a = run_backtest({"EURUSD": df.iloc[:cut]}, cfgd, Assumptions())
    b = run_backtest({"EURUSD": df}, cfgd, Assumptions())
    ta = [(t.open_time, t.side, round(t.open_price, 5)) for t in a["trades"][:-2]]
    tb = {(t.open_time, t.side, round(t.open_price, 5)) for t in b["trades"]}
    assert ta and all(x in tb for x in ta)


def test_synthetic_pipeline_runs_offline():
    df = synthetic_ohlc("EURUSD", bars=4000, seed=5)   # SYNTHETIC example data
    m = metrics(run_backtest({"EURUSD": df}, default_cfg(["EURUSD"], "H1"), Assumptions()))
    assert m["trades"] >= 0
