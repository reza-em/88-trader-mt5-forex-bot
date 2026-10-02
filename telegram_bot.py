"""88 Trader - Telegram control bot (plain `requests`, long polling, bilingual fa/en).

Security: only the owner (numeric id bound on the first /start from @example_owner) is ever served.
Everyone else gets one polite refusal and nothing else. The token is read from the environment only
and is never logged (exceptions are logged by type, not text, because requests embeds the URL).
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from config import (ALLOWED_SYMBOLS, EDITABLE, HARD_MAX_DAILY_LOSS, HARD_MAX_POSITIONS, HARD_MAX_RISK_PER_TRADE,
                    TIMEFRAMES, Config)
from engine import Engine
from models import BrokerError
from risk import LIVE_CONFIRM_PHRASE
from state import State

log = logging.getLogger("88trader.telegram")

OWNER_USERNAME_DEFAULT = "example_owner"
TOKEN_ENV = "TRADER_TELEGRAM_BOT_TOKEN"
TEHRAN = timezone(timedelta(hours=3, minutes=30))

# ------------------------------------------------------------------ texts
TX: dict[str, tuple[str, str]] = {
    "welcome": ("سلام {name}! 👋 من «۸۸ تریدر» هستم 🤖💹\nمن فقط روی حساب دمو کار می‌کنم و هیچ سودی را تضمین نمی‌کنم.\nاز دکمه‌های زیر استفاده کن 👇",
                "Hi {name}! 👋 I'm 88 Trader 🤖💹\nI run on a DEMO account and never promise profit.\nUse the buttons below 👇"),
    "not_owner": ("این ربات خصوصی است. 🔒", "This bot is private. 🔒"),
    "owner_bound": ("✅ مالک ربات ثبت شد. ربات در حالت ⏸ توقف است؛ وقتی آماده بودی ▶️ را بزن.",
                    "✅ Owner registered. The bot starts ⏸ paused; press ▶️ when you're ready."),
    "menu": ("منوی اصلی 🏠", "Main menu 🏠"),
    "started": ("▶️ ربات فعال شد. (فقط اگر همه‌ی شرایط ایمنی برقرار باشد معامله می‌کند)", "▶️ Bot is running (trades only if every safety check passes)."),
    "paused": ("⏸ ربات متوقف شد. معاملات باز دست‌نخورده می‌مانند (با حد ضرر).", "⏸ Bot paused. Open trades stay open (they have stop losses)."),
    "start_blocked": ("⛔ شروع ممکن نیست: {why}", "⛔ Cannot start: {why}"),
    "halted_note": ("⚠️ توجه: {why}", "⚠️ Note: {why}"),
    "estop_ask": ("🛑 مطمئنی؟ همه‌ی معاملات باز این ربات بسته می‌شود و ربات متوقف می‌شود.",
                  "🛑 Are you sure? This closes ALL open positions of this bot and pauses it."),
    "estop_done": ("🛑 توقف اضطراری انجام شد. بسته‌شده: {ok}، ناموفق: {bad}. ربات متوقف است.",
                   "🛑 Emergency stop done. Closed: {ok}, failed: {bad}. Bot is paused."),
    "cancelled": ("لغو شد ↩️", "Cancelled ↩️"),
    "yes_stop": ("✅ بله، ببند", "✅ Yes, close all"),
    "no": ("↩️ خیر", "↩️ No"),
    "no_positions": ("هیچ معامله‌ی بازی نیست 😴", "No open positions 😴"),
    "no_trades": ("هنوز معامله‌ای ثبت نشده 🗒", "No trades logged yet 🗒"),
    "conn_err": ("⚠️ اتصال به MT5 برقرار نیست: {e}", "⚠️ MT5 not reachable: {e}"),
    "set_prompt_num": ("مقدار جدید «{label}» را بفرست (عدد). لغو: /cancel", "Send the new value for “{label}” (a number). Cancel: /cancel"),
    "set_prompt_text": ("مقدار جدید «{label}» را بفرست. نمونه: {example}. لغو: /cancel", "Send the new value for “{label}”. Example: {example}. Cancel: /cancel"),
    "set_ok": ("✅ {label} = {value}", "✅ {label} = {value}"),
    "set_bad": ("❌ {e}", "❌ {e}"),
    "set_usage": ("استفاده: /set کلید مقدار\nکلیدها: {keys}", "Usage: /set key value\nKeys: {keys}"),
    "cant_disable": ("🔐 مدیریت ریسک اجباری است و از تلگرام خاموش نمی‌شود. سقف‌ها: ریسک هر معامله ≤ {r}٪، ضرر روزانه ≤ {d}٪، معاملات همزمان ≤ {p}.",
                     "🔐 The risk manager is mandatory and can't be turned off from Telegram. Hard caps: risk/trade ≤ {r}%, daily loss ≤ {d}%, positions ≤ {p}."),
    "live_need": ("🔴 حساب REAL شناسایی شد. برای ادامه باید allow_live: true را خودت در config.yaml بگذاری و سپس این عبارت را دقیقاً بفرستی:\n{phrase}",
                  "🔴 A REAL account was detected. You must set allow_live: true by hand in config.yaml and then send exactly:\n{phrase}"),
    "live_ok": ("⚠️ تأیید شد. معامله روی حساب واقعی برای همین اجرا فعال است. ریسک با خودت!", "⚠️ Confirmed. Live trading is enabled for this run only. Your risk!"),
    "live_no": ("❌ تأیید رد شد: {why}", "❌ Confirmation refused: {why}"),
    "notify_on": ("🔔 اعلان معاملات روشن شد", "🔔 Trade notifications ON"),
    "notify_off": ("🔕 اعلان معاملات خاموش شد (خطاها و توقف‌ها همیشه ارسال می‌شوند)", "🔕 Trade notifications OFF (errors and halts are always sent)"),
    "lang_set": ("🌐 زبان: فارسی", "🌐 Language: English"),
    "csv_caption": ("📝 گزارش معاملات", "📝 Trade log"),
    "unknown": ("متوجه نشدم 🤔 /menu را بزن.", "I didn't get that 🤔 try /menu."),
    "settings_title": ("⚙️ تنظیمات — روی هر مورد بزن:", "⚙️ Settings — tap an item:"),
    "pick_tf": ("⏱ تایم‌فریم را انتخاب کن:", "⏱ Pick a timeframe:"),
    "pick_mode": ("🧠 روش ورود را انتخاب کن:", "🧠 Pick the entry mode:"),
    "symbols_title": ("💱 نمادها (روشن/خاموش). طلا به‌طور پیش‌فرض خاموش است.", "💱 Symbols (toggle). Gold is off by default."),
}

DISCLAIMER = ("⚠️ هشدار ریسک / Risk disclaimer\n\n"
              "فارسی: معامله در فارکس و طلا ریسک بالایی دارد و می‌توانید بخشی یا همه‌ی سرمایه را از دست بدهید. "
              "این ربات هیچ سودی را تضمین نمی‌کند؛ نتایج بک‌تست و دمو تضمینی برای آینده نیستند. "
              "فقط با حساب دمو استفاده کنید و تا وقتی کاملاً مطمئن نیستید سراغ پول واقعی نروید. مسئولیت همه‌ی تصمیم‌ها با خود شماست؛ این پیام توصیه‌ی مالی نیست.\n\n"
              "English: Trading forex and gold is highly risky and you can lose some or all of your money. "
              "This bot does NOT guarantee profit; backtest and demo results are no guarantee of future results. "
              "Use a DEMO account only, and do not go near real money until you fully understand the risks. "
              "You are responsible for every decision; this is not financial advice.")

SETTING_LABELS = {
    "risk.risk_per_trade_pct": ("ریسک هر معامله ٪", "Risk per trade %"),
    "risk.max_daily_loss_pct": ("حداکثر ضرر روزانه ٪", "Max daily loss %"),
    "risk.max_open_positions": ("حداکثر معاملات باز", "Max open positions"),
    "trading.sessions_utc": ("ساعت معامله (UTC)", "Trading sessions (UTC)"),
    "trading.timeframe": ("تایم‌فریم", "Timeframe"),
    "strategy.mode": ("روش ورود", "Entry mode"),
    "trading.symbols": ("نمادها", "Symbols"),
}
SETTING_EXAMPLES = {"trading.sessions_utc": "07:00-16:00 or 07:00-11:00,13:00-17:00"}


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


class TelegramAPI:
    """Minimal Bot API client. `session` can be replaced in tests."""

    def __init__(self, token: str, session: Optional[requests.Session] = None, timeout: int = 30):
        self._token = token
        self.s = session or requests.Session()
        self.timeout = timeout

    def call(self, method: str, payload: Optional[dict] = None, files: Optional[dict] = None,
             http_timeout: Optional[float] = None) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            if files:
                r = self.s.post(url, data=payload or {}, files=files, timeout=http_timeout or 60)
            else:
                r = self.s.post(url, json=payload or {}, timeout=http_timeout or (self.timeout + 10))
            data = r.json()
        except Exception as e:  # do NOT log str(e): it contains the URL with the token
            raise ConnectionError(f"telegram {method} failed: {type(e).__name__}") from None
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} error {data.get('error_code')}: {data.get('description')}")
        return data["result"]


class ControlBot:
    def __init__(self, api: TelegramAPI, cfg: Config, state: State, engine: Engine,
                 owner_username: Optional[str] = None):
        self.api, self.cfg, self.state, self.engine = api, cfg, state, engine
        self.owner_username = (owner_username or cfg.get("telegram.owner_username") or OWNER_USERNAME_DEFAULT).lstrip("@").lower()
        self._offset = 0
        self._stop = threading.Event()
        self._strangers: dict[int, float] = {}

    # ------------------------------------------------------------ i18n
    @property
    def lang(self) -> str:
        return self.state.get("language") or self.cfg.get("telegram.language", "fa")

    def t(self, key: str, **kw: Any) -> str:
        fa, en = TX[key]
        s = fa if self.lang == "fa" else en
        return s.format(**kw) if kw else s

    def L(self, fa: str, en: str) -> str:
        return fa if self.lang == "fa" else en

    # ------------------------------------------------------------ sending
    @property
    def owner_id(self) -> Optional[int]:
        return self.state.get("owner_id")

    def send(self, text: str, markup: Optional[list] = None, chat_id: Optional[int] = None) -> Any:
        cid = chat_id or self.owner_id
        if not cid:
            return None
        payload: dict[str, Any] = {"chat_id": cid, "text": text[:4000]}
        if markup is not None:
            payload["reply_markup"] = {"inline_keyboard": markup}
        try:
            return self.api.call("sendMessage", payload)
        except Exception as e:
            log.warning("send failed: %s", e)
            return None

    def edit(self, chat_id: int, message_id: int, text: str, markup: Optional[list] = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
        if markup is not None:
            payload["reply_markup"] = {"inline_keyboard": markup}
        try:
            self.api.call("editMessageText", payload)
        except Exception as e:
            log.info("edit failed (%s) - sending new message", e)
            self.send(text, markup, chat_id)

    def notify(self, kind: str, text: str) -> None:
        """Engine -> owner notifications. Trade open/close can be muted; errors/halts cannot."""
        if kind in ("open", "close") and not self.state.get("notify_trades", True):
            return
        self.send(text)

    # ------------------------------------------------------------ keyboards
    def main_menu(self) -> list:
        paused = self.state.get("paused", True)
        return [
            [_btn(self.L("⏸ توقف", "⏸ Pause") if not paused else self.L("▶️ شروع", "▶️ Start"), "pause" if not paused else "start"),
             _btn(self.L("🛑 توقف اضطراری", "🛑 Emergency stop"), "estop")],
            [_btn(self.L("📊 وضعیت", "📊 Status"), "status"), _btn(self.L("📈 گزارش", "📈 Reports"), "reports")],
            [_btn(self.L("⚙️ تنظیمات", "⚙️ Settings"), "settings"), _btn(self.L("🧪 حالت", "🧪 Mode"), "mode")],
            [_btn(self.L("📝 خروجی CSV", "📝 Export CSV"), "csv"),
             _btn(self.L("🔕 اعلان‌ها", "🔔 Alerts") if self.state.get("notify_trades", True) else self.L("🔔 اعلان‌ها", "🔕 Alerts"), "notify")],
            [_btn(self.L("❓ راهنما", "❓ Help"), "help"), _btn("🌐 فارسی/English", "lang")],
        ]

    def back(self) -> list:
        return [_btn(self.L("🏠 منو", "🏠 Menu"), "menu")]

    def settings_menu(self) -> list:
        g = self.cfg.get
        rows = [
            [_btn(f"🎯 {self.L('ریسک', 'Risk')}: {g('risk.risk_per_trade_pct')}%", "set:risk.risk_per_trade_pct")],
            [_btn(f"📉 {self.L('ضرر روزانه', 'Daily loss')}: {g('risk.max_daily_loss_pct')}%", "set:risk.max_daily_loss_pct")],
            [_btn(f"🧮 {self.L('حداکثر معاملات', 'Max positions')}: {g('risk.max_open_positions')}", "set:risk.max_open_positions")],
            [_btn(f"⏱ {self.L('تایم‌فریم', 'Timeframe')}: {g('trading.timeframe')}", "tfmenu")],
            [_btn(f"🕐 {self.L('ساعت معامله', 'Sessions')}: {','.join(g('trading.sessions_utc'))}", "set:trading.sessions_utc")],
            [_btn(f"💱 {self.L('نمادها', 'Symbols')}: {','.join(g('trading.symbols'))}", "symbols")],
            [_btn(f"🧠 {self.L('روش', 'Mode')}: {g('strategy.mode')}", "modemenu")],
            self.back(),
        ]
        return rows

    def symbols_menu(self) -> list:
        on = set(self.cfg.get("trading.symbols", []))
        rows, row = [], []
        for s in ALLOWED_SYMBOLS:
            row.append(_btn(("✅ " if s in on else "⬜ ") + s, "sym:" + s))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([_btn(self.L("↩️ تنظیمات", "↩️ Settings"), "settings")])
        return rows

    # ------------------------------------------------------------ status text
    def status_text(self) -> str:
        eng = self.engine
        with eng._lock:
            try:
                if not eng.broker.is_connected():
                    eng.ensure_connected()
                a = eng.broker.account_info()
                pos = eng.broker.get_positions(eng.magic)
            except BrokerError as e:
                return self.t("conn_err", e=e)
        now = eng.broker.now()
        eng.risk.update(a, now)
        live = a.trade_mode == "real"
        mode = "🔴 LIVE (REAL)" if live else ("🟢 DEMO" if a.trade_mode == "demo" else f"🟡 {a.trade_mode.upper()}")
        paused = self.state.get("paused", True)
        run = self.L("⏸ متوقف", "⏸ Paused") if paused else self.L("▶️ فعال", "▶️ Running")
        lines = [f"🤖 88 Trader — {run}", f"🧪 {self.L('حالت', 'Mode')}: {mode}",
                 f"🏦 {a.login} @ {a.server}",
                 f"💰 {self.L('بالانس', 'Balance')}: {a.balance:,.2f} {a.currency}",
                 f"📊 Equity: {a.equity:,.2f} | Margin: {a.margin:,.2f} | Free: {a.margin_free:,.2f}",
                 f"📅 {self.L('سود/زیان امروز', 'Today P/L')}: {eng.risk.daily_pnl(a.equity):+,.2f}",
                 f"📉 {self.L('افت از اوج', 'Drawdown')}: {eng.risk.drawdown_pct(a.equity):.2f}%"]
        h = eng.risk.halted(now)
        if not h:
            lines.append(self.t("halted_note", why=h.reason))
        if eng.last_block_reason and not paused:
            lines.append(f"ℹ️ {eng.last_block_reason}")
        if eng.last_error:
            lines.append(f"⚠️ {eng.last_error[:200]}")
        lines.append("")
        if not pos:
            lines.append(self.t("no_positions"))
        else:
            lines.append(f"📌 {self.L('معاملات باز', 'Open positions')} ({len(pos)}):")
            tot = 0.0
            for p in pos:
                tot += p.profit
                ic = "🟢" if p.profit >= 0 else "🔴"
                lines.append(f"{ic} {p.symbol} {p.side.upper()} {p.volume:g} @ {p.price_open:g} | SL {p.sl:g} TP {p.tp:g} | {p.profit:+.2f}")
            lines.append(f"Σ {tot:+.2f}")
        return "\n".join(lines)

    def report_text(self, kind: str) -> str:
        now = self.engine.broker.now()
        if kind == "today":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            title = self.L("📈 گزارش امروز (UTC)", "📈 Today's report (UTC)")
        else:
            since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            title = self.L("📅 گزارش هفته (از دوشنبه، UTC)", "📅 Weekly report (since Monday, UTC)")
        r = self.engine.report(since)
        if r["trades"] == 0:
            return f"{title}\n{self.t('no_trades')}"
        return (f"{title}\n🔢 {self.L('تعداد', 'Trades')}: {r['trades']} (✅ {r['wins']} / ❌ {r['losses']})\n"
                f"🎯 Win rate: {r['win_rate']:.0f}%\n💵 P/L: {r['pnl']:+,.2f}\n\n"
                + self.L("توجه: نتایج گذشته تضمینی برای آینده نیست.", "Note: past results don't guarantee future results."))

    def mode_text(self) -> str:
        eng = self.engine
        try:
            with eng._lock:
                a = eng.broker.account_info()
        except BrokerError as e:
            return self.t("conn_err", e=e)
        allow = self.cfg.get("risk.allow_live") is True
        dec = eng.risk.check_account_mode(a)
        s = [f"🧪 {self.L('حالت حساب', 'Account type')}: {a.trade_mode.upper()}",
             f"allow_live (config.yaml): {'TRUE ⚠️' if allow else 'false ✅'}",
             f"{'✅' if dec else '⛔'} {self.L('وضعیت مجوز معامله', 'Trading permission')}: {'OK' if dec else dec.reason}"]
        if a.trade_mode == "real":
            s.append(self.t("live_need", phrase=LIVE_CONFIRM_PHRASE))
        else:
            s.append(self.L("فقط DEMO امن است. حساب واقعی به‌صورت پیش‌فرض رد می‌شود.", "DEMO only is safe. Real accounts are refused by default."))
        return "\n".join(s)

    def help_text(self) -> str:
        cmds = ("/start /menu /status /pause /run /today /week /settings /mode /csv /help\n"
                "/set risk 0.5 | dailyloss 3 | maxpos 3 | tf H1 | sessions 07:00-16:00 | symbols EURUSD,GBPUSD | mode rsi_pullback | rr 2")
        return f"❓ {self.L('راهنما', 'Help')}\n{cmds}\n\n{DISCLAIMER}"

    # ------------------------------------------------------------ dispatch
    def handle_update(self, upd: dict) -> None:
        try:
            if "callback_query" in upd:
                self._on_callback(upd["callback_query"])
            elif "message" in upd:
                self._on_message(upd["message"])
        except Exception as e:
            log.exception("handler error")
            self.send(f"⚠️ {type(e).__name__}: {e}"[:300], chat_id=self._chat_of(upd))

    @staticmethod
    def _chat_of(upd: dict) -> Optional[int]:
        m = upd.get("message") or (upd.get("callback_query") or {}).get("message") or {}
        return (m.get("chat") or {}).get("id")

    def _authorized(self, user: dict, chat_id: int, is_start: bool) -> bool:
        uid = user.get("id")
        if self.owner_id is None:
            if is_start and str(user.get("username", "")).lower() == self.owner_username:
                self.state.set("owner_id", uid)
                log.info("owner bound to numeric id")
                self.send(self.t("owner_bound"), chat_id=chat_id)
                return True
            return False
        return uid == self.owner_id

    def _refuse(self, chat_id: int) -> None:
        now = time.time()
        if now - self._strangers.get(chat_id, 0) > 3600:  # answer strangers at most once per hour
            self._strangers[chat_id] = now
            self.send(TX["not_owner"][0] + "\n" + TX["not_owner"][1], chat_id=chat_id)

    def _on_message(self, m: dict) -> None:
        chat_id = m["chat"]["id"]
        user = m.get("from") or {}
        if m["chat"].get("type") != "private":
            return
        text = (m.get("text") or "").strip()
        if not self._authorized(user, chat_id, text.lower().startswith("/start")):
            self._refuse(chat_id)
            return
        if not text:
            return
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            arg = arg.strip()
            self.state.set("pending_input", None)
            return self._command(cmd, arg, chat_id)
        if text == LIVE_CONFIRM_PHRASE:
            return self._live_confirm(text)
        pend = self.state.get("pending_input")
        if pend:
            return self._apply_setting(pend, text, chat_id)
        self.send(self.t("unknown"), self.main_menu(), chat_id)

    def _command(self, cmd: str, arg: str, chat_id: int) -> None:
        if cmd == "/start":
            name = "Tester"
            self.send(self.t("welcome", name=name) + "\n\n" + DISCLAIMER, self.main_menu(), chat_id)
        elif cmd in ("/menu",):
            self.send(self.t("menu"), self.main_menu(), chat_id)
        elif cmd == "/status":
            self.send(self.status_text(), [self.back()], chat_id)
        elif cmd in ("/pause",):
            self._pause(chat_id)
        elif cmd in ("/run", "/go"):
            self._resume(chat_id)
        elif cmd == "/today":
            self.send(self.report_text("today"), [self.back()], chat_id)
        elif cmd == "/week":
            self.send(self.report_text("week"), [self.back()], chat_id)
        elif cmd == "/settings":
            self.send(self.t("settings_title"), self.settings_menu(), chat_id)
        elif cmd == "/mode":
            self.send(self.mode_text(), [self.back()], chat_id)
        elif cmd == "/csv":
            self._send_csv(chat_id)
        elif cmd == "/help":
            self.send(self.help_text(), [self.back()], chat_id)
        elif cmd == "/cancel":
            self.send(self.t("cancelled"), self.main_menu(), chat_id)
        elif cmd == "/set":
            self._set_cmd(arg, chat_id)
        elif cmd in ("/disable_risk", "/risk_off", "/norisk"):
            self.send(self.t("cant_disable", r=HARD_MAX_RISK_PER_TRADE, d=HARD_MAX_DAILY_LOSS, p=HARD_MAX_POSITIONS), None, chat_id)
        else:
            self.send(self.t("unknown"), self.main_menu(), chat_id)

    # ------------------------------------------------------------ actions
    def _resume(self, chat_id: int, edit_mid: Optional[int] = None) -> None:
        eng = self.engine
        try:
            with eng._lock:
                if not eng.broker.is_connected():
                    eng.ensure_connected()
                a = eng.broker.account_info()
        except BrokerError as e:
            self.send(self.t("conn_err", e=e), [self.back()], chat_id)
            return
        d = eng.risk.check_account_mode(a)
        if not d:
            msg = self.t("start_blocked", why=d.reason)
            if a.trade_mode == "real":
                msg += "\n\n" + self.t("live_need", phrase=LIVE_CONFIRM_PHRASE)
            self.send(msg, [self.back()], chat_id)
            return
        self.state.set("paused", False)
        msg = self.t("started")
        h = eng.risk.halted(eng.broker.now())
        if not h:
            msg += "\n" + self.t("halted_note", why=h.reason)
        self.send(msg, self.main_menu(), chat_id)

    def _pause(self, chat_id: int) -> None:
        self.state.set("paused", True)
        self.send(self.t("paused"), self.main_menu(), chat_id)

    def _emergency(self, chat_id: int) -> None:
        self.state.set("paused", True)  # pause FIRST so nothing re-opens
        try:
            with self.engine._lock:
                ok, bad = self.engine.close_all()
        except BrokerError as e:
            self.send(self.t("conn_err", e=e), [self.back()], chat_id)
            return
        self.send(self.t("estop_done", ok=ok, bad=bad), self.main_menu(), chat_id)

    def _live_confirm(self, phrase: str) -> None:
        try:
            with self.engine._lock:
                a = self.engine.broker.account_info()
        except BrokerError as e:
            self.send(self.t("conn_err", e=e))
            return
        d = self.engine.risk.confirm_live(a, phrase)
        self.send(self.t("live_ok") if d else self.t("live_no", why=d.reason), self.main_menu())

    def _send_csv(self, chat_id: int) -> None:
        p = self.engine.trade_log_path()
        if not p.exists():
            self.send(self.t("no_trades"), [self.back()], chat_id)
            return
        with open(p, "rb") as f:
            data = f.read()
        try:
            self.api.call("sendDocument", {"chat_id": chat_id, "caption": self.t("csv_caption")},
                          files={"document": ("trades.csv", io.BytesIO(data), "text/csv")})
        except Exception as e:
            log.warning("csv send failed: %s", e)
            self.send("⚠️ CSV: " + str(e)[:150], None, chat_id)

    # ------------------------------------------------------------ settings
    def _label(self, key: str) -> str:
        fa, en = SETTING_LABELS.get(key, (key, key))
        return fa if self.lang == "fa" else en

    def _set_cmd(self, arg: str, chat_id: int) -> None:
        parts = arg.split(None, 1)
        if len(parts) != 2:
            self.send(self.t("set_usage", keys=", ".join(sorted(__import__("config").ALIASES))), None, chat_id)
            return
        self._apply_setting(parts[0], parts[1], chat_id)

    def _apply_setting(self, key: str, raw: str, chat_id: int) -> None:
        try:
            full = __import__("config").ALIASES.get(key.lower(), key)
            val = self.cfg.set_override(key, raw)
        except ValueError as e:
            self.send(self.t("set_bad", e=e), None, chat_id)
            return
        self.state.set("pending_input", None)
        shown = ",".join(val) if isinstance(val, list) else val
        self.send(self.t("set_ok", label=self._label(full), value=shown), self.settings_menu(), chat_id)

    def _on_callback(self, cq: dict) -> None:
        user = cq.get("from") or {}
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        try:
            self.api.call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        except Exception:
            pass
        if chat_id is None or not self._authorized(user, chat_id, False):
            if chat_id is not None:
                self._refuse(chat_id)
            return
        data = cq.get("data", "")
        if data == "menu":
            self.state.set("pending_input", None)
            self.edit(chat_id, mid, self.t("menu"), self.main_menu())
        elif data == "start":
            self._resume(chat_id)
        elif data == "pause":
            self._pause(chat_id)
        elif data == "estop":
            self.edit(chat_id, mid, self.t("estop_ask"),
                      [[_btn(self.t("yes_stop"), "estop_yes"), _btn(self.t("no"), "menu")]])
        elif data == "estop_yes":
            self._emergency(chat_id)
        elif data == "status":
            self.edit(chat_id, mid, self.status_text(), [[_btn("🔄", "status")] + self.back()])
        elif data == "reports":
            self.edit(chat_id, mid, self.L("کدام گزارش؟", "Which report?"),
                      [[_btn(self.L("📈 امروز", "📈 Today"), "rep_today"), _btn(self.L("📅 هفته", "📅 Week"), "rep_week")], self.back()])
        elif data in ("rep_today", "rep_week"):
            self.edit(chat_id, mid, self.report_text("today" if data == "rep_today" else "week"), [self.back()])
        elif data == "settings":
            self.state.set("pending_input", None)
            self.edit(chat_id, mid, self.t("settings_title"), self.settings_menu())
        elif data.startswith("set:"):
            key = data[4:]
            if key not in EDITABLE:
                return
            self.state.set("pending_input", key)
            if key in ("trading.sessions_utc",):
                self.send(self.t("set_prompt_text", label=self._label(key), example=SETTING_EXAMPLES.get(key, "")), None, chat_id)
            else:
                self.send(self.t("set_prompt_num", label=self._label(key)) + f"\n({self._range_hint(key)})", None, chat_id)
        elif data == "tfmenu":
            self.edit(chat_id, mid, self.t("pick_tf"),
                      [[_btn(("✅ " if tf == self.cfg.get("trading.timeframe") else "") + tf, "tf:" + tf) for tf in TIMEFRAMES],
                       [_btn(self.L("↩️ تنظیمات", "↩️ Settings"), "settings")]])
        elif data.startswith("tf:"):
            self._apply_setting("trading.timeframe", data[3:], chat_id)
        elif data == "modemenu":
            self.edit(chat_id, mid, self.t("pick_mode"),
                      [[_btn("📉 RSI pullback", "md:rsi_pullback"), _btn("✖️ EMA cross", "md:ema_cross")],
                       [_btn(self.L("↩️ تنظیمات", "↩️ Settings"), "settings")]])
        elif data.startswith("md:"):
            self._apply_setting("strategy.mode", data[3:], chat_id)
        elif data == "symbols":
            self.edit(chat_id, mid, self.t("symbols_title"), self.symbols_menu())
        elif data.startswith("sym:"):
            try:
                self.cfg.toggle_symbol(data[4:])
            except ValueError as e:
                self.send(self.t("set_bad", e=e), None, chat_id)
            self.edit(chat_id, mid, self.t("symbols_title"), self.symbols_menu())
        elif data == "mode":
            self.edit(chat_id, mid, self.mode_text(), [self.back()])
        elif data == "csv":
            self._send_csv(chat_id)
        elif data == "notify":
            on = not self.state.get("notify_trades", True)
            self.state.set("notify_trades", on)
            self.edit(chat_id, mid, self.t("notify_on" if on else "notify_off"), self.main_menu())
        elif data == "help":
            self.edit(chat_id, mid, self.help_text(), [self.back()])
        elif data == "lang":
            self.state.set("language", "en" if self.lang == "fa" else "fa")
            self.edit(chat_id, mid, self.t("lang_set") + "\n" + self.t("menu"), self.main_menu())

    def _range_hint(self, key: str) -> str:
        return {"risk.risk_per_trade_pct": f"0.05 – {HARD_MAX_RISK_PER_TRADE}",
                "risk.max_daily_loss_pct": f"0.5 – {HARD_MAX_DAILY_LOSS}",
                "risk.max_open_positions": f"1 – {HARD_MAX_POSITIONS}"}.get(key, "")

    # ------------------------------------------------------------ polling
    def poll_once(self, timeout: int = 25) -> int:
        ups = self.api.call("getUpdates", {"offset": self._offset, "timeout": timeout,
                                           "allowed_updates": ["message", "callback_query"]},
                            http_timeout=timeout + 10)
        for u in ups:
            self._offset = max(self._offset, u["update_id"] + 1)
            self.handle_update(u)
        return len(ups)

    def run_forever(self, stop: Optional[threading.Event] = None) -> None:
        stop = stop or self._stop
        delay = 1.0
        while not stop.is_set():
            try:
                self.poll_once()
                delay = 1.0
            except (ConnectionError, RuntimeError) as e:
                log.warning("polling problem: %s (retry in %.0fs)", e, delay)
                stop.wait(delay)
                delay = min(delay * 2, 60.0)

    def stop(self) -> None:
        self._stop.set()


def get_token() -> Optional[str]:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    return tok or None


NO_TOKEN_MESSAGE = """
=====================================================================
 88 Trader - توکن تلگرام تنظیم نشده است / Telegram token is not set
=====================================================================
فارسی:
  1) در تلگرام به @BotFather پیام بدهید و با /newbot یک ربات بسازید.
  2) توکن را کپی کنید.
  3) فایل ".env" را کنار run.bat بسازید (از روی .env.example) و بنویسید:
         TRADER_TELEGRAM_BOT_TOKEN=توکن_شما
  4) دوباره run.bat را اجرا کنید.
English:
  Create a bot with @BotFather (/newbot), copy the token, put it in a file named
  ".env" next to run.bat as:   TRADER_TELEGRAM_BOT_TOKEN=your_token
  (or set the environment variable), then run run.bat again.
  Nothing is traded until the Telegram control bot is running.
=====================================================================
"""
