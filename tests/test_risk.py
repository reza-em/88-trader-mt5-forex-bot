from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from mock_broker import MockBroker
from models import AccountInfo, Position
from risk import LIVE_CONFIRM_PHRASE, RiskManager, lot_size, round_down_to_step

NOW = datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)


def acct(equity=10000.0, mode="demo", free=9000.0):
    return AccountInfo(1, "S", "USD", equity, equity, 0, free, 500, mode)


def si_eurusd(b=None):
    b = b or MockBroker()
    from history import synthetic_ohlc
    b.load_data("EURUSD", synthetic_ohlc("EURUSD", bars=100), "H1"); b.connect(); b.set_cursor(10)
    return b.symbol_info("EURUSD")


def pos(sym="EURUSD", magic=880088):
    return Position(1, sym, "buy", 0.1, 1.1, 1.09, 1.12, 0.0, NOW, magic)


# ---------------------------------------------------------------- sizing
def test_lot_size_eurusd():
    si = si_eurusd()
    # 0.5% of 10000 = 50 USD; SL 50 pips = 0.0050 -> 50 USD per lot*... 1 lot loses 500 USD -> 0.10 lot
    assert lot_size(10000, 0.5, 0.0050, si) == pytest.approx(0.10)


def test_lot_size_rounds_down_and_min_max():
    si = si_eurusd()
    v = lot_size(10000, 0.5, 0.0037, si)   # raw 0.135 -> 0.13
    assert v == pytest.approx(0.13)
    assert lot_size(10000, 0.5, 0.5, si) == 0.0           # would need < min lot: refuse, never round up
    assert lot_size(100000000, 2.0, 0.0001, si) == si.volume_max


def test_lot_size_hard_cap_and_invalid():
    si = si_eurusd()
    capped = lot_size(10000, 50.0, 0.0050, si)    # asks for 50% -> capped to 2%
    assert capped == pytest.approx(0.40)
    assert lot_size(0, 1, 0.005, si) == 0.0
    assert lot_size(10000, 1, 0, si) == 0.0


def test_lot_size_usdjpy_conversion():
    b = MockBroker()
    from history import synthetic_ohlc
    b.load_data("USDJPY", synthetic_ohlc("USDJPY", bars=100), "H1"); b.connect(); b.set_cursor(5)
    si = b.symbol_info("USDJPY")
    px = si.bid
    v = lot_size(10000, 0.5, 0.50, si)       # 50 JPY SL distance... 0.5 JPY = 50 pips
    loss = 0.5 * 100000 / px * v             # USD loss at SL
    assert loss <= 50.0 + 1e-6 and loss > 45


def test_round_down_step():
    assert round_down_to_step(0.139, 0.01) == 0.13
    assert round_down_to_step(0.14, 0.01) == 0.14


# ---------------------------------------------------------------- account mode
def test_demo_allowed(cfg, state):
    assert RiskManager(cfg, state).check_account_mode(acct(mode="demo"))
    assert RiskManager(cfg, state).check_account_mode(acct(mode="contest"))


def test_refuse_live_by_default(cfg, state):
    d = RiskManager(cfg, state).check_account_mode(acct(mode="real"))
    assert not d and d.code == "live_refused"


def test_refuse_live_even_with_confirmation_if_allow_live_false(cfg, state):
    rm = RiskManager(cfg, state)
    assert not rm.confirm_live(acct(mode="real"), LIVE_CONFIRM_PHRASE)
    state.set("live_confirmed", True)    # even if flag were somehow set
    assert not rm.check_account_mode(acct(mode="real"))


def test_live_needs_allow_live_and_phrase(cfg, state):
    cfg._base["risk"]["allow_live"] = True; cfg._rebuild()
    rm = RiskManager(cfg, state)
    d = rm.check_account_mode(acct(mode="real"))
    assert not d and d.code == "live_unconfirmed"
    assert not rm.confirm_live(acct(mode="real"), "yes please")
    assert rm.confirm_live(acct(mode="real"), LIVE_CONFIRM_PHRASE)
    assert rm.check_account_mode(acct(mode="real"))
    assert not rm.check_account_mode(acct(mode="unknown"))


def test_allow_live_must_be_literal_true(tmp_path):
    for v in ("true", "yes", 1, "True"):
        c = Config(data={"risk": {"allow_live": v}}, base_dir=tmp_path)
        assert c.get("risk.allow_live") is False


def test_telegram_cannot_enable_allow_live(cfg):
    with pytest.raises(ValueError):
        cfg.set_override("risk.allow_live", "true")
    with pytest.raises(ValueError):
        cfg.set_override("risk.max_total_drawdown_pct", "50")


