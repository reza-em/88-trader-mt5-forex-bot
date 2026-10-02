"""Exercises MT5Broker against a FAKE `MetaTrader5` module (API-shape check only).
This does NOT prove it works with a real terminal - that can only be tested on Windows (check_setup.py)."""
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from broker import MT5Broker
from models import BrokerError


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 1, 5, 15, 30, 16385, 16388, 16408
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
    SYMBOL_TRADE_MODE_FULL = 4
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED = 10009, 10008
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_REASON_CLIENT, DEAL_REASON_MOBILE, DEAL_REASON_WEB, DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 0, 1, 2, 3, 4, 5, 6

    def __init__(self, mode=0):
        self.mode = mode
        self.ok = True
        self.positions = []
        self.sent = []
        self.now = time.time() + 7200   # server clock = UTC+2

    def initialize(self, **kw): return self.ok
    def shutdown(self): pass
    def last_error(self): return (1, "Success")
    def terminal_info(self): return NS(connected=True, trade_allowed=True)
    def account_info(self):
        return NS(login=1, server="Alpari-Demo", currency="USD", balance=10000.0, equity=10010.0, margin=0.0,
                  margin_free=10010.0, leverage=500, trade_mode=self.mode, name="T")
    def symbol_select(self, s, v): return True
    def symbol_info(self, s):
        return NS(digits=5, point=1e-5, trade_tick_size=1e-5, trade_tick_value_loss=1.0, trade_tick_value=1.0, volume_min=0.01,
                  volume_max=100.0, volume_step=0.01, trade_contract_size=100000.0, trade_stops_level=10,
                  trade_mode=4, filling_mode=2)
    def symbol_info_tick(self, s): return NS(bid=1.1, ask=1.10012, time=int(self.now))
    def copy_rates_from_pos(self, s, tf, pos, n):
        t = int(self.now) // 3600 * 3600 - np.arange(n)[::-1] * 3600
        return np.array([(x, 1.1, 1.11, 1.09, 1.1, 10) for x in t],
                        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"), ("tick_volume", "i8")])
    def positions_get(self, **kw):
        if "ticket" in kw:
            return [p for p in self.positions if p.ticket == kw["ticket"]]
        if "symbol" in kw:
            return [p for p in self.positions if p.symbol == kw["symbol"]]
        return list(self.positions)
    def order_check(self, req): return NS(retcode=0, comment="ok")
    def order_send(self, req):
        self.sent.append(req)
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            self.positions.append(NS(ticket=555, symbol=req["symbol"], type=req["type"], volume=req["volume"], price_open=req["price"],
                                     sl=req["sl"], tp=req["tp"], profit=0.0, swap=0.0, time=int(self.now), magic=req["magic"], comment=""))
        elif "position" in req and req["action"] == self.TRADE_ACTION_DEAL:
            self.positions = [p for p in self.positions if p.ticket != req["position"]]
        return NS(retcode=10009, order=555, price=req.get("price", 0), volume=req.get("volume", 0), comment="done")
    def history_deals_get(self, a, b):
        t = int(self.now)
        return [NS(position_id=9, entry=0, time=t - 600, symbol="EURUSD", type=0, volume=0.1, price=1.1, profit=0.0, swap=0.0, commission=-0.7, fee=0.0, magic=880088, reason=3),
                NS(position_id=9, entry=1, time=t - 60, symbol="EURUSD", type=1, volume=0.1, price=1.098, profit=-20.0, swap=0.0, commission=0.0, fee=0.0, magic=880088, reason=4)]


def mk(mode=0):
    f = FakeMT5(mode)
    b = MT5Broker(1, "pw", "Alpari-Demo", mt5_module=f)
    b.connect()
    return b, f


def test_account_mode_mapping():
    for m, name in [(0, "demo"), (1, "contest"), (2, "real")]:
        b, _ = mk(m)
        assert b.account_info().trade_mode == name


def test_rates_timezone_offset_detected():
    b, f = mk()
    df = b.get_rates("EURUSD", "H1", 50)
    assert len(df) == 50 and b.server_offset() == 7200
    assert abs(df.index[-1].timestamp() - time.time()) < 3700          # converted to real UTC


def test_symbol_info_and_positions():
    b, f = mk()
    si = b.symbol_info("EURUSD")
    assert si.spread_points == pytest.approx(12.0, abs=0.5) and si.trade_allowed
    r = b.place_order("EURUSD", "buy", 0.1, 1.095, 1.11, deviation=20, magic=880088)
    assert r.ok and r.ticket == 555
    req = f.sent[0]
    assert req["sl"] == 1.095 and req["magic"] == 880088 and req["deviation"] == 20
    ps = b.get_positions(880088)
    assert len(ps) == 1 and ps[0].side == "buy" and b.get_positions(1) == []
    assert b.close_position(555).ok and b.get_positions() == []


def test_refuses_order_without_sl():
    b, f = mk()
    r = b.place_order("EURUSD", "buy", 0.1, 0, 1.11)
    assert not r.ok and f.sent == []


def test_closed_trades_from_history():
    b, f = mk()
    ct = b.closed_trades(b.now() - __import__("datetime").timedelta(hours=1), 880088)
    assert len(ct) == 1 and ct[0].reason == "sl" and ct[0].profit == pytest.approx(-20.7) and ct[0].side == "buy"


def test_connect_failure_raises():
    f = FakeMT5(); f.ok = False
    b = MT5Broker(1, "pw", "S", mt5_module=f)
    with pytest.raises(BrokerError):
        b.connect()


def test_check_setup_with_mock(capsys):
    import check_setup
    from history import synthetic_ohlc
    from mock_broker import MockBroker
    from config import Config
    mb = MockBroker()
    for s in ("EURUSD", "GBPUSD", "USDJPY"):
        mb.load_data(s, synthetic_ohlc(s, bars=800), "H1")
    mb.set_cursor(600)
    ok = check_setup.run(broker=mb)
    out = capsys.readouterr().out
    assert "TRADER_TELEGRAM_BOT_TOKEN" in out and "[FAIL]" in out and ok is False   # no token/pw/login in test env
    assert "Account type: DEMO" in out
