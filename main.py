"""88 Trader entry point:  python main.py   (use run.bat on Windows)."""
from __future__ import annotations

import logging
import os
import sys
import threading

from config import Config, load_dotenv
from utils import setup_logging

EXIT_NO_TOKEN = 2
EXIT_NO_PASSWORD = 3
EXIT_BAD_CONFIG = 4


def build_broker(cfg: Config):
    from broker import MT5Broker
    pw = os.environ.get("MT5_PASSWORD", "")
    return MT5Broker(login=int(cfg.get("mt5.login", 0)), password=pw, server=str(cfg.get("mt5.server", "")),
                     path=str(cfg.get("mt5.path", "") or ""), timeout_ms=int(cfg.get("mt5.timeout_ms", 60000)),
                     symbol_suffix=str(cfg.get("mt5.symbol_suffix", "") or ""),
                     utc_offset_hours=cfg.get("mt5.server_utc_offset_hours", "auto"))


def main(argv: list[str] | None = None, broker=None, api=None) -> int:
    load_dotenv()
    from telegram_bot import (NO_TOKEN_MESSAGE, ControlBot, TelegramAPI, get_token)
    try:
        cfg = Config()
    except Exception as e:
        print(f"config.yaml problem: {type(e).__name__}: {e}")
        return EXIT_BAD_CONFIG
    setup_logging(cfg.log_file)
    log = logging.getLogger("88trader.main")

    token = get_token()
    if not token:
        print(NO_TOKEN_MESSAGE)
        log.warning("TRADER_TELEGRAM_BOT_TOKEN not set - not starting")
        return EXIT_NO_TOKEN
    if broker is None:
        if not os.environ.get("MT5_PASSWORD"):
            print("MT5_PASSWORD تنظیم نشده است. آن را در فایل .env بنویسید.\nMT5_PASSWORD is not set - put it in .env (see .env.example).")
            return EXIT_NO_PASSWORD
        if not int(cfg.get("mt5.login", 0)):
            print("mt5.login در config.yaml پر نشده است. / mt5.login is not set in config.yaml.")
            return EXIT_BAD_CONFIG
        broker = build_broker(cfg)

    from engine import Engine
    from state import State
    state = State(cfg.data_dir / "state.json")
    bot_holder: dict = {}

    def notify(kind: str, text: str) -> None:
        b = bot_holder.get("bot")
        if b:
            b.notify(kind, text)

    engine = Engine(cfg, state, broker, notify=notify)
    bot = ControlBot(api or TelegramAPI(token), cfg, state, engine)
    bot_holder["bot"] = bot
    stop = threading.Event()
    pid_file = cfg.data_dir / "88trader.pid"
    pid_file.write_text(str(os.getpid()))

    def engine_thread() -> None:
        started = False
        backoff = 5.0
        while not stop.is_set():
            if not started:
                try:
                    d = engine.startup()
                    started = True
                    if d:
                        if state.get("paused", True):
                            bot.notify("info", "✅ 88 Trader آماده است (حالت: توقف). برای شروع ▶️ را بزن.\n88 Trader is ready (paused). Press ▶️ to start.")
                        else:
                            bot.notify("info", "🔁 88 Trader دوباره راه‌اندازی شد و فعال است.\n88 Trader restarted and is RUNNING (state restored).")
                    else:
                        bot.notify("error", f"⛔ {d.reason}")
                except Exception as e:
                    engine.last_error = f"startup failed: {e}"
                    log.error("startup failed: %s", e)
                    bot.notify("error", f"⚠️ اتصال به MT5 ناموفق / MT5 startup failed: {e}\n"
                                        "MT5 باز و لاگین باشد؟ Is MT5 open and logged in?")
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 300)
                    continue
            engine.cycle()
            stop.wait(float(cfg.get("engine.poll_seconds", 20)))

    t = threading.Thread(target=engine_thread, daemon=True, name="engine")
    t.start()
    log.info("88 Trader started (paused until the owner presses Start)")
    print("88 Trader is running. Talk to your bot in Telegram. Press Ctrl+C to stop.")
    try:
        bot.run_forever(stop)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass
        stop.set()
        t.join(timeout=10)
        try:
            broker.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