# ---------------------------------------------------------------- halts
def test_daily_loss_halt_and_next_day_resume(cfg, state):
    rm = RiskManager(cfg, state)
    assert rm.update(acct(10000), NOW) == []
    assert rm.halted(NOW)
    assert rm.update(acct(9800), NOW) == []            # -2%: still fine
    ev = rm.update(acct(9690), NOW)                   # -3.1%
    assert ev == ["daily_loss"]
    assert not rm.halted(NOW) and rm.halted(NOW).code == "halt_daily"
    assert rm.update(acct(9600), NOW + timedelta(hours=2)) == []   # no duplicate event
    nxt = NOW + timedelta(days=1)
    rm.update(acct(9650), nxt)
    assert rm.halted(nxt)                              # resumes next day, new day baseline


def test_drawdown_halt_persists_until_manual_reset(cfg, state):
    rm = RiskManager(cfg, state)
    t = NOW
    rm.update(acct(10000), t)
    for i, eq in enumerate([9800, 9600, 9400, 9200, 9100]):   # slow bleed across days: no daily halt
        t = NOW + timedelta(days=i + 1)
        rm.update(acct(eq), t)
    assert rm.halted(t)
    ev = rm.update(acct(8990), t + timedelta(days=1))
    assert "drawdown" in ev
    later = t + timedelta(days=30)
    assert not rm.halted(later) and rm.halted(later).code == "halt_dd"
    # survives restart
    from state import State
    st2 = State(state.path)
    assert not RiskManager(cfg, st2).halted(later)
    # manual reset
    st2.update(halt_drawdown=False, peak_equity=None)
    assert RiskManager(cfg, st2).halted(later)


def test_hard_caps_clamp_yaml(tmp_path):
    c = Config(data={"risk": {"risk_per_trade_pct": 10, "max_daily_loss_pct": 50, "max_open_positions": 50,
                               "max_total_drawdown_pct": 90}}, base_dir=tmp_path)
    assert c.get("risk.risk_per_trade_pct") == 2.0
    assert c.get("risk.max_daily_loss_pct") == 5.0
    assert c.get("risk.max_open_positions") == 5
    assert c.get("risk.max_total_drawdown_pct") == 20.0


# ---------------------------------------------------------------- gate
def test_can_open_rules(cfg, state):
    rm = RiskManager(cfg, state)
    si = si_eurusd()
    rm.update(acct(), NOW)
    assert rm.can_open("EURUSD", "buy", acct(), [], si, NOW)
    d = rm.can_open("EURUSD", "buy", acct(), [pos("EURUSD")], si, NOW)
    assert not d and d.code == "one_per_symbol"
    three = [pos("GBPUSD"), pos("USDJPY"), pos("AUDUSD")]
    d = rm.can_open("EURUSD", "buy", acct(), three, si, NOW)
    assert not d and d.code == "max_positions"
    # other bots' positions (different magic) do not count
    assert rm.can_open("EURUSD", "buy", acct(), [pos("EURUSD", magic=1)] * 1, si, NOW)
    wide = si_eurusd(); wide.spread_points = 99
    d = rm.can_open("EURUSD", "buy", acct(), [], wide, NOW)
    assert not d and d.code == "spread"
    assert not rm.can_open("EURUSD", "buy", acct(mode="real"), [], si, NOW)


def test_rate_limit(cfg, state):
    rm = RiskManager(cfg, state)
    si = si_eurusd()
    rm.record_order(NOW)
    d = rm.can_open("EURUSD", "buy", acct(), [], si, NOW + timedelta(seconds=1))
    assert d.code == "rate_gap"
    for i in range(6):
        rm.record_order(NOW + timedelta(minutes=i * 5))
    d = rm.can_open("EURUSD", "buy", acct(), [], si, NOW + timedelta(minutes=40))
    assert d.code == "rate_hour"


def test_validate_order_mandatory_sl(cfg, state):
    rm = RiskManager(cfg, state)
    si = si_eurusd()
    assert not rm.validate_order("buy", 1.1, 0, 1.12, si)
    assert not rm.validate_order("buy", 1.1, 1.11, 1.12, si)     # SL above entry
    assert rm.validate_order("buy", 1.1, 1.09, 1.12, si)
    assert rm.validate_order("sell", 1.1, 1.11, 1.08, si)


def test_news_pause_hook(cfg, state, tmp_path):
    from news import NewsFilter
    f = tmp_path / "n.csv"
    f.write_text("datetime_utc,currency,impact,title\n2026-03-04 10:15,USD,high,NFP\n2026-03-04 10:15,EUR,low,x\n")
    si = si_eurusd()
    off = RiskManager(cfg, state, NewsFilter(False, f))
    assert off.can_open("EURUSD", "buy", acct(), [], si, NOW)          # disabled by default
    on = RiskManager(cfg, state, NewsFilter(True, f, 30, 30))
    d = on.can_open("EURUSD", "buy", acct(), [], si, NOW)
    assert not d and d.code == "news"
    assert on.can_open("EURUSD", "buy", acct(), [], si, NOW + timedelta(hours=2))
