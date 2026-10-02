import copy
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402
from history import synthetic_ohlc  # noqa: E402
from mock_broker import MockBroker  # noqa: E402
from state import State  # noqa: E402

BASE = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch):
    monkeypatch.delenv("TRADER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MT5_PASSWORD", raising=False)


@pytest.fixture
def cfg(tmp_path):
    c = Config(path=BASE / "config.yaml", base_dir=tmp_path)
    c.data["paths"] = {"data_dir": str(tmp_path / "data"), "log_file": str(tmp_path / "l.log")}
    return c


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.json")


@pytest.fixture
def broker():
    b = MockBroker(balance=10000)
    b.load_data("EURUSD", synthetic_ohlc("EURUSD", bars=1500, seed=3), "H1")
    b.load_data("GBPUSD", synthetic_ohlc("GBPUSD", bars=1500, seed=4), "H1")
    b.connect()
    b.set_cursor(400)
    return b
