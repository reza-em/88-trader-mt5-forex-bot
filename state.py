"""Persistent JSON state (atomic writes, thread-safe). Restart-safe."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("88trader.state")

DEFAULTS: dict[str, Any] = {
    "owner_id": None,
    "paused": True,            # first run starts paused; owner presses ▶️
    "halt_daily_day": None,    # 'YYYY-MM-DD' (UTC) if the daily loss limit was hit that day
    "halt_drawdown": False,    # only cleared manually with reset_halt.py
    "peak_equity": None,
    "day_key": None,
    "day_start_equity": None,
    "live_confirmed": False,   # never trusted across restarts (cleared at startup)
    "language": "fa",
    "known_positions": {},     # ticket(str) -> {symbol, side, volume, price_open, sl, tp, time}
    "last_bar": {},            # symbol -> ISO time of last evaluated closed bar
    "order_times": [],         # epoch seconds of recent orders (rate limit)
    "last_closed_check": None, # ISO time
    "pending_input": None,
    "notified_reasons": [],
}


class State:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.is_file():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    for k, v in raw.items():
                        self.data[k] = v
                except Exception as e:
                    bad = self.path.with_suffix(".corrupt")
                    try:
                        os.replace(self.path, bad)
                    except OSError:
                        pass
                    log.warning("state file unreadable (%s); moved to %s and starting fresh", e, bad.name)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with self._lock:
            self.data[key] = value
            if save:
                self.save()

    def update(self, **kw: Any) -> None:
        with self._lock:
            self.data.update(kw)
            self.save()
