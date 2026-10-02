"""Plain data containers shared by every module (broker-agnostic)."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional


@dataclass
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    trade_mode: str  # 'demo' | 'contest' | 'real' | 'unknown'
    name: str = ""


@dataclass
class SymbolInfo:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float  # account-currency value of one tick for 1 lot (loss side)
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float
    bid: float
    ask: float
    spread_points: float
    stops_level: int = 0
    trade_allowed: bool = True


@dataclass
class Position:
    ticket: int
    symbol: str
    side: str  # 'buy' | 'sell'
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float  # floating P/L incl. swap/commission when available
    time: datetime  # UTC
    magic: int = 0
    comment: str = ""


@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    volume: float = 0.0
    retcode: int = 0
    message: str = ""


@dataclass
class ClosedTrade:
    ticket: int  # position id
    symbol: str
    side: str
    volume: float
    open_time: datetime
    close_time: datetime
    open_price: float
    close_price: float
    profit: float  # net (profit + swap + commission)
    reason: str = "unknown"  # sl | tp | manual | bot | stopout | unknown

    def to_dict(self) -> dict:
        d = asdict(self)
        d["open_time"] = self.open_time.strftime("%Y-%m-%d %H:%M:%S")
        d["close_time"] = self.close_time.strftime("%Y-%m-%d %H:%M:%S")
        return d


class BrokerError(Exception):
    """Raised for connection / transport problems (retryable)."""


class OrderRejected(Exception):
    """Raised when the broker refuses an order (not retryable as-is)."""
