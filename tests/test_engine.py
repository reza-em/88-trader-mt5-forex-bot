import logging
import logging.handlers
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from engine import Engine
from history import synthetic_ohlc
from mock_broker import MockBroker
from models import BrokerError
from risk import RiskManager
from state import State
from strategy import compute_features, signal_at
from utils import RedactFilter, retry, setup_logging


def trending_df():
    """Deterministic data guaranteed to contain a buy signal (uptrend + pullback + bounce), Mon-Fri 08:00+."""
    up = np.linspace(1.00, 1.20, 320)
    pull = up[-1] - np.linspace(0, 0.012, 14)
    rec = pull[-1] + np.linspace(0.0005, 0.004, 4)
    tail = rec[-1] + np.linspace(0.0005, 0.002, 60)
    c = np.concatenate([up, pull, rec, tail])
    idx = pd.date_range("2025-01-06 00:00", periods=len(c), freq="1h", tz="UTC")
    o = pd.Series(c, index=idx).shift(1).fillna(c[0]).to_numpy()
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.0006, "low": np.minimum(o, c) - 0.0006, "close": c}, index=idx)


def signal_bar(cfg, df):
    f = compute_features(df, cfg.get("strategy"))
    for i in range(250, len(f)):
        s = signal_at(f, i, cfg.get("strategy"), 0.00001)
        if s and s.side == "buy":
            return i
    raise AssertionError("no signal")


@pytest.fixture
def rig(cfg, state):
    df = trending_df()
    b = MockBroker(balance=10000, spread_points={"EURUSD": 10})
    b.load_data("EURUSD", df, "H1"); b.connect()
    cfg._base["trading"]["symbols"] = ["EURUSD"]
    cfg._base["trading"]["sessions_utc"] = ["00:00-23:59"]
    cfg._base["trading"]["trading_days"] = [0, 1, 2, 3, 4, 5, 6]
    cfg._base["strategy"]["min_atr_points"] = 0
    cfg._rebuild()
    msgs = []
    eng = Engine(cfg, state, b, notify=lambda k, t: msgs.append((k, t)), sleep=lambda s: None)
    i = signal_bar(cfg, df)
    b.set_cursor(i + 1)          # bar i is the last CLOSED bar; i+1 is forming
    state.set("paused", False)
    return eng, b, state, msgs, df, i


def test_opens_one_trade_with_sl_tp_and_magic(rig):
    eng, b, state, msgs, df, i = rig
    assert eng.startup()
    assert eng.cycle() == "ok:1"
    pos = b.get_positions(eng.magic)
    assert len(pos) == 1
    p = pos[0]
    assert p.sl > 0 and p.tp > 0 and p.magic == 880088
    risk_usd = abs(p.price_open - p.sl) * 100000 * p.volume
    assert risk_usd <= 50.5                                   # <= 0.5% of 10k
    assert abs(p.tp - p.price_open) == pytest.approx(2 * abs(p.price_open - p.sl), rel=0.02)
    assert any(k == "open" for k, _ in msgs)
    # same bar evaluated again -> no second order; also one position per symbol
    assert eng.cycle() == "ok:0"
    assert len(b.get_positions()) == 1


