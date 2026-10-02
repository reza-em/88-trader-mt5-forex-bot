"""Logging (rotating, secret-redacting) + retry helper."""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import time
from pathlib import Path
from typing import Callable, TypeVar

from models import BrokerError

T = TypeVar("T")
SECRET_ENV_VARS = ("TRADER_TELEGRAM_BOT_TOKEN", "MT5_PASSWORD")
_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


class RedactFilter(logging.Filter):
    """Removes bot tokens / passwords from every log line, even by accident."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for name in SECRET_ENV_VARS:
            val = os.environ.get(name)
            if val and len(val) >= 4 and val in msg:
                msg = msg.replace(val, "***")
        msg = _TOKEN_RE.sub("***", msg)
        record.msg, record.args = msg, None
        return True


def setup_logging(log_file: str | os.PathLike, level: int = logging.INFO, console: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_88trader", False):
            root.removeHandler(h)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = Path(log_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(p, maxBytes=1_000_000, backupCount=5, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        h._88trader = True  # type: ignore[attr-defined]
        root.addHandler(h)
    # 'requests'/'urllib3' log full URLs (which contain the Telegram token) at DEBUG - keep them quiet
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def retry(fn: Callable[[], T], attempts: int = 4, base_delay: float = 2.0, max_delay: float = 60.0,
          retry_on: tuple = (BrokerError,), sleep: Callable[[float], None] = time.sleep,
          on_retry: Callable[[int, Exception], None] | None = None) -> T:
    """Call fn(); on `retry_on` exceptions wait base*2^n (capped) and try again."""
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except retry_on as e:  # type: ignore[misc]
            last = e
            if i == attempts - 1:
                break
            if on_retry:
                on_retry(i + 1, e)
            sleep(min(max_delay, base_delay * (2 ** i)))
    assert last is not None
    raise last
