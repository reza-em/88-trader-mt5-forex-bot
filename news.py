"""Optional high-impact-news pause hook (DISABLED by default).

Put events into news_events.csv (UTC):   datetime_utc,currency,impact,title
  2026-10-02 12:30,USD,high,Non-Farm Payrolls
While enabled, new entries on pairs containing that currency are paused from `minutes_before` to
`minutes_after` around each 'high' impact event. Nothing is downloaded automatically.
You may also replace NewsFilter.is_blocked with your own function (API, calendar, ...).
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("88trader.news")

SYMBOL_CCY = {"XAUUSD": ["XAU", "USD"]}


def currencies(symbol: str) -> list[str]:
    return SYMBOL_CCY.get(symbol, [symbol[:3], symbol[3:6]])


class NewsFilter:
    def __init__(self, enabled: bool = False, events_file: str | Path = "news_events.csv",
                 minutes_before: int = 30, minutes_after: int = 30):
        self.enabled = enabled
        self.path = Path(events_file)
        self.before = timedelta(minutes=minutes_before)
        self.after = timedelta(minutes=minutes_after)
        self.events: list[tuple[datetime, str, str]] = []
        self._mtime = None

    def _load(self) -> None:
        if not self.path.is_file():
            self.events = []
            return
        m = self.path.stat().st_mtime
        if m == self._mtime:
            return
        self._mtime = m
        ev = []
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    if str(row.get("impact", "")).strip().lower() != "high":
                        continue
                    t = datetime.strptime(row["datetime_utc"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                    ev.append((t, row["currency"].strip().upper(), row.get("title", "")))
                except Exception:
                    log.warning("bad news row ignored: %s", row)
        self.events = ev

    def is_blocked(self, symbol: str, now: datetime) -> tuple[bool, str]:
        if not self.enabled:
            return False, ""
        self._load()
        ccys = currencies(symbol)
        for t, c, title in self.events:
            if c in ccys and t - self.before <= now <= t + self.after:
                return True, f"{c} {title} @ {t:%H:%M}"
        return False, ""