def test_paused_does_not_trade(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup(); state.set("paused", True)
    assert eng.cycle() == "paused"
    assert b.get_positions() == []


def test_refuses_real_account(rig):
    eng, b, state, msgs, df, i = rig
    b.trade_mode = "real"
    eng.startup()
    assert eng.cycle().startswith("blocked:live_refused")
    assert b.orders_sent == 0
    assert any("allow_live" in t for k, t in msgs)


def test_outside_session_and_spread_filter(rig, cfg):
    eng, b, state, msgs, df, i = rig
    eng.startup()
    b.fixed_quotes["EURUSD"] = (1.2, 1.2 + 0.0050)   # 500 points spread
    assert eng.cycle() == "ok:0" and b.orders_sent == 0
    del b.fixed_quotes["EURUSD"]
    state.set("last_bar", {})
    cfg._base["trading"]["sessions_utc"] = ["00:00-00:01"]; cfg._rebuild()
    assert eng.cycle() == "outside_session"


def test_halted_by_daily_loss_blocks_orders(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup()
    state.update(day_key=eng.broker.now().strftime("%Y-%m-%d"), day_start_equity=20000.0)   # equity 10k = -50% today
    assert eng.cycle() == "halted"
    assert b.orders_sent == 0
    assert any(k == "halt" for k, _ in msgs)


def test_restart_safe_reads_positions_by_magic(rig, cfg, state):
    eng, b, state, msgs, df, i = rig
    eng.startup(); eng.cycle()
    b.place_order("EURUSD", "sell", 0.01, 1.3, 1.0, magic=999)       # foreign position
    eng2 = Engine(cfg, State(state.path), b, sleep=lambda s: None)
    eng2.startup()
    known = eng2.state.get("known_positions")
    assert len(known) == 1                                            # only ours
    assert eng2.cycle() == "ok:0"                                     # does not open a duplicate for same symbol


def test_closed_trade_notification_and_csv(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup(); eng.cycle()
    p = b.get_positions()[0]
    b.set_cursor(i + 2)
    b.fixed_quotes = {}
    # force a stop-out on the next bar
    b._data["EURUSD"].iloc[i + 2, b._data["EURUSD"].columns.get_loc("low")] = p.sl - 0.01
    b.process_bar("EURUSD")
    eng.cycle()
    closes = [t for k, t in msgs if k == "close"]
    assert closes and "SL" in closes[0]
    rows = eng.read_trade_log()
    assert len(rows) == 1 and rows[0]["reason"] == "sl" and float(rows[0]["profit"]) < 0
    assert eng.state.get("known_positions") == {}
    rep = eng.report(datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert rep["trades"] == 1 and rep["wins"] == 0


def test_close_all_only_own_positions(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup(); eng.cycle()
    b.place_order("EURUSD", "buy", 0.01, 1.0, 1.5, magic=1)
    ok, bad = eng.close_all()
    assert (ok, bad) == (1, 0)
    assert [p.magic for p in b.get_positions()] == [1]


def test_reconnect_after_disconnect(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup()
    b.disconnect()
    b.connect_failures = 2           # first two reconnects fail, backoff retries succeed
    assert eng.cycle() == "ok:1"
    assert b.is_connected()


def test_reconnect_total_failure_is_reported_not_crashing(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup(); b.disconnect(); b.connect_failures = 99
    assert eng.cycle() == "disconnected"
    assert any(k == "error" for k, _ in msgs)


def test_transient_broker_errors_retried(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup()
    b.fail_calls = 2
    assert eng.cycle() == "ok:1"


def test_no_martingale_no_averaging(rig):
    eng, b, state, msgs, df, i = rig
    eng.startup(); eng.cycle()
    for _ in range(3):
        state.set("last_bar", {})
        eng.cycle()
    assert len(b.get_positions()) == 1


# ---------------------------------------------------------------- utils
def test_retry_backoff_delays():
    calls, sleeps = [], []

    def f():
        calls.append(1)
        if len(calls) < 4:
            raise BrokerError("x")
        return "ok"
    assert retry(f, attempts=4, base_delay=2, sleep=sleeps.append) == "ok"
    assert sleeps == [2, 4, 8]
    with pytest.raises(BrokerError):
        retry(lambda: (_ for _ in ()).throw(BrokerError("y")), attempts=2, sleep=lambda s: None)


def test_log_redaction_and_rotation(tmp_path, monkeypatch):
    tok = "123456789:" + "A" * 35
    monkeypatch.setenv("TRADER_TELEGRAM_BOT_TOKEN", tok)
    monkeypatch.setenv("MT5_PASSWORD", "hunter2pass")
    f = tmp_path / "logs" / "x.log"
    setup_logging(f, console=False)
    lg = logging.getLogger("88trader.test")
    lg.error("url https://api.telegram.org/bot%s/getMe pw=%s", tok, "hunter2pass")
    for h in logging.getLogger().handlers:
        h.flush()
    txt = f.read_text()
    assert tok not in txt and "hunter2pass" not in txt and "***" in txt
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers)
