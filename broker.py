"""Broker abstraction + MT5Broker (real, Windows only) + MockBroker (tests / backtest)."""
from __future__ import annotations

import abc
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from models import (AccountInfo, BrokerError, ClosedTrade, OrderResult, Position, SymbolInfo)

log = logging.getLogger("88trader.broker")

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================== interface
class Broker(abc.ABC):
    """Everything the bot needs from a broker. All times are timezone-aware UTC."""

    # --- connection
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def shutdown(self) -> None: ...

    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    def now(self) -> datetime:
        return utcnow()

    # --- market data / account
    @abc.abstractmethod
    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """DataFrame indexed by UTC bar-open time with open/high/low/close/tick_volume.
        The LAST row is the currently forming bar (like MT5); strategies must ignore it."""

    @abc.abstractmethod
    def get_positions(self, magic: Optional[int] = None) -> list[Position]: ...

    @abc.abstractmethod
    def place_order(self, symbol: str, side: str, volume: float, sl: float, tp: float,
                    deviation: int = 20, magic: int = 0, comment: str = "88Trader") -> OrderResult: ...

    @abc.abstractmethod
    def close_position(self, ticket: int, deviation: int = 20) -> OrderResult: ...

    @abc.abstractmethod
    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> OrderResult: ...

    @abc.abstractmethod
    def account_info(self) -> AccountInfo: ...

    @abc.abstractmethod
    def symbol_info(self, symbol: str) -> SymbolInfo: ...

    @abc.abstractmethod
    def closed_trades(self, since: datetime, magic: Optional[int] = None) -> list[ClosedTrade]:
        """Positions fully closed since `since` (UTC)."""

    # helpers
    def resolve_symbol(self, symbol: str) -> str:
        return symbol


