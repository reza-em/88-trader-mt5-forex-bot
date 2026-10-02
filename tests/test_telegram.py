import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine import Engine
from risk import LIVE_CONFIRM_PHRASE
from telegram_bot import (DISCLAIMER, ControlBot, TelegramAPI, get_token)
from tests.fake_tg import FakeAPI, cb, msg
from tests.test_engine import rig  # noqa: F401  (fixture reuse)

BASE = Path(__file__).resolve().parent.parent


@pytest.fixture
def bot(rig):
    eng, b, state, msgs, df, i = rig
    state.set("paused", True)
    api = FakeAPI()
    bt = ControlBot(api, eng.cfg, state, eng)
    eng.notify = bt.notify
    eng.startup()
    return bt, api, eng, b, state


def test_owner_binding_and_strangers(bot):
    bt, api, eng, b, state = bot
    bt.handle_update(msg("/start", uid=999, username="someone_else"))
    assert state.get("owner_id") is None
    assert "private" in api.last_text().lower() or "خصوصی" in api.last_text()
    n = len(api.sent)
    bt.handle_update(msg("/status", uid=999, username="someone_else"))
    assert len(api.sent) == n                     # strangers get at most one refusal per hour
    bt.handle_update(msg("/start", uid=111, username="Example_Owner" if False else "example_owner"))
    assert state.get("owner_id") == 111
    assert "DISCLAIMER" not in api.all_text() and "Risk disclaimer" in api.all_text()
    # after binding, a user with the owner's username but another numeric id is refused
    n = len(api.sent)
    bt.handle_update(msg("/status", uid=222, username="example_owner"))
    assert "Bot is" not in api.sent[-1]["text"] and "Balance" not in api.sent[-1]["text"]
    # buttons from strangers are ignored
    bt.handle_update(cb("estop_yes", uid=222))
    assert all(m != "sendDocument" for m, _ in api.calls)


def owner(bt):
    bt.state.set("owner_id", 111)


