"""Trading engine: one `cycle()` = reconnect-if-needed -> safety checks -> manage -> maybe open."""
from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from broker import Broker, utcnow
from config import Config
from models import AccountInfo, BrokerError, ClosedTrade, Position
from news import NewsFilter
from risk import Decision, RiskManager, day_key
from state import State
from strategy import evaluate, in_session
from utils import retry

log = logging.getLogger("88trader.engine")

TRADE_LOG_FIELDS = ["ticket", "symbol", "side", "volume", "open_time", "close_time", "open_price",
                    "close_price", "profit", "reason"]


class Engine:
    def __init__(self, cfg: Config, state: State, broker: Broker, notify: Callable[[str, str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        """notify(key, **) style is avoided: notify(kind, text). Kinds: open/close/error/halt/info."""
        self.cfg, self.state, self.broker = cfg, state, broker
        self.notify = notify or (lambda kind, text: None)
        self.sleep = sleep
        news_cfg = cfg.get("news", {}) or {}
        nf = Path(news_cfg.get("events_file", "news_events.csv"))
        nf = nf if nf.is_absolute() else cfg.base_dir / nf
        self.news = NewsFilter(bool(news_cfg.get("enabled", False)), nf, int(news_cfg.get("minutes_before", 30)),
                               int(news_cfg.get("minutes_after", 30)))
        self.risk = RiskManager(cfg, state, self.news)
        self.last_error: str = ""
        self.last_cycle: Optional[datetime] = None
        self.last_account: Optional[AccountInfo] = None
        self.last_block_reason: str = ""
        self._lock = threading.RLock()
        self._reconnect_fail_notified = False
        self._err_last_sent: dict[str, float] = {}
        self._mode_ok_notified: Optional[str] = None

    # ---------------------------------------------------------------- helpers
    @property
    def magic(self) -> int:
        return int(self.cfg.get("risk.magic"))

    def _retry(self, fn):
        return retry(fn, attempts=int(self.cfg.get("engine.retry_attempts", 4)),
                     base_delay=float(self.cfg.get("engine.retry_base_delay", 2.0)), sleep=self.sleep,
                     on_retry=lambda n, e: log.warning("retry %d after: %s", n, e))

    def _err(self, key: str, text: str, every: float = 900.0) -> None:
        """Rate-limited error notification (same key at most every `every` seconds)."""
        self.last_error = text
        log.error(text)
        now = time.time()
        if now - self._err_last_sent.get(key, 0) >= every:
            self._err_last_sent[key] = now
            self.notify("error", text)

    def ensure_connected(self) -> bool:
        if self.broker.is_connected():
            return True
        log.warning("broker disconnected - reconnecting")
        try:
            self._retry(self.broker.connect)
        except BrokerError as e:
            self._err("reconnect", f"MT5 reconnect failed: {e}")
            return False
        if self._reconnect_fail_notified or self.last_error.startswith("MT5 reconnect"):
            self.notify("info", "🔌 MT5 reconnected / اتصال MT5 برقرار شد")
        self._reconnect_fail_notified = False
        self.last_error = ""
        return True

    # ---------------------------------------------------------------- startup
    def startup(self) -> Decision:
        """Connect, verify account type, sync positions by magic (restart safety)."""
        self.state.set("live_confirmed", False)  # confirmation is per-session, never persisted
        self._retry(self.broker.connect)
        acct = self._retry(self.broker.account_info)
        self.last_account = acct
        d = self.risk.check_account_mode(acct)
        log.info("startup: login=%s server=%s mode=%s balance=%.2f", acct.login, acct.server, acct.trade_mode, acct.balance)
        self.risk.update(acct, self.broker.now())
        self.sync_positions(initial=True)
        return d

    def sync_positions(self, initial: bool = False) -> list[Position]:
        pos = self._retry(lambda: self.broker.get_positions(self.magic))
        known = {str(p.ticket): dict(symbol=p.symbol, side=p.side, volume=p.volume, price_open=p.price_open,
                                      sl=p.sl, tp=p.tp, time=p.time.isoformat()) for p in pos}
        if initial and known:
            log.info("restart: found %d open position(s) with magic %s - managing them", len(known), self.magic)
        self.state.set("known_positions", known)
        return pos

    # ---------------------------------------------------------------- closed-trade detection
    def detect_closed(self) -> list[ClosedTrade]:
        known: dict = dict(self.state.get("known_positions", {}) or {})
        if not known:
            return []
        current = {str(p.ticket) for p in self._retry(lambda: self.broker.get_positions(self.magic))}
        gone = [t for t in known if t not in current]
        if not gone:
            return []
        oldest = min(datetime.fromisoformat(known[t]["time"]) for t in gone) - timedelta(hours=1)
        trades = self._retry(lambda: self.broker.closed_trades(oldest, self.magic))
        by_id = {str(t.ticket): t for t in trades}
        out = []
        for t in gone:
            ct = by_id.get(t)
            if ct:
                out.append(ct)
                self._append_trade_log(ct)
                self.notify("close", self.format_close(ct))
            else:
                log.warning("position %s disappeared but no history found yet", t)
                continue
            known.pop(t, None)
        self.state.set("known_positions", known)
        return out

    @staticmethod
    def format_close(ct: ClosedTrade) -> str:
        icon = {"sl": "🛑 SL", "tp": "🎯 TP"}.get(ct.reason, "🔒")
        pl = "✅" if ct.profit >= 0 else "🔻"
        return (f"{icon} {ct.symbol} {ct.side.upper()} {ct.volume:g} lot\n"
                f"{ct.open_price:g} → {ct.close_price:g}\n{pl} P/L: {ct.profit:+.2f}")

    def trade_log_path(self) -> Path:
        return self.cfg.data_dir / "trades.csv"

    def _append_trade_log(self, ct: ClosedTrade) -> None:
        p = self.trade_log_path()
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: v for k, v in ct.to_dict().items() if k in TRADE_LOG_FIELDS})

    # ---------------------------------------------------------------- reports
    def read_trade_log(self) -> list[dict]:
        p = self.trade_log_path()
        if not p.exists():
            return []
        with open(p, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def report(self, since: datetime) -> dict:
        rows = [r for r in self.read_trade_log() if datetime.strptime(r["close_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) >= since]
        pl = [float(r["profit"]) for r in rows]
        wins = [x for x in pl if x > 0]
        return {"trades": len(pl), "wins": len(wins), "losses": len(pl) - len(wins), "pnl": sum(pl),
                "win_rate": (len(wins) / len(pl) * 100.0) if pl else 0.0}

    # ---------------------------------------------------------------- emergency
    def close_all(self) -> tuple[int, int]:
        """Close every position of THIS bot (by magic). Returns (closed, failed)."""
        ok = bad = 0
        dev = int(self.cfg.get("risk.max_slippage_points", 20)) * 5  # emergency: allow wider slippage
        for p in self._retry(lambda: self.broker.get_positions(self.magic)):
            try:
                r = self._retry(lambda: self.broker.close_position(p.ticket, deviation=dev))
                if r.ok:
                    ok += 1
                else:
                    bad += 1
                    log.error("close %s failed: %s", p.ticket, r.message)
            except BrokerError as e:
                bad += 1
                log.error("close %s error: %s", p.ticket, e)
        try:
            self.detect_closed()
        except BrokerError:
            pass
        return ok, bad

    # ---------------------------------------------------------------- main cycle
    def cycle(self) -> str:
        """Returns a short status string (useful for tests)."""
        with self._lock:
            now = self.broker.now()
            self.last_cycle = now
            if not self.ensure_connected():
                return "disconnected"
            try:
                acct = self._retry(self.broker.account_info)
                self.last_account = acct
                # 1) account-type guard every cycle
                mode_ok = self.risk.check_account_mode(acct)
                # 2) track equity + halts
                for ev in self.risk.update(acct, now):
                    if ev == "daily_loss":
                        self.notify("halt", "🚫 حد ضرر روزانه فعال شد؛ تا فردا معامله نمی‌شود.\n"
                                            "Daily loss limit hit - no new trades until tomorrow (UTC).")
                    elif ev == "drawdown":
                        self.notify("halt", "🚨 حداکثر افت سرمایه رسید؛ ربات متوقف شد تا ریست دستی.\n"
                                            "Max drawdown hit - bot halted until manual reset.")
                # 3) report closed trades
                self.detect_closed()
                # 4) gates
                if not mode_ok:
                    self.last_block_reason = mode_ok.reason
                    if self._mode_ok_notified != mode_ok.code:
                        self._mode_ok_notified = mode_ok.code
                        self.notify("error", f"⛔ {mode_ok.reason}")
                    return "blocked:" + mode_ok.code
                self._mode_ok_notified = None
                if self.state.get("paused", True):
                    self.last_block_reason = "paused"
                    return "paused"
                h = self.risk.halted(now)
                if not h:
                    self.last_block_reason = h.reason
                    return "halted"
                if not in_session(now, self.cfg.get("trading.sessions_utc"), self.cfg.get("trading.trading_days")):
                    self.last_block_reason = "outside session"
                    return "outside_session"
                self.last_block_reason = ""
                opened = 0
                for sym in list(self.cfg.get("trading.symbols", [])):
                    if self._try_symbol(sym, acct, now):
                        opened += 1
                        acct = self._retry(self.broker.account_info)
                return f"ok:{opened}"
            except BrokerError as e:
                self._err("cycle", f"Broker error: {e}")
                try:
                    self.broker.shutdown()
                except Exception:
                    pass
                return "error"
            except Exception as e:  # never let an unexpected bug kill the loop
                log.exception("unexpected error in cycle")
                self._err("bug", f"Unexpected error: {type(e).__name__}: {e}")
                return "error"

    def _try_symbol(self, sym: str, acct: AccountInfo, now: datetime) -> bool:
        tf = self.cfg.get("trading.timeframe", "H1")
        try:
            rates = self._retry(lambda: self.broker.get_rates(sym, tf, int(self.cfg.get("trading.candles", 400))))
            si = self._retry(lambda: self.broker.symbol_info(sym))
        except BrokerError as e:
            self._err(f"data:{sym}", f"{sym}: data error: {e}")
            return False
        last_closed = rates.index[-2].isoformat() if len(rates) >= 2 else None
        last_bar = self.state.get("last_bar", {}) or {}
        if last_bar.get(sym) == last_closed:
            return False  # this closed bar was already evaluated: one decision per bar
        last_bar[sym] = last_closed
        self.state.set("last_bar", last_bar)
        sig = evaluate(rates, self.cfg.get("strategy"), si.point)
        if sig is None:
            return False
        positions = self._retry(lambda: self.broker.get_positions())
        d = self.risk.can_open(sym, sig.side, acct, positions, si, now)
        if not d:
            log.info("skip %s %s: %s", sym, sig.side, d.reason)
            if d.code == "rate_gap":  # temporary block: re-evaluate this bar on the next cycle
                last_bar.pop(sym, None)
                self.state.set("last_bar", last_bar)
            return False
        entry = si.ask if sig.side == "buy" else si.bid
        sl = entry - sig.sl_distance if sig.side == "buy" else entry + sig.sl_distance
        tp = entry + sig.tp_distance if sig.side == "buy" else entry - sig.tp_distance
        sl, tp = round(sl, si.digits), round(tp, si.digits)
        v = self.risk.validate_order(sig.side, entry, sl, tp, si)
        if not v:
            log.warning("skip %s: %s", sym, v.reason)
            return False
        vol = self.risk.size(acct, abs(entry - sl), si)
        if vol <= 0:
            log.info("skip %s: stop too wide for min lot at current risk%%", sym)
            self.notify("info", f"ℹ️ {sym}: سیگنال رد شد؛ حداقل لات ریسک را از حد بیشتر می‌کند.\n"
                                f"Signal skipped: minimum lot would exceed the risk limit.")
            return False
        dev = int(self.cfg.get("risk.max_slippage_points", 20))
        # Order is NOT retried blindly on timeouts (see MT5Broker.RETRYABLE): avoids duplicate orders.
        try:
            res = self._retry(lambda: self.broker.place_order(sym, sig.side, vol, sl, tp, deviation=dev,
                                                               magic=self.magic, comment="88Trader"))
        except BrokerError as e:
            self._err(f"order:{sym}", f"{sym}: order error: {e}")
            return False
        self.risk.record_order(now)
        if not res.ok:
            self._err(f"order:{sym}", f"{sym}: order rejected: {res.message}", every=300)
            return False
        self.sync_positions()
        self.notify("open", f"🚀 {sym} {sig.side.upper()} {vol:g} lot @ {res.price:g}\n"
                            f"SL {sl:g} | TP {tp:g}\n💡 {sig.reason}")
        log.info("opened %s %s %s lot ticket=%s", sym, sig.side, vol, res.ticket)
        return True

    # ---------------------------------------------------------------- run loop
    def run_forever(self, stop_event: threading.Event) -> None:
        poll = float(self.cfg.get("engine.poll_seconds", 20))
        while not stop_event.is_set():
            self.cycle()
            stop_event.wait(poll)