# =============================================================== MT5
class MT5Broker(Broker):
    """Real broker through the official `MetaTrader5` package (Windows + MT5 terminal required).

    NOTE: this class could NOT be run in the development sandbox (no Windows / no MT5).
    Use check_setup.py on the Windows PC first.
    """

    # Return codes where the order was certainly NOT executed, so a retry cannot duplicate it:
    # 10004 requote, 10020 prices changed, 10021 no quotes, 10024 too many requests, 10031 no connection.
    # (10012 "timeout" is deliberately NOT retried automatically: the order may have been executed.)
    RETRYABLE = {10004, 10020, 10021, 10024, 10031}

    def __init__(self, login: int, password: str, server: str, path: str = "", timeout_ms: int = 60000,
                 symbol_suffix: str = "", utc_offset_hours="auto", mt5_module=None):
        self._mt5 = mt5_module
        self.login, self._password, self.server = int(login), password, server
        self.path, self.timeout_ms, self.suffix = path, int(timeout_ms), symbol_suffix or ""
        self._offset_cfg = utc_offset_hours
        self._offset_s: Optional[int] = None if str(utc_offset_hours).lower() == "auto" else int(float(utc_offset_hours) * 3600)
        self._connected = False

    # ---- lazy import so the module loads on Linux
    @property
    def mt5(self):
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore
            except ImportError as e:
                raise BrokerError("بسته MetaTrader5 نصب نیست (فقط ویندوز) / MetaTrader5 package missing (Windows only)") from e
            self._mt5 = mt5
        return self._mt5

    def resolve_symbol(self, symbol: str) -> str:
        return symbol + self.suffix

    # ---- connection
    def connect(self) -> None:
        mt5 = self.mt5
        kwargs = dict(login=self.login, password=self._password, server=self.server, timeout=self.timeout_ms)
        if self.path:
            kwargs["path"] = self.path
        try:
            mt5.shutdown()
        except Exception:
            pass
        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            self._connected = False
            raise BrokerError(f"MT5 initialize failed: {err}")
        if mt5.account_info() is None:
            self._connected = False
            raise BrokerError(f"MT5 login failed: {mt5.last_error()}")
        self._connected = True
        log.info("connected to MT5 (login %s, server %s)", self.login, self.server)

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        finally:
            self._connected = False

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        try:
            ti = self.mt5.terminal_info()
            return bool(ti is not None and ti.connected)
        except Exception:
            return False

    def _need(self):
        if not self._connected:
            raise BrokerError("not connected")

    # ---- time conversion
    def server_offset(self, symbol: str = "EURUSD") -> int:
        """Seconds to subtract from MT5 'server time' stamps to get UTC."""
        if self._offset_s is not None and str(self._offset_cfg).lower() != "auto":
            return self._offset_s
        now = utcnow()
        if 1 <= now.weekday() <= 3 or (now.weekday() in (0, 4) and 3 <= now.hour <= 18):
            tick = self.mt5.symbol_info_tick(self.resolve_symbol(symbol))
            if tick is not None and tick.time:
                diff = tick.time - now.timestamp()
                off = int(round(diff / 1800.0)) * 1800
                if abs(off) <= 14 * 3600 and abs(diff - off) < 600:
                    self._offset_s = off
        return self._offset_s if self._offset_s is not None else 0

    def _to_utc(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts - self.server_offset(), tz=timezone.utc)

    def _tf(self, timeframe: str):
        m = self.mt5
        table = {"M1": m.TIMEFRAME_M1, "M5": m.TIMEFRAME_M5, "M15": m.TIMEFRAME_M15, "M30": m.TIMEFRAME_M30,
                 "H1": m.TIMEFRAME_H1, "H4": m.TIMEFRAME_H4, "D1": m.TIMEFRAME_D1}
        return table[timeframe.upper()]

    # ---- data
    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self._need()
        sym = self.resolve_symbol(symbol)
        self.mt5.symbol_select(sym, True)
        rates = self.mt5.copy_rates_from_pos(sym, self._tf(timeframe), 0, int(count))
        if rates is None or len(rates) == 0:
            raise BrokerError(f"no rates for {sym}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        off = self.server_offset(symbol)
        df.index = pd.to_datetime(df["time"] - off, unit="s", utc=True)
        df.index.name = "time"
        return df[["open", "high", "low", "close", "tick_volume"]].astype(float)

    def account_info(self) -> AccountInfo:
        self._need()
        a = self.mt5.account_info()
        if a is None:
            raise BrokerError(f"account_info failed: {self.mt5.last_error()}")
        mode = {self.mt5.ACCOUNT_TRADE_MODE_DEMO: "demo", self.mt5.ACCOUNT_TRADE_MODE_CONTEST: "contest",
                self.mt5.ACCOUNT_TRADE_MODE_REAL: "real"}.get(a.trade_mode, "unknown")
        return AccountInfo(login=a.login, server=a.server, currency=a.currency, balance=a.balance, equity=a.equity,
                           margin=a.margin, margin_free=a.margin_free, leverage=a.leverage, trade_mode=mode, name=a.name)

    def symbol_info(self, symbol: str) -> SymbolInfo:
        self._need()
        sym = self.resolve_symbol(symbol)
        self.mt5.symbol_select(sym, True)
        i = self.mt5.symbol_info(sym)
        t = self.mt5.symbol_info_tick(sym)
        if i is None or t is None:
            raise BrokerError(f"symbol {sym} unavailable: {self.mt5.last_error()}")
        allowed = i.trade_mode == self.mt5.SYMBOL_TRADE_MODE_FULL
        return SymbolInfo(name=symbol, digits=i.digits, point=i.point, tick_size=i.trade_tick_size or i.point,
                          tick_value=i.trade_tick_value_loss or i.trade_tick_value, volume_min=i.volume_min,
                          volume_max=i.volume_max, volume_step=i.volume_step, contract_size=i.trade_contract_size,
                          bid=t.bid, ask=t.ask, spread_points=round((t.ask - t.bid) / i.point, 1),
                          stops_level=i.trade_stops_level, trade_allowed=allowed)

    def get_positions(self, magic: Optional[int] = None) -> list[Position]:
        self._need()
        raw = self.mt5.positions_get()
        if raw is None:
            code, _ = self.mt5.last_error()
            if code == 1:  # RES_S_OK : simply no positions
                return []
            raise BrokerError(f"positions_get failed: {self.mt5.last_error()}")
        out = []
        for p in raw:
            if magic is not None and p.magic != magic:
                continue
            sym = p.symbol[:-len(self.suffix)] if self.suffix and p.symbol.endswith(self.suffix) else p.symbol
            out.append(Position(ticket=p.ticket, symbol=sym,
                                side="buy" if p.type == self.mt5.POSITION_TYPE_BUY else "sell",
                                volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp,
                                profit=p.profit + getattr(p, "swap", 0.0), time=self._to_utc(p.time),
                                magic=p.magic, comment=p.comment))
        return out

    # ---- trading
    def _filling(self, info) -> int:
        m = self.mt5
        fm = info.filling_mode
        if fm & 2:
            return m.ORDER_FILLING_IOC
        if fm & 1:
            return m.ORDER_FILLING_FOK
        return m.ORDER_FILLING_RETURN

    def _send(self, request: dict, what: str) -> OrderResult:
        m = self.mt5
        chk = m.order_check(request)
        if chk is None:
            raise BrokerError(f"order_check returned None: {m.last_error()}")
        if chk.retcode != 0 and chk.retcode != m.TRADE_RETCODE_DONE:
            if chk.retcode in self.RETRYABLE:
                raise BrokerError(f"{what}: retryable check retcode {chk.retcode}")
            return OrderResult(False, retcode=chk.retcode, message=f"{what} rejected by check: {chk.comment}")
        res = m.order_send(request)
        if res is None:
            raise BrokerError(f"order_send returned None: {m.last_error()}")
        if res.retcode == m.TRADE_RETCODE_DONE or res.retcode == m.TRADE_RETCODE_PLACED:
            return OrderResult(True, ticket=res.order, price=res.price, volume=res.volume, retcode=res.retcode)
        if res.retcode in self.RETRYABLE:
            raise BrokerError(f"{what}: retryable retcode {res.retcode} {res.comment}")
        return OrderResult(False, retcode=res.retcode, message=f"{what} failed: {res.retcode} {res.comment}")

    def place_order(self, symbol, side, volume, sl, tp, deviation=20, magic=0, comment="88Trader") -> OrderResult:
        self._need()
        m = self.mt5
        if not sl:
            return OrderResult(False, message="refused: stop loss is mandatory")
        sym = self.resolve_symbol(symbol)
        m.symbol_select(sym, True)
        info, tick = m.symbol_info(sym), m.symbol_info_tick(sym)
        if info is None or tick is None:
            raise BrokerError(f"symbol {sym} unavailable")
        buy = side == "buy"
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": sym, "volume": float(volume),
               "type": m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL,
               "price": tick.ask if buy else tick.bid, "sl": round(sl, info.digits), "tp": round(tp, info.digits),
               "deviation": int(deviation), "magic": int(magic), "comment": comment[:30],
               "type_time": m.ORDER_TIME_GTC, "type_filling": self._filling(info)}
        r = self._send(req, "open")
        if r.ok:  # MT5 returns the deal/order; resolve the position ticket
            for p in (m.positions_get(symbol=sym) or []):
                if p.magic == magic and p.sl == req["sl"] and abs(p.volume - volume) < 1e-9:
                    r.ticket = p.ticket
        return r

    def close_position(self, ticket: int, deviation: int = 20) -> OrderResult:
        self._need()
        m = self.mt5
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return OrderResult(False, message="position not found")
        p = pos[0]
        info, tick = m.symbol_info(p.symbol), m.symbol_info_tick(p.symbol)
        if info is None or tick is None:
            raise BrokerError("symbol/tick unavailable")
        buy = p.type == m.POSITION_TYPE_BUY
        req = {"action": m.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": p.ticket,
               "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, "price": tick.bid if buy else tick.ask,
               "deviation": int(deviation), "magic": p.magic, "comment": "88Trader close",
               "type_time": m.ORDER_TIME_GTC, "type_filling": self._filling(info)}
        return self._send(req, "close")

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> OrderResult:
        self._need()
        m = self.mt5
        pos = m.positions_get(ticket=ticket)
        if not pos:
            return OrderResult(False, message="position not found")
        if not sl:
            return OrderResult(False, message="refused: stop loss is mandatory")
        p = pos[0]
        info = m.symbol_info(p.symbol)
        req = {"action": m.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket,
               "sl": round(sl, info.digits), "tp": round(tp, info.digits), "magic": p.magic}
        return self._send(req, "modify")

    def closed_trades(self, since: datetime, magic: Optional[int] = None) -> list[ClosedTrade]:
        self._need()
        m = self.mt5
        off = timedelta(seconds=self.server_offset())
        # the python API wants naive datetimes in server time; pad the window generously
        frm = (since - timedelta(days=1) + off).replace(tzinfo=None)
        to = (utcnow() + timedelta(days=2) + off).replace(tzinfo=None)
        deals = m.history_deals_get(frm, to)
        if deals is None:
            raise BrokerError(f"history_deals_get failed: {m.last_error()}")
        by_pos: dict[int, list] = {}
        for d in deals:
            if d.position_id:
                by_pos.setdefault(d.position_id, []).append(d)
        reasons = {m.DEAL_REASON_SL: "sl", m.DEAL_REASON_TP: "tp", m.DEAL_REASON_SO: "stopout",
                   m.DEAL_REASON_CLIENT: "manual", m.DEAL_REASON_MOBILE: "manual", m.DEAL_REASON_WEB: "manual",
                   m.DEAL_REASON_EXPERT: "bot"}
        out = []
        for pid, ds in by_pos.items():
            ins = [d for d in ds if d.entry == m.DEAL_ENTRY_IN]
            outs = [d for d in ds if d.entry in (m.DEAL_ENTRY_OUT, m.DEAL_ENTRY_OUT_BY)]
            if not ins or not outs:
                continue
            if magic is not None and not any(d.magic == magic for d in ds):
                continue
            last = max(outs, key=lambda d: d.time)
            ctime = self._to_utc(last.time)
            if ctime < since:
                continue
            first = min(ins, key=lambda d: d.time)
            sym = first.symbol[:-len(self.suffix)] if self.suffix and first.symbol.endswith(self.suffix) else first.symbol
            net = sum(d.profit + d.swap + d.commission + getattr(d, "fee", 0.0) for d in ds)
            out.append(ClosedTrade(ticket=pid, symbol=sym,
                                   side="buy" if first.type == m.DEAL_TYPE_BUY else "sell",
                                   volume=first.volume, open_time=self._to_utc(first.time), close_time=ctime,
                                   open_price=first.price, close_price=last.price, profit=net,
                                   reason=reasons.get(last.reason, "unknown")))
        return sorted(out, key=lambda t: t.close_time)
