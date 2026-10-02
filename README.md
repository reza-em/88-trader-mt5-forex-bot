# 88 Trader — Conservative MT5 Forex Trading Bot with Telegram Control

> Conservative demo-account forex bot for MetaTrader 5 with Telegram control: EMA trend + RSI pullback strategy, ATR stops, mandatory risk manager, honest backtester. Persian and English docs. Not financial advice.

## 🌐 فارسی

**۸۸ تریدر**: ربات محافظه‌کار فارکس برای **متاتریدر ۵ (MT5)** روی حساب **دمو** با کنترل از تلگرام. استراتژی روند EMA و پولبک RSI، حد ضرر ATR، مدیر ریسک اجباری و بک‌تستر صادق. راهنمای فارسی در `README_fa.md`. **توصیه مالی نیست و سودی تضمین نمی‌شود.**

**کلیدواژه‌ها:** ربات معامله‌گر فارکس، متاتریدر ۵، ربات ترید، بک‌تست، مدیریت ریسک، ربات تلگرام ترید، معاملات الگوریتمی

## 🇬🇧 English

**88 Trader** is a conservative **forex trading bot for MetaTrader 5 (MT5)** meant for **demo accounts**, controlled through Telegram. EMA50/200 trend + RSI pullback strategy (or EMA cross), ATR stop-loss, 1:2 take-profit, mandatory risk manager and an honest backtester. **Not financial advice; no profit is promised.** Live trading is refused unless explicitly enabled.

**Keywords:** MT5 forex bot, MetaTrader 5 Python trading bot, algorithmic trading, backtesting, risk management, Telegram trading bot, EMA RSI ATR strategy, XAUUSD

## 🇷🇺 Русский

**88 Trader** — консервативный **торговый бот Forex для MetaTrader 5 (MT5)** для **демо-счетов** с управлением через Telegram. Стратегия: тренд EMA50/200 + откат RSI, стоп-лосс по ATR, обязательный риск-менеджер, честный бэктестер. **Не является финансовой рекомендацией.**

**Ключевые слова:** торговый бот Forex, MetaTrader 5 Python, алготрейдинг, бэктест, управление рисками, Telegram бот для трейдинга

## 🇩🇪 Deutsch

**88 Trader** ist ein konservativer **Forex-Handelsbot für MetaTrader 5 (MT5)** für **Demokonten** mit Telegram-Steuerung. EMA50/200-Trend + RSI-Pullback, ATR-Stop-Loss, verpflichtender Risikomanager, ehrlicher Backtester. **Keine Anlageberatung.**

**Stichwörter:** Forex Handelsbot, MetaTrader 5 Python, algorithmischer Handel, Backtesting, Risikomanagement, Telegram Trading Bot

---

## Features

- Broker interface with MT5 adapter (Windows) and mock broker for tests/backtests
- EMA trend + RSI pullback strategy, ATR stop, 1:2 take profit
- Mandatory risk manager; real-account trading refused unless `allow_live: true` + confirmation
- Telegram control bot (fa/en), owner-only
- Backtester using Yahoo Finance hourly candles (`history.py`, downloaded on demand)
- pytest suite (`tests/`)

## Setup

```bash
pip install -r requirements.txt        # Windows + MetaTrader5 for live demo trading
copy .env.example .env                 # set TRADER_TELEGRAM_BOT_TOKEN and MT5_PASSWORD
# edit config.yaml: demo account login/server, owner_username
python check_setup.py                  # green/red checklist
python backtest.py                     # downloads real hourly data and runs the backtest
python main.py
```

> **Configuration note:** the owner/admin identity is read from environment variables (`OWNER_ID`, `OWNER_USERNAME`, `SUPPORT_USERNAME`) with the placeholder `example_owner`. Set them to your own values before running. Never commit bot tokens — they are read only from the environment.

## Usage

Open your bot in the messenger and send `/start`. See the detailed documentation below for commands, admin panel and platform-specific notes.

## License

Code released under the [MIT License](LICENSE).

---

## Detailed documentation

# 88 Trader (English quick notes)
Conservative demo-account forex bot for MetaTrader 5 (Windows) + Telegram control. **Not financial advice. No profit is promised or implied.**
Persian step-by-step guide: `README_fa.md`.

* `broker.py` interface + `MT5Broker` (official `MetaTrader5` package, Windows only) · `mock_broker.py` (tests/backtest)
* `strategy.py` EMA50/200 trend + RSI pullback (or EMA cross), ATR stop, 1:2 TP · `risk.py` mandatory risk manager
* `engine.py` trading loop · `telegram_bot.py` control bot (requests long polling, fa/en) · `backtest.py` honest backtester
* `check_setup.py` green/red checklist · tests: `pip install -r requirements-dev.txt && python -m pytest`
* Real-account trading is refused unless `allow_live: true` is set by hand in config.yaml AND MT5 reports a real account AND the owner sends the confirmation phrase.
* Backtest data: `history.py` downloads real hourly candles from Yahoo Finance; `--synthetic` data is labelled EXAMPLE DATA and is meaningless for evaluation.
* The MT5 adapter has been tested only against a fake module on Linux; run `check_setup.py` on Windows first.