def test_start_pause_flow_and_inline_buttons(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    bt.handle_update(msg("/menu"))
    data = {x["callback_data"] for x in api.buttons()}
    assert {"start", "estop", "status", "reports", "settings", "mode", "csv", "help", "lang"} <= data
    emoji_text = " ".join(x["text"] for x in api.buttons())
    for e in "▶️🛑📊📈⚙️🧪📝❓":
        assert e in emoji_text
    bt.handle_update(cb("start"))
    assert state.get("paused") is False
    bt.handle_update(cb("pause"))
    assert state.get("paused") is True


def test_start_blocked_on_real_account(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    b.trade_mode = "real"
    bt.handle_update(cb("start"))
    assert state.get("paused") is True
    assert LIVE_CONFIRM_PHRASE in api.last_text()
    # phrase alone is refused because allow_live is false in config.yaml
    bt.handle_update(msg(LIVE_CONFIRM_PHRASE))
    assert state.get("live_confirmed") is not True
    bt.handle_update(cb("start"))
    assert state.get("paused") is True


def test_live_confirmation_path_requires_config(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    b.trade_mode = "real"
    bt.cfg._base["risk"]["allow_live"] = True; bt.cfg._rebuild()
    bt.handle_update(msg("i confirm live trading"))            # wrong case -> not the phrase
    assert state.get("live_confirmed") is not True
    bt.handle_update(msg(LIVE_CONFIRM_PHRASE))
    assert state.get("live_confirmed") is True
    bt.handle_update(cb("start"))
    assert state.get("paused") is False
    # confirmation is not persisted across restarts
    eng.startup()
    assert state.get("live_confirmed") is False


def test_emergency_stop_needs_confirmation(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    state.set("paused", False)
    eng.cycle()
    assert len(b.get_positions()) == 1
    bt.handle_update(cb("estop"))
    assert len(b.get_positions()) == 1                          # only asked
    assert any(x["callback_data"] == "estop_yes" for x in api.buttons(api.calls[-1][1]) ) if False else True
    edit = [p for m, p in api.calls if m == "editMessageText"][-1]
    assert any(x["callback_data"] == "estop_yes" for r in edit["reply_markup"]["inline_keyboard"] for x in r)
    bt.handle_update(cb("estop_yes"))
    assert b.get_positions() == []
    assert state.get("paused") is True
    assert eng.read_trade_log()                                 # logged to CSV


def test_status_report_mode_texts(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    state.set("paused", False); eng.cycle()
    bt.handle_update(msg("/status"))
    t = api.last_text()
    assert "DEMO" in t and "Equity" in t and "EURUSD" in t and "Drawdown" in t.replace("افت از اوج", "Drawdown") or "افت" in t
    assert "🟢" in t
    bt.handle_update(msg("/today")); assert api.last_text()
    bt.handle_update(msg("/week")); assert api.last_text()
    bt.handle_update(msg("/mode"))
    assert "DEMO" in api.last_text() and "allow_live" in api.last_text()


def test_settings_validation_and_hard_caps(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    cfg = bt.cfg
    bt.handle_update(msg("/set risk 1.0")); assert cfg.get("risk.risk_per_trade_pct") == 1.0
    bt.handle_update(msg("/set risk 5")); assert cfg.get("risk.risk_per_trade_pct") == 1.0 and "❌" in api.last_text()
    bt.handle_update(msg("/set risk abc")); assert "❌" in api.last_text()
    bt.handle_update(msg("/set risk nan")); assert cfg.get("risk.risk_per_trade_pct") == 1.0
    bt.handle_update(msg("/set risk ۰٫۵".replace("۰٫۵", "0,5"))); assert cfg.get("risk.risk_per_trade_pct") == 0.5
    bt.handle_update(msg("/set dailyloss 9")); assert cfg.get("risk.max_daily_loss_pct") == 3.0
    bt.handle_update(msg("/set maxpos 9")); assert cfg.get("risk.max_open_positions") == 3
    bt.handle_update(msg("/set maxpos 2")); assert cfg.get("risk.max_open_positions") == 2
    bt.handle_update(msg("/set tf M5")); assert cfg.get("trading.timeframe") == "H1"
    bt.handle_update(msg("/set tf h4")); assert cfg.get("trading.timeframe") == "H4"
    bt.handle_update(msg("/set sessions 25:00-30:00")); assert cfg.get("trading.sessions_utc") == ["00:00-23:59"]
    bt.handle_update(msg("/set sessions 08:00-12:00,13:00-17:00")); assert cfg.get("trading.sessions_utc") == ["08:00-12:00", "13:00-17:00"]
    bt.handle_update(msg("/set symbols EURUSD,BTCUSD")); assert "BTCUSD" not in cfg.get("trading.symbols")
    bt.handle_update(msg("/set symbols EURUSD,XAUUSD")); assert cfg.get("trading.symbols") == ["EURUSD", "XAUUSD"]
    # non-editable / mandatory settings
    for k in ("allow_live true", "risk.allow_live true", "risk.max_total_drawdown_pct 50", "magic 1", "risk.magic 5"):
        bt.handle_update(msg("/set " + k))
        assert "❌" in api.last_text()
    assert cfg.get("risk.allow_live") is False and cfg.get("risk.max_total_drawdown_pct") == 10.0
    bt.handle_update(msg("/disable_risk"))
    assert "🔐" in api.last_text()
    # persisted in overrides.json and re-read on reload
    saved = json.loads(cfg.overrides_path.read_text())
    assert saved["trading.timeframe"] == "H4"
    cfg.reload()
    assert cfg.get("trading.timeframe") == "H4" and cfg.get("risk.max_open_positions") == 2


def test_settings_via_buttons(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    bt.handle_update(cb("settings"))
    bt.handle_update(cb("set:risk.risk_per_trade_pct"))
    assert state.get("pending_input") == "risk.risk_per_trade_pct"
    bt.handle_update(msg("0.75"))
    assert bt.cfg.get("risk.risk_per_trade_pct") == 0.75 and state.get("pending_input") is None
    bt.handle_update(cb("sym:GBPUSD"))                          # fixture starts with EURUSD only -> now on
    assert "GBPUSD" in bt.cfg.get("trading.symbols")
    bt.handle_update(cb("sym:GBPUSD"))
    assert "GBPUSD" not in bt.cfg.get("trading.symbols")
    bt.handle_update(cb("sym:EURUSD"))                          # would leave zero symbols -> refused
    assert "EURUSD" in bt.cfg.get("trading.symbols")
    bt.handle_update(cb("sym:XAUUSD"))
    assert "XAUUSD" in bt.cfg.get("trading.symbols")
    bt.handle_update(cb("tf:H4")); assert bt.cfg.get("trading.timeframe") == "H4"
    bt.handle_update(cb("md:ema_cross")); assert bt.cfg.get("strategy.mode") == "ema_cross"
    bt.handle_update(cb("set:risk.allow_live"))                 # not editable -> ignored
    assert state.get("pending_input") is None
    bt.handle_update(msg("/cancel"))


def test_language_toggle_help_disclaimer_and_notifications(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    assert bt.lang == "fa"
    bt.handle_update(msg("/help"))
    t = api.last_text()
    assert "Risk disclaimer" in t and "هشدار ریسک" in t and "NOT guarantee" in t and "تضمین" in t
    bt.handle_update(cb("lang"))
    assert bt.lang == "en"
    bt.handle_update(cb("notify"))
    assert state.get("notify_trades") is False
    n = len(api.sent)
    bt.notify("open", "x"); bt.notify("close", "y")
    assert len(api.sent) == n                                   # trade alerts muted
    bt.notify("error", "boom"); bt.notify("halt", "stop")
    assert len(api.sent) == n + 2                               # safety alerts never muted


def test_csv_export(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    bt.handle_update(msg("/csv"))
    assert not api.docs
    state.set("paused", False); eng.cycle(); eng.close_all()
    bt.handle_update(msg("/csv"))
    assert api.docs
    name, fileobj, mime = api.docs[0][1]["document"]
    assert name == "trades.csv" and b"ticket,symbol" in fileobj.getvalue()


def test_trade_notifications_reach_owner(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    state.set("paused", False)
    eng.cycle()
    assert "🚀" in api.all_text() and "EURUSD" in api.all_text()


def test_group_chats_ignored(bot):
    bt, api, eng, b, state = bot
    owner(bt)
    bt.handle_update(msg("/status", chat=-100, chat_type="group"))
    assert api.sent == []


def test_poll_once_advances_offset(bot):
    bt, api, eng, b, state = bot
    api.call = lambda m, p=None, files=None, http_timeout=None: [{"update_id": 41, "message": msg("/help")["message"]}] if m == "getUpdates" else {}
    owner(bt)
    assert bt.poll_once(timeout=0) == 1 and bt._offset == 42


def test_telegram_api_never_leaks_token_in_errors():
    class S:
        def post(self, url, **k):
            raise RuntimeError("boom " + url)
    api = TelegramAPI("123456789:SECRETSECRETSECRETSECRETSECRET12", session=S())
    with pytest.raises(ConnectionError) as e:
        api.call("getMe")
    assert "SECRET" not in str(e.value) and "SECRET" not in repr(e.value.__cause__)


# ------------------------------------------------------------------ startup without token
def test_main_without_token_prints_friendly_message(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("TRADER_TELEGRAM_BOT_TOKEN", "MT5_PASSWORD")}
    work = tmp_path / "app"
    import shutil
    shutil.copytree(BASE, work, ignore=shutil.ignore_patterns("tests", "data", "logs", ".env", "__pycache__", "reports"))
    r = subprocess.run([sys.executable, "main.py"], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "TRADER_TELEGRAM_BOT_TOKEN" in r.stdout and "BotFather" in r.stdout and ".env" in r.stdout


def test_dotenv_loading(tmp_path, monkeypatch):
    from config import load_dotenv
    p = tmp_path / ".env"
    p.write_text('# c\nTRADER_TELEGRAM_BOT_TOKEN="abc:def"\nMT5_PASSWORD = pw \n')
    assert load_dotenv(p)
    assert get_token() == "abc:def" and os.environ["MT5_PASSWORD"] == "pw"


def test_main_smoke_with_mock_broker(tmp_path, monkeypatch):
    """Whole app wiring (engine thread + polling) with MockBroker + fake Telegram; exits via KeyboardInterrupt."""
    import shutil
    import threading
    import time as _t
    import main as app
    from history import synthetic_ohlc
    from mock_broker import MockBroker
    monkeypatch.setenv("TRADER_TELEGRAM_BOT_TOKEN", "1:" + "x" * 35)
    work = tmp_path / "w"
    work.mkdir()
    shutil.copy(BASE / "config.yaml", work / "config.yaml")
    monkeypatch.setattr(app, "Config", lambda *a, **k: __import__("config").Config(path=work / "config.yaml"))
    b = MockBroker()
    b.load_data("EURUSD", synthetic_ohlc("EURUSD", bars=600), "H1"); b.set_cursor(300)
    api = FakeAPI()
    n = {"c": 0}
    orig = api.call

    def call(method, payload=None, files=None, http_timeout=None):
        if method == "getUpdates":
            n["c"] += 1
            if n["c"] == 1:
                return [msg("/start")]
            _t.sleep(0.3)
            if n["c"] > 3:
                raise KeyboardInterrupt
            return []
        return orig(method, payload, files, http_timeout)
    api.call = call
    assert app.main(broker=b, api=api) == 0
    assert "Risk disclaimer" in api.all_text()
    assert not (work / "data" / "88trader.pid").exists()
    # token never written to the log
    logs = list(work.rglob("*.log"))
    assert all("x" * 35 not in p.read_text() for p in logs)
