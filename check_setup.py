"""88 Trader - setup checker. Run:  python check_setup.py   (or double-click check_setup.bat)
Prints a green/red checklist. Never prints passwords or tokens."""
from __future__ import annotations

import os
import sys

from config import Config, load_dotenv

GREEN, RED, YELLOW, END = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
if os.name == "nt":
    os.system("")  # enables ANSI colours in Windows 10+ terminals


def line(ok, text, hint="", warn=False):
    mark = f"{YELLOW}[ ! ]{END}" if warn else (f"{GREEN}[ OK ]{END}" if ok else f"{RED}[FAIL]{END}")
    print(f"{mark} {text}")
    if not ok and hint:
        print(f"       -> {hint}")
    return ok


def run(broker=None, cfg: Config | None = None, out=print) -> bool:
    load_dotenv()
    cfg = cfg or Config()
    allok = True
    allok &= line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "Install Python 3.11 64-bit / پایتون ۳.۱۱ نصب کنید")
    allok &= line(sys.maxsize > 2**32, "Python is 64-bit", "MetaTrader5 package needs 64-bit Python")
    allok &= line(os.name == "nt", "Windows detected", "MT5 python package only works on Windows", warn=os.name != "nt")
    has_tok = bool(os.environ.get("TRADER_TELEGRAM_BOT_TOKEN"))
    allok &= line(has_tok, "TRADER_TELEGRAM_BOT_TOKEN is set", "create a bot with @BotFather and put the token in .env")
    has_pw = bool(os.environ.get("MT5_PASSWORD"))
    allok &= line(has_pw, "MT5_PASSWORD is set", "put MT5_PASSWORD=... in .env")
    login = int(cfg.get("mt5.login", 0) or 0)
    allok &= line(login > 0, f"config.yaml mt5.login = {login if login else '(empty)'}", "write your DEMO account number in config.yaml")
    allok &= line(bool(cfg.get("mt5.server")), f"config.yaml mt5.server = {cfg.get('mt5.server')}", "copy the exact server name from MT5")
    line(cfg.get("risk.allow_live") is not True, "allow_live is false (DEMO only)", "allow_live: true is dangerous!", warn=cfg.get("risk.allow_live") is True)

    if broker is None:
        try:
            from main import build_broker
            broker = build_broker(cfg)
        except Exception as e:
            allok &= line(False, f"cannot create MT5 broker: {e}")
            return bool(allok)
    try:
        broker.connect()
        allok &= line(True, "Connected to MT5 terminal and logged in")
    except Exception as e:
        allok &= line(False, f"MT5 connection failed: {e}",
                      "MT5 must be open & logged in; Tools > Options > Expert Advisors > 'Allow algorithmic trading'; "
                      "check login/server/password")
        return bool(allok)
    try:
        a = broker.account_info()
        allok &= line(True, f"Account {a.login} @ {a.server}  balance {a.balance:,.2f} {a.currency}  leverage 1:{a.leverage}")
        is_demo = a.trade_mode in ("demo", "contest")
        allok &= line(is_demo, f"Account type: {a.trade_mode.upper()}",
                      "This is a REAL account. The bot will refuse to trade unless you set allow_live by hand. Use a DEMO account!")
        terminal_ok = True
        try:
            ti = broker.mt5.terminal_info()
            terminal_ok = bool(ti.trade_allowed)
            allok &= line(terminal_ok, "Algo Trading button is ON in the terminal", "click the 'Algo Trading' button in the MT5 toolbar (make it green)")
            allok &= line(bool(ti.connected), "Terminal is connected to the broker server", "check internet / server name")
        except AttributeError:
            pass
    except Exception as e:
        allok &= line(False, f"account_info failed: {e}")
    for sym in cfg.get("trading.symbols", []):
        try:
            si = broker.symbol_info(sym)
            ok = si.trade_allowed
            line(ok, f"{broker.resolve_symbol(sym)}: spread {si.spread_points} pts (max {cfg.max_spread(sym)}), "
                     f"lot {si.volume_min}-{si.volume_max} step {si.volume_step}",
                 "trading disabled for this symbol right now (market closed?)", warn=not ok)
            if ok and si.spread_points > cfg.max_spread(sym):
                line(True, f"   spread currently above your max - bot would skip {sym} now", warn=True)
            rates = broker.get_rates(sym, cfg.get("trading.timeframe", "H1"), 300)
            allok &= line(len(rates) >= 250, f"{sym}: {len(rates)} candles available for {cfg.get('trading.timeframe')}",
                          "open the chart of this symbol in MT5 and scroll back to download history")
        except Exception as e:
            allok &= line(False, f"{sym}: not available ({e})",
                          "right-click Market Watch > Show All / set symbol_suffix in config.yaml (e.g. '.m')")
    try:
        broker.shutdown()
    except Exception:
        pass
    print()
    print((f"{GREEN}ALL GOOD - you can run run.bat{END}" if allok else f"{RED}Fix the red items above, then run this check again.{END}"))
    return bool(allok)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
