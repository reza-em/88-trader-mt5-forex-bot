"""Configuration: config.yaml (hand-edited) + data/overrides.json (Telegram edits) + .env loading.

Hard caps are enforced here AND in risk.py, so neither a typo in config.yaml nor a Telegram
message can push the risk above the limits.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger("88trader.config")

BASE_DIR = Path(__file__).resolve().parent

ALLOWED_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "XAUUSD"]
TIMEFRAMES = ["M15", "M30", "H1", "H4"]

# Hard caps (cannot be exceeded from yaml or Telegram)
HARD_MAX_RISK_PER_TRADE = 2.0
HARD_MIN_RISK_PER_TRADE = 0.05
HARD_MAX_DAILY_LOSS = 5.0
HARD_MAX_TOTAL_DD = 20.0      # yaml only; default 10; not editable from Telegram
HARD_MAX_POSITIONS = 5


# ---------------------------------------------------------------- .env
def load_dotenv(path: str | os.PathLike | None = None, override: bool = False) -> bool:
    """Tiny .env loader (KEY=VALUE, # comments, optional quotes). Never prints values."""
    p = Path(path) if path else BASE_DIR / ".env"
    if not p.is_file():
        return False
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and (override or k not in os.environ or not os.environ[k]):
            os.environ[k] = v
    return True


# ---------------------------------------------------------------- validators
def _num(lo: float, hi: float, integer: bool = False) -> Callable[[Any], Any]:
    def f(raw: Any):
        try:
            v = float(str(raw).strip().replace("٫", ".").replace(",", "."))
        except Exception:
            raise ValueError("عدد معتبر نیست / not a valid number")
        if v != v:  # NaN
            raise ValueError("عدد معتبر نیست / not a valid number")
        if integer:
            if abs(v - round(v)) > 1e-9:
                raise ValueError("باید عدد صحیح باشد / must be a whole number")
            v = int(round(v))
        if not (lo <= v <= hi):
            raise ValueError(f"مقدار باید بین {lo} و {hi} باشد / must be between {lo} and {hi}")
        return v
    return f


def _choice(options: list[str]) -> Callable[[Any], str]:
    def f(raw: Any):
        v = str(raw).strip().upper()
        opts = {o.upper(): o for o in options}
        if v not in opts:
            raise ValueError("گزینه‌های مجاز / allowed: " + ", ".join(options))
        return opts[v]
    return f


_SESSION_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")


def validate_sessions(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]
    else:
        parts = [str(p).strip() for p in raw]
    if not parts or len(parts) > 4:
        raise ValueError("۱ تا ۴ بازه مثل 07:00-16:00 / 1-4 ranges like 07:00-16:00")
    out = []
    for p in parts:
        m = _SESSION_RE.match(p)
        if not m:
            raise ValueError(f"قالب نادرست '{p}' — نمونه: 07:00-16:00 / bad format, e.g. 07:00-16:00")
        a = f"{int(m.group(1)):02d}:{m.group(2)}"
        b = f"{int(m.group(3)):02d}:{m.group(4)}"
        if a == b:
            raise ValueError("شروع و پایان نباید یکی باشد / start and end must differ")
        out.append(f"{a}-{b}")
    return out


def validate_symbols(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,;\s]+", raw) if p]
    out: list[str] = []
    for s in raw:
        s = str(s).upper().strip()
        if s not in ALLOWED_SYMBOLS:
            raise ValueError(f"نماد مجاز نیست / symbol not allowed: {s}. Allowed: {', '.join(ALLOWED_SYMBOLS)}")
        if s not in out:
            out.append(s)
    if not out:
        raise ValueError("حداقل یک نماد لازم است / at least one symbol is required")
    return out


# Keys editable from Telegram -> validator. Anything not here can only be changed by hand in config.yaml.
EDITABLE: dict[str, Callable[[Any], Any]] = {
    "risk.risk_per_trade_pct": _num(HARD_MIN_RISK_PER_TRADE, HARD_MAX_RISK_PER_TRADE),
    "risk.max_daily_loss_pct": _num(0.5, HARD_MAX_DAILY_LOSS),
    "risk.max_open_positions": _num(1, HARD_MAX_POSITIONS, integer=True),
    "trading.symbols": validate_symbols,
    "trading.timeframe": _choice(TIMEFRAMES),
    "trading.sessions_utc": validate_sessions,
    "strategy.mode": _choice(["rsi_pullback", "ema_cross"]),
    "strategy.rr": _num(1.0, 4.0),
    "strategy.atr_sl_mult": _num(0.8, 4.0),
    "strategy.rsi_buy_level": _num(20, 50),
    "strategy.rsi_sell_level": _num(50, 80),
    "telegram.language": _choice(["fa", "en"]),
}

# Short aliases used by /set
ALIASES = {
    "risk": "risk.risk_per_trade_pct",
    "dailyloss": "risk.max_daily_loss_pct",
    "maxpos": "risk.max_open_positions",
    "symbols": "trading.symbols",
    "tf": "trading.timeframe",
    "timeframe": "trading.timeframe",
    "sessions": "trading.sessions_utc",
    "mode": "strategy.mode",
    "rr": "strategy.rr",
    "atr": "strategy.atr_sl_mult",
    "rsibuy": "strategy.rsi_buy_level",
    "rsisell": "strategy.rsi_sell_level",
    "lang": "telegram.language",
}


# ---------------------------------------------------------------- helpers
def _deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _set_path(d: dict, dotted: str, value: Any) -> None:
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _get_path(d: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = d
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def sanitize(data: dict) -> dict:
    """Clamp everything to hard caps and drop disallowed symbols. Returns a cleaned copy."""
    d = copy.deepcopy(data)
    r = d.setdefault("risk", {})

    def clamp(key: str, lo: float, hi: float, default: float):
        try:
            v = float(r.get(key, default))
        except Exception:
            v = default
        nv = min(max(v, lo), hi)
        if nv != v:
            log.warning("config risk.%s=%s out of allowed range, using %s", key, v, nv)
        r[key] = nv

    clamp("risk_per_trade_pct", HARD_MIN_RISK_PER_TRADE, HARD_MAX_RISK_PER_TRADE, 0.5)
    clamp("max_daily_loss_pct", 0.5, HARD_MAX_DAILY_LOSS, 3.0)
    clamp("max_total_drawdown_pct", 1.0, HARD_MAX_TOTAL_DD, 10.0)
    r["max_open_positions"] = int(min(max(int(r.get("max_open_positions", 3)), 1), HARD_MAX_POSITIONS))
    r["max_slippage_points"] = int(min(max(int(r.get("max_slippage_points", 20)), 0), 200))
    r["allow_live"] = r.get("allow_live") is True  # only a literal YAML true counts
    r["magic"] = int(r.get("magic", 880088))

    t = d.setdefault("trading", {})
    syms = []
    for s in t.get("symbols", []) or []:
        s = str(s).upper()
        if s in ALLOWED_SYMBOLS:
            if s not in syms:
                syms.append(s)
        else:
            log.warning("symbol %s is not in the allowed major list and was ignored", s)
    t["symbols"] = syms
    tf = str(t.get("timeframe", "H1")).upper()
    t["timeframe"] = tf if tf in TIMEFRAMES else "H1"
    try:
        t["sessions_utc"] = validate_sessions(t.get("sessions_utc") or ["07:00-16:00"])
    except ValueError:
        t["sessions_utc"] = ["07:00-16:00"]
    return d


# ---------------------------------------------------------------- Config
class Config:
    def __init__(self, path: str | os.PathLike | None = None, base_dir: str | os.PathLike | None = None,
                 data: dict | None = None):
        """`data` (optional) replaces config.yaml entirely (used by tests/backtest)."""
        self.path = Path(path) if path else BASE_DIR / "config.yaml"
        self.base_dir = Path(base_dir) if base_dir else self.path.parent
        self._lock = threading.RLock()
        self._base: dict = {}
        self._overrides: dict = {}
        self.data: dict = {}
        self._inline = copy.deepcopy(data) if data is not None else None
        self.reload()

    # paths
    @property
    def data_dir(self) -> Path:
        p = Path(self.get("paths.data_dir", "data"))
        p = p if p.is_absolute() else self.base_dir / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_file(self) -> Path:
        p = Path(self.get("paths.log_file", "logs/88trader.log"))
        return p if p.is_absolute() else self.base_dir / p

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / "overrides.json"

    def reload(self) -> None:
        with self._lock:
            if self._inline is not None:
                self._base = copy.deepcopy(self._inline)
            else:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._base = yaml.safe_load(f) or {}
            self._overrides = {}
            op = self.base_dir / Path(_get_path(self._base, "paths.data_dir", "data")) / "overrides.json"
            if op.is_file():
                try:
                    raw = json.loads(op.read_text(encoding="utf-8"))
                    for k, v in raw.items():
                        if k in EDITABLE:
                            try:
                                self._overrides[k] = EDITABLE[k](v)
                            except ValueError:
                                log.warning("ignoring invalid override %s", k)
                        else:
                            log.warning("ignoring non-editable override key %s", k)
                except Exception as e:  # corrupted file must never crash the bot
                    log.warning("could not read overrides.json: %s", e)
            self._rebuild()

    def _rebuild(self) -> None:
        merged = copy.deepcopy(self._base)
        for k, v in self._overrides.items():
            _set_path(merged, k, v)
        self.data = sanitize(merged)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            return _get_path(self.data, dotted, default)

    def max_spread(self, symbol: str) -> float:
        ms = self.get("trading.max_spread_points", {}) or {}
        if isinstance(ms, dict):
            return float(ms.get(symbol, ms.get("default", 25)))
        return float(ms)

    def set_override(self, key: str, raw: Any) -> Any:
        """Validate + persist a Telegram edit. Raises ValueError on bad input / non-editable key."""
        key = ALIASES.get(key.lower(), key)
        if key not in EDITABLE:
            raise ValueError("این تنظیم از تلگرام قابل تغییر نیست / this setting cannot be changed from Telegram")
        value = EDITABLE[key](raw)
        with self._lock:
            self._overrides[key] = value
            self._rebuild()
            self._persist()
        log.info("setting changed: %s", key)
        return value

    def _persist(self) -> None:
        p = self.overrides_path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._overrides, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def toggle_symbol(self, symbol: str) -> list[str]:
        symbol = symbol.upper()
        cur = list(self.get("trading.symbols", []))
        if symbol in cur:
            cur.remove(symbol)
        else:
            cur.append(symbol)
        if not cur:
            raise ValueError("حداقل یک نماد باید روشن باشد / at least one symbol must stay on")
        self.set_override("trading.symbols", cur)
        return cur
