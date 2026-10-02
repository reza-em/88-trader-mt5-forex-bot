"""MockBroker: deterministic, bar-by-bar simulated broker for unit tests and backtests.

Model (intentionally conservative):
  * prices are BID prices from the supplied candles; ASK = BID + spread (fixed, configurable per symbol)
  * market orders fill at the bar open (+ slippage against you)
  * if SL and TP are both touched inside one bar, the STOP LOSS is assumed to hit first
  * stop-loss fills get extra slippage against you
  * optional commission per lot per side
  * account currency USD; USD-quoted pairs/gold and USD-based pairs (USDJPY...) convert correctly
"""
from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from broker import Broker, TF_MINUTES
from models import (AccountInfo, BrokerError, ClosedTrade, OrderResult, Position, SymbolInfo)

DIGITS = {"USDJPY": 3, "XAUUSD": 2}
CONTRACT = {"XAUUSD": 100.0}
DEFAULT_SPREAD_POINTS = {"EURUSD": 12.0, "GBPUSD": 15.0, "USDJPY": 13.0, "XAUUSD": 30.0}


def _digits(sym: str) -> int:
    return DIGITS.get(sym, 5)


class MockBroker(Broker):
    def __init__(self, balance: float = 10000.0, trade_mode: str = "demo", leverage: int = 500,
                 spread_points: Optional[dict] = None, slippage_points: float = 1.0,
                 sl_extra_slippage_points: float = 2.0, commission_per_lot: float = 0.0,
                 login: int = 12345678, server: str = "Mock-Demo"):
        self.balance = float(balance)
        self.trade_mode = trade_mode
        self.leverage = leverage
        self.login, self.server = login, server
        self.spread_points = dict(DEFAULT_SPREAD_POINTS)
        if spread_points:
            self.spread_points.update(spread_points)
        self.slippage_points = slippage_points
        self.sl_extra_slippage_points = sl_extra_slippage_points
        self.commission_per_lot = commission_per_lot
        self._data: dict[str, pd.DataFrame] = {}
        self._base_tf = "H1"
        self._cursor: dict[str, int] = {}
        self._time: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self._positions: dict[int, dict] = {}
        self._closed: list[ClosedTrade] = []
        self._ticket = itertools.count(1000)
        self._connected = False
        self.connect_failures = 0          # test hook: next N connect() calls fail
        self.fail_calls = 0                # test hook: next N data calls raise BrokerError
        self.orders_sent = 0
        self.fixed_quotes: dict[str, tuple[float, float]] = {}  # test hook: symbol -> (bid, ask)
        self.stopped_out_ticks: list[int] = []

    # ------------------------------------------------------------------ data loading
    def load_data(self, symbol: str, df: pd.DataFrame, timeframe: str = "H1") -> None:
        df = df.sort_index()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        self._data[symbol] = df[["open", "high", "low", "close"]].astype(float)
        self._base_tf = timeframe
        self._cursor.setdefault(symbol, 0)

    def set_cursor(self, index: int, symbol: Optional[str] = None) -> None:
        """Move the simulated clock to the OPEN of bar `index` (no look-ahead beyond it)."""
        for s, df in self._data.items():
            if symbol and s != symbol:
                continue
            self._cursor[s] = min(index, len(df) - 1)
        first = next(iter(self._data.values()))
        self._time = first.index[min(index, len(first) - 1)].to_pydatetime()

    def set_time(self, t: datetime) -> None:
        self._time = t

    def now(self) -> datetime:
        return self._time

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise BrokerError("mock: connection refused")
        self._connected = True

    def shutdown(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:  # test hook
        self._connected = False

    def _need(self) -> None:
        if not self._connected:
            raise BrokerError("mock: not connected")
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise BrokerError("mock: transient failure")

    # ------------------------------------------------------------------ quotes
    def _bar(self, symbol: str) -> pd.Series:
        df = self._data.get(symbol)
        if df is None:
            raise BrokerError(f"mock: no data for {symbol}")
        return df.iloc[self._cursor[symbol]]

    def _spread_price(self, symbol: str) -> float:
        return self.spread_points.get(symbol, 20.0) * 10 ** -_digits(symbol)

    def _quote(self, symbol: str) -> tuple[float, float]:
        if symbol in self.fixed_quotes:
            return self.fixed_quotes[symbol]
        bid = float(self._bar(symbol)["open"])
        return bid, bid + self._spread_price(symbol)

    def _conv(self, symbol: str, price: float) -> float:
        """Multiplier converting quote-currency P/L into USD."""
        return 1.0 / price if symbol.startswith("USD") else 1.0

    def _contract(self, symbol: str) -> float:
        return CONTRACT.get(symbol, 100000.0)

    # ------------------------------------------------------------------ rates
    def get_rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self._need()
        df = self._data.get(symbol)
        if df is None:
            raise BrokerError(f"mock: no data for {symbol}")
        i = self._cursor[symbol]
        tfm = TF_MINUTES[timeframe.upper()]
        ratio = max(1, tfm // TF_MINUTES[self._base_tf])
        closed = df.iloc[max(0, i - (count + 2) * ratio - ratio):i]
        cur = df.iloc[i]
        forming = pd.DataFrame({"open": [cur["open"]], "high": [cur["open"]], "low": [cur["open"]],
                                "close": [cur["open"]]}, index=[df.index[i]])
        hist = pd.concat([closed, forming])
        if tfm < TF_MINUTES[self._base_tf]:
            raise BrokerError(f"mock: data resolution {self._base_tf} is coarser than {timeframe}")
        if tfm > TF_MINUTES[self._base_tf]:
            hist = hist.resample(f"{tfm}min", origin="epoch", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        hist = hist.copy()
        hist["tick_volume"] = 1.0
        return hist.iloc[-count:]

    # ------------------------------------------------------------------ account
    def _floating(self) -> float:
        tot = 0.0
        for p in self._positions.values():
            bid, ask = self._quote(p["symbol"])
            tot += self._pnl(p, bid if p["side"] == "buy" else ask)
        return tot

    def _pnl(self, p: dict, exit_price: float) -> float:
        d = 1 if p["side"] == "buy" else -1
        gross = (exit_price - p["price_open"]) * d * self._contract(p["symbol"]) * p["volume"] * self._conv(p["symbol"], exit_price)
        return gross - self.commission_per_lot * p["volume"]  # one commission side (second side added on close)

    def account_info(self) -> AccountInfo:
        self._need()
        fl = self._floating() if self._data else 0.0
        eq = self.balance + fl
        margin = 0.0
        for p in self._positions.values():
            margin += p["volume"] * self._contract(p["symbol"]) * p["price_open"] * self._conv(p["symbol"], p["price_open"]) / self.leverage
        return AccountInfo(login=self.login, server=self.server, currency="USD", balance=self.balance, equity=eq,
                           margin=margin, margin_free=eq - margin, leverage=self.leverage,
                           trade_mode=self.trade_mode, name="Mock")

    def symbol_info(self, symbol: str) -> SymbolInfo:
        self._need()
        d = _digits(symbol)
        bid, ask = self._quote(symbol)
        point = 10 ** -d
        return SymbolInfo(name=symbol, digits=d, point=point, tick_size=point,
                          tick_value=point * self._contract(symbol) * self._conv(symbol, bid),
                          volume_min=0.01, volume_max=100.0, volume_step=0.01, contract_size=self._contract(symbol),
                          bid=bid, ask=ask, spread_points=round((ask - bid) / point, 1), stops_level=10, trade_allowed=True)

    # ------------------------------------------------------------------ positions / orders
    def _to_position(self, t: int, p: dict) -> Position:
        bid, ask = self._quote(p["symbol"])
        return Position(ticket=t, symbol=p["symbol"], side=p["side"], volume=p["volume"], price_open=p["price_open"],
                        sl=p["sl"], tp=p["tp"], profit=self._pnl(p, bid if p["side"] == "buy" else ask),
                        time=p["time"], magic=p["magic"], comment=p["comment"])

    def get_positions(self, magic: Optional[int] = None) -> list[Position]:
        self._need()
        return [self._to_position(t, p) for t, p in self._positions.items() if magic is None or p["magic"] == magic]

    def place_order(self, symbol, side, volume, sl, tp, deviation=20, magic=0, comment="88Trader") -> OrderResult:
        self._need()
        self.orders_sent += 1
        if side not in ("buy", "sell"):
            return OrderResult(False, message="bad side")
        if not sl:
            return OrderResult(False, message="refused: stop loss is mandatory")
        info = self.symbol_info(symbol)
        if volume < info.volume_min - 1e-12 or volume > info.volume_max + 1e-12:
            return OrderResult(False, retcode=10014, message="invalid volume")
        steps = volume / info.volume_step
        if abs(steps - round(steps)) > 1e-6:
            return OrderResult(False, retcode=10014, message="invalid volume step")
        slip = self.slippage_points * info.point
        bid, ask = info.bid, info.ask
        price = ask + slip if side == "buy" else bid - slip
        if self.slippage_points > deviation:
            return OrderResult(False, retcode=10004, message="requote beyond deviation")
        min_dist = info.stops_level * info.point
        if side == "buy":
            bad = sl >= bid - min_dist or (tp and tp <= bid + min_dist)
        else:
            bad = sl <= ask + min_dist or (tp and tp >= ask - min_dist)
        if bad:
            return OrderResult(False, retcode=10016, message="invalid stops")
        t = next(self._ticket)
        self._positions[t] = dict(symbol=symbol, side=side, volume=float(volume), price_open=price, sl=float(sl),
                                  tp=float(tp or 0.0), time=self._time, magic=magic, comment=comment)
        return OrderResult(True, ticket=t, price=price, volume=volume, retcode=10009)

    def _close(self, t: int, price: float, reason: str) -> ClosedTrade:
        p = self._positions.pop(t)
        profit = self._pnl(p, price) - self.commission_per_lot * p["volume"]  # second commission side
        self.balance += profit
        ct = ClosedTrade(ticket=t, symbol=p["symbol"], side=p["side"], volume=p["volume"], open_time=p["time"],
                         close_time=self._time, open_price=p["price_open"], close_price=price, profit=profit,
                         reason=reason)
        ct._magic = p["magic"]  # type: ignore[attr-defined]
        self._closed.append(ct)
        return ct

    def close_position(self, ticket: int, deviation: int = 20) -> OrderResult:
        self._need()
        if ticket not in self._positions:
            return OrderResult(False, message="position not found")
        p = self._positions[ticket]
        bid, ask = self._quote(p["symbol"])
        slip = self.slippage_points * 10 ** -_digits(p["symbol"])
        price = bid - slip if p["side"] == "buy" else ask + slip
        ct = self._close(ticket, price, "bot")
        return OrderResult(True, ticket=ticket, price=price, volume=ct.volume, retcode=10009)

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> OrderResult:
        self._need()
        if ticket not in self._positions:
            return OrderResult(False, message="position not found")
        if not sl:
            return OrderResult(False, message="refused: stop loss is mandatory")
        self._positions[ticket]["sl"] = float(sl)
        self._positions[ticket]["tp"] = float(tp or 0.0)
        return OrderResult(True, ticket=ticket, retcode=10009)

    def closed_trades(self, since: datetime, magic: Optional[int] = None) -> list[ClosedTrade]:
        self._need()
        return [c for c in self._closed if c.close_time >= since and (magic is None or getattr(c, "_magic", None) == magic)]

    # ------------------------------------------------------------------ simulation step
    def process_bar(self, symbol: Optional[str] = None) -> list[ClosedTrade]:
        """Apply the current bar's high/low to open positions (SL first when ambiguous)."""
        done: list[ClosedTrade] = []
        for t, p in list(self._positions.items()):
            if symbol and p["symbol"] != symbol:
                continue
            if p["symbol"] in self.fixed_quotes:
                continue
            bar = self._bar(p["symbol"])
            sp = self._spread_price(p["symbol"])
            pt = 10 ** -_digits(p["symbol"])
            sl_slip = (self.slippage_points + self.sl_extra_slippage_points) * pt
            if p["side"] == "buy":  # exits at BID
                hit_sl = bar["low"] <= p["sl"]
                hit_tp = bool(p["tp"]) and bar["high"] >= p["tp"]
                if hit_sl:
                    px = min(p["sl"], float(bar["open"])) - sl_slip
                    done.append(self._close(t, px, "sl"))
                elif hit_tp:
                    done.append(self._close(t, p["tp"], "tp"))
            else:  # sell exits at ASK = bid + spread
                hit_sl = bar["high"] + sp >= p["sl"]
                hit_tp = bool(p["tp"]) and bar["low"] + sp <= p["tp"]
                if hit_sl:
                    px = max(p["sl"], float(bar["open"]) + sp) + sl_slip
                    done.append(self._close(t, px, "sl"))
                elif hit_tp:
                    done.append(self._close(t, p["tp"], "tp"))
        return done

    def force_close_all_at_end(self) -> None:
        for t in list(self._positions):
            p = self._positions[t]
            bid, ask = self._quote(p["symbol"])
            self._close(t, bid if p["side"] == "buy" else ask, "end")
