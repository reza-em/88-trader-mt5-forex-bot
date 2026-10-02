"""Risk manager. Mandatory: the engine cannot place an order without passing through here,
and nothing in Telegram can switch it off or raise its hard caps (see config.EDITABLE / config.sanitize)."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import (Config, HARD_MAX_DAILY_LOSS, HARD_MAX_POSITIONS, HARD_MAX_RISK_PER_TRADE, HARD_MAX_TOTAL_DD)
from models import AccountInfo, Position, SymbolInfo
from news import NewsFilter
from state import State

log = logging.getLogger("88trader.risk")

LIVE_CONFIRM_PHRASE = "I CONFIRM LIVE TRADING"
SAFE_MODES = ("demo", "contest")


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    code: str = ""

    def __bool__(self) -> bool:
        return self.ok


def day_key(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def round_down_to_step(x: float, step: float, digits: int = 8) -> float:
    if step <= 0:
        return x
    return round(math.floor(x / step + 1e-9) * step, digits)


def lot_size(equity: float, risk_pct: float, sl_distance: float, si: SymbolInfo) -> float:
    """Volume so that hitting the SL loses ~risk_pct% of equity. Returns 0.0 if even the minimum
    lot would risk more than the allowed amount (we never round UP beyond the risk budget)."""
    risk_pct = min(max(risk_pct, 0.0), HARD_MAX_RISK_PER_TRADE)
    if equity <= 0 or sl_distance <= 0 or si.tick_size <= 0 or si.tick_value <= 0:
        return 0.0
    risk_money = equity * risk_pct / 100.0
    loss_per_lot = (sl_distance / si.tick_size) * si.tick_value
    if loss_per_lot <= 0:
        return 0.0
    raw = risk_money / loss_per_lot
    vol = round_down_to_step(raw, si.volume_step)
    vol = min(vol, si.volume_max)
    if vol < si.volume_min - 1e-12:
        return 0.0
    return round(vol, 8)


class RiskManager:
    def __init__(self, cfg: Config, state: State, news: Optional[NewsFilter] = None):
        self.cfg, self.state = cfg, state
        self.news = news or NewsFilter(False)

    # ------------------------------------------------------------ account type
    def check_account_mode(self, acct: AccountInfo) -> Decision:
        """DEMO/contest always allowed. REAL only with allow_live (hand-edited) + real + Telegram phrase."""
        mode = (acct.trade_mode or "unknown").lower()
        if mode in SAFE_MODES:
            return Decision(True)
        if mode == "real":
            if self.cfg.get("risk.allow_live") is not True:
                return Decision(False, "REAL account detected but allow_live is false in config.yaml", "live_refused")
            if not self.state.get("live_confirmed", False):
                return Decision(False, "REAL account: waiting for the confirmation phrase in Telegram", "live_unconfirmed")
            return Decision(True)
        return Decision(False, f"account type '{mode}' is unknown - refusing to trade", "mode_unknown")

    def confirm_live(self, acct: AccountInfo, phrase: str) -> Decision:
        if (acct.trade_mode or "").lower() != "real":
            return Decision(False, "account is not real; no confirmation needed", "not_real")
        if self.cfg.get("risk.allow_live") is not True:
            return Decision(False, "allow_live is false in config.yaml (must be set by hand)", "live_refused")
        if phrase.strip() != LIVE_CONFIRM_PHRASE:
            return Decision(False, "wrong phrase", "bad_phrase")
        self.state.set("live_confirmed", True)
        log.warning("LIVE trading confirmed by owner for this session")
        return Decision(True)

    # ------------------------------------------------------------ equity tracking / halts
    def update(self, acct: AccountInfo, now: datetime) -> list[str]:
        """Track day start, peak equity; trip halts. Returns list of newly tripped events
        ('daily_loss', 'drawdown')."""
        events: list[str] = []
        eq = acct.equity
        dk = day_key(now)
        if self.state.get("day_key") != dk:
            self.state.update(day_key=dk, day_start_equity=eq)
        peak = self.state.get("peak_equity")
        if peak is None or eq > peak:
            self.state.set("peak_equity", eq)
            peak = eq
        start = self.state.get("day_start_equity") or eq
        max_daily = min(float(self.cfg.get("risk.max_daily_loss_pct", 3.0)), HARD_MAX_DAILY_LOSS)
        if start > 0 and self.state.get("halt_daily_day") != dk:
            if (start - eq) / start * 100.0 >= max_daily:
                self.state.set("halt_daily_day", dk)
                events.append("daily_loss")
                log.warning("DAILY LOSS LIMIT hit: %.2f%% -> no new trades until next UTC day", (start - eq) / start * 100)
        max_dd = min(float(self.cfg.get("risk.max_total_drawdown_pct", 10.0)), HARD_MAX_TOTAL_DD)
        if peak and peak > 0 and not self.state.get("halt_drawdown", False):
            if (peak - eq) / peak * 100.0 >= max_dd:
                self.state.set("halt_drawdown", True)
                events.append("drawdown")
                log.warning("MAX DRAWDOWN hit: %.2f%% -> trading halted until manual reset", (peak - eq) / peak * 100)
        return events

    def halted(self, now: datetime) -> Decision:
        if self.state.get("halt_drawdown", False):
            return Decision(False, "max drawdown reached - halted until manual reset (reset_halt.bat)", "halt_dd")
        if self.state.get("halt_daily_day") == day_key(now):
            return Decision(False, "daily loss limit reached - no trading until next day", "halt_daily")
        return Decision(True)

    def drawdown_pct(self, equity: float) -> float:
        peak = self.state.get("peak_equity") or equity
        return max(0.0, (peak - equity) / peak * 100.0) if peak else 0.0

    def daily_pnl(self, equity: float) -> float:
        start = self.state.get("day_start_equity")
        return equity - start if start else 0.0

    # ------------------------------------------------------------ pre-trade gate
    def can_open(self, symbol: str, side: str, acct: AccountInfo, positions: list[Position],
                 si: SymbolInfo, now: datetime) -> Decision:
        d = self.check_account_mode(acct)
        if not d:
            return d
        d = self.halted(now)
        if not d:
            return d
        if not si.trade_allowed:
            return Decision(False, f"{symbol}: trading not allowed right now", "symbol_closed")
        mine = [p for p in positions if p.magic == self.cfg.get("risk.magic")]
        max_pos = min(int(self.cfg.get("risk.max_open_positions", 3)), HARD_MAX_POSITIONS)
        if len(mine) >= max_pos:
            return Decision(False, f"max open positions ({max_pos}) reached", "max_positions")
        if any(p.symbol == symbol for p in positions if p.magic == self.cfg.get("risk.magic")):
            return Decision(False, f"{symbol}: already has an open position (one per symbol, no averaging)", "one_per_symbol")
        max_spread = self.cfg.max_spread(symbol)
        if si.spread_points > max_spread:
            return Decision(False, f"{symbol}: spread {si.spread_points} > max {max_spread}", "spread")
        blocked, why = self.news.is_blocked(symbol, now)
        if blocked:
            return Decision(False, f"news pause: {why}", "news")
        times = [t for t in self.state.get("order_times", []) if now.timestamp() - t < 3600]
        gap = float(self.cfg.get("risk.min_seconds_between_orders", 10))
        if times and now.timestamp() - max(times) < gap:
            return Decision(False, "rate limit: orders too close together", "rate_gap")
        if len(times) >= int(self.cfg.get("risk.max_orders_per_hour", 6)):
            return Decision(False, "rate limit: max orders per hour reached", "rate_hour")
        if acct.margin_free <= 0:
            return Decision(False, "no free margin", "margin")
        return Decision(True)

    def record_order(self, now: datetime) -> None:
        times = [t for t in self.state.get("order_times", []) if now.timestamp() - t < 3600]
        times.append(now.timestamp())
        self.state.set("order_times", times)

    def validate_order(self, side: str, entry: float, sl: float, tp: float, si: SymbolInfo) -> Decision:
        if not sl or sl <= 0:
            return Decision(False, "stop loss is mandatory", "no_sl")
        if not tp or tp <= 0:
            return Decision(False, "take profit missing", "no_tp")
        if side == "buy" and not (sl < entry < tp):
            return Decision(False, "invalid SL/TP for buy", "bad_levels")
        if side == "sell" and not (tp < entry < sl):
            return Decision(False, "invalid SL/TP for sell", "bad_levels")
        if abs(entry - sl) < si.stops_level * si.point:
            return Decision(False, "SL closer than broker minimum stop distance", "stops_level")
        return Decision(True)

    def size(self, acct: AccountInfo, sl_distance: float, si: SymbolInfo) -> float:
        pct = min(float(self.cfg.get("risk.risk_per_trade_pct", 0.5)), HARD_MAX_RISK_PER_TRADE)
        return lot_size(acct.equity, pct, sl_distance, si)
