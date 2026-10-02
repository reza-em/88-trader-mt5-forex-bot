"""88 Trader - honest backtest tool.

    python backtest.py                       # real Yahoo H1 data (downloads/caches ~2 years), default config
    python backtest.py --symbols EURUSD GBPUSD --spread-mult 2
    python backtest.py --synthetic           # SYNTHETIC example data (pipeline demo only, meaningless)

It replays candles through the SAME strategy + risk manager code used live (position sizing, spread
filter, sessions, daily-loss / drawdown halts, one position per symbol, max positions) and a MockBroker
with explicit spread / slippage / commission assumptions. It is NOT a guarantee of anything.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import Config, load_dotenv
from history import PROXY_NOTE, SYNTHETIC_LABEL, load_or_download, synthetic_ohlc
from mock_broker import DEFAULT_SPREAD_POINTS, MockBroker
from models import ClosedTrade
from risk import RiskManager, day_key
from state import State
from strategy import compute_features, in_session, signal_at, min_bars

CAVEAT_EN = (
    "IMPORTANT: a backtest is a simulation of the PAST with simplified assumptions (fixed spread, small slippage, "
    "hourly Yahoo candles that are not your broker's feed). It does NOT guarantee or predict future results. Real "
    "trading has variable spreads, requotes, gaps, swaps, news spikes and emotions. Small differences in settings "
    "can change results a lot (overfitting), and a few dozen trades is a tiny sample. Treat any profit here as "
    "'not proven', and any loss as a warning. Test on a DEMO account for months before thinking about real money; "
    "you can lose all the money you put in.")
CAVEAT_FA = (
    "هشدار مهم: بک‌تست فقط شبیه‌سازی گذشته با فرضیات ساده است (اسپرد ثابت، لغزش کم، کندل‌های ساعتی یاهو که با دیتای "
    "بروکر شما یکی نیست). این نتایج سود آینده را تضمین یا پیش‌بینی نمی‌کند. در بازار واقعی اسپرد متغیر، ریکوت، گپ، "
    "سواپ و اخبار وجود دارد. تغییر کوچک در تنظیمات می‌تواند نتیجه را خیلی عوض کند و تعداد معاملات کم نمونه‌ی "
    "ناکافی است. چند ماه روی حساب دمو آزمایش کنید و هرگز پولی که تحمل از دست دادنش را ندارید وارد بازار نکنید.")


@dataclass
class Assumptions:
    spread_points: dict = field(default_factory=lambda: dict(DEFAULT_SPREAD_POINTS))
    slippage_points: float = 1.0
    sl_extra_slippage_points: float = 2.0
    commission_per_lot: float = 0.0   # per lot per side (USD). Alpari standard accounts: spread only
    start_balance: float = 10000.0


def default_cfg(symbols: list[str], timeframe: str, overrides: Optional[dict] = None) -> dict:
    base = Config().data  # config.yaml defaults (hard caps applied)
    d = copy.deepcopy(base)
    d["trading"]["symbols"] = symbols
    d["trading"]["timeframe"] = timeframe
    d["risk"]["min_seconds_between_orders"] = 0   # several symbols share one simulated timestamp
    d["risk"]["max_orders_per_hour"] = 6
    d["paths"] = {"data_dir": "data", "log_file": "logs/bt.log"}
    for k, v in (overrides or {}).items():
        sec, key = k.split(".")
        d[sec][key] = v
    return d


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    m = {"H1": None, "H4": "4h", "M30": None}.get(tf)
    if tf == "H1":
        return df
    if tf == "H4":
        return df.resample("4h", origin="epoch").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    raise ValueError("backtest supports H1 and H4 with the Yahoo H1 data")


def run_backtest(data: dict[str, pd.DataFrame], cfg_data: dict, assume: Assumptions,
                 start: Optional[pd.Timestamp] = None, end: Optional[pd.Timestamp] = None,
                 auto_reset_drawdown_halt: bool = False) -> dict:
    """auto_reset_drawdown_halt=True is an ANALYSIS option only (live bot halts until a manual reset)."""
    tf = cfg_data["trading"]["timeframe"]
    tmp = Path(tempfile.mkdtemp(prefix="bt88_"))
    cfg = Config(data=cfg_data, base_dir=tmp)
    state = State(tmp / "state.json")
    state.set("paused", False)
    broker = MockBroker(balance=assume.start_balance, spread_points=assume.spread_points,
                        slippage_points=assume.slippage_points, sl_extra_slippage_points=assume.sl_extra_slippage_points,
                        commission_per_lot=assume.commission_per_lot)
    broker.connect()
    risk = RiskManager(cfg, state)
    params = cfg.get("strategy")
    magic = cfg.get("risk.magic")
    sessions, days = cfg.get("trading.sessions_utc"), cfg.get("trading.trading_days")
    feats, idxpos, syms = {}, {}, []
    for s in cfg.get("trading.symbols"):
        if s not in data:
            continue
        df = resample(data[s], tf)
        broker.load_data(s, df, tf)
        si0 = 10 ** -(3 if s == "USDJPY" else 2 if s == "XAUUSD" else 5)
        feats[s] = (compute_features(df, params), si0)
        idxpos[s] = {t: i for i, t in enumerate(df.index)}
        syms.append(s)
    if not syms:
        raise ValueError("no data")
    all_times = sorted(set().union(*[set(feats[s][0].index) for s in syms]))
    if start is not None:
        all_times = [t for t in all_times if t >= start]
    if end is not None:
        all_times = [t for t in all_times if t <= end]
    equity_curve: list[tuple[pd.Timestamp, float]] = []
    halts = {"daily": 0, "drawdown": 0}
    skipped = {"rate_hour": 0, "spread": 0, "max_positions": 0, "one_per_symbol": 0, "risk_too_small": 0, "other": 0}
    halt_dates: list[str] = []
    signals = 0
    warm = min_bars(params)
    for t in all_times:
        now = t.to_pydatetime()
        here = []
        for s in syms:
            i = idxpos[s].get(t)
            if i is not None:
                broker.set_cursor(i, s)
                here.append((s, i))
        broker.set_time(now)
        acct = broker.account_info()
        for ev in risk.update(acct, now):
            halts["daily" if ev == "daily_loss" else "drawdown"] += 1
            if ev == "drawdown":
                halt_dates.append(f"{t:%Y-%m-%d}")
                if auto_reset_drawdown_halt:
                    state.update(halt_drawdown=False, peak_equity=acct.equity)
        equity_curve.append((t, acct.equity))
        if not state.get("halt_drawdown") and risk.halted(now) and in_session(now, sessions, days):
            for s, i in here:
                if i < warm:
                    continue
                f, pt = feats[s]
                sig = signal_at(f, i - 1, params, pt)  # decision from last CLOSED bar (i-1), fill at open of i
                if sig is None:
                    continue
                signals += 1
                si = broker.symbol_info(s)
                positions = broker.get_positions()
                d = risk.can_open(s, sig.side, acct, positions, si, now)
                if not d:
                    skipped[d.code if d.code in skipped else "other"] += 1
                    continue
                entry = si.ask if sig.side == "buy" else si.bid
                sl = entry - sig.sl_distance if sig.side == "buy" else entry + sig.sl_distance
                tp = entry + sig.tp_distance if sig.side == "buy" else entry - sig.tp_distance
                sl, tp = round(sl, si.digits), round(tp, si.digits)
                if not risk.validate_order(sig.side, entry, sl, tp, si):
                    skipped["other"] += 1
                    continue
                vol = risk.size(acct, abs(entry - sl), si)
                if vol <= 0:
                    skipped["risk_too_small"] += 1
                    continue
                r = broker.place_order(s, sig.side, vol, sl, tp, deviation=cfg.get("risk.max_slippage_points"), magic=magic)
                if r.ok:
                    risk.record_order(now)
                    acct = broker.account_info()
                else:
                    skipped["other"] += 1
        for s, i in here:
            broker.process_bar(s)
    broker.force_close_all_at_end()
    trades = broker.closed_trades(datetime(2000, 1, 1, tzinfo=timezone.utc))
    return {"trades": trades, "equity": equity_curve, "final_balance": broker.balance, "start_balance": assume.start_balance,
            "halts": halts, "halt_dates": halt_dates, "skipped": skipped, "signals": signals,
            "period": (all_times[0], all_times[-1]) if all_times else (None, None)}


def metrics(res: dict) -> dict:
    tr: list[ClosedTrade] = res["trades"]
    pl = np.array([t.profit for t in tr], dtype=float)
    wins, losses = pl[pl > 0], pl[pl <= 0]
    gp, gl = wins.sum() if len(wins) else 0.0, -losses.sum() if len(losses) else 0.0
    eq = pd.Series([e for _, e in res["equity"]], index=[t for t, _ in res["equity"]], dtype=float)
    if len(eq):
        dd = (eq.cummax() - eq) / eq.cummax() * 100.0
        max_dd = float(dd.max())
    else:
        max_dd = 0.0
    streak = cur = 0
    for x in pl:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    n = len(pl)
    wr = len(wins) / n * 100 if n else 0.0
    se = math.sqrt(max(wr / 100 * (1 - wr / 100), 0) / n) * 100 * 1.96 if n else 0.0
    by_sym: dict[str, dict] = {}
    for t in tr:
        b = by_sym.setdefault(t.symbol, {"n": 0, "pnl": 0.0, "wins": 0})
        b["n"] += 1
        b["pnl"] += t.profit
        b["wins"] += t.profit > 0
    return {
        "trades": n, "wins": int(len(wins)), "losses": int(len(losses)), "win_rate": wr, "win_rate_ci95": se,
        "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "net_pnl": float(pl.sum()), "return_pct": float(pl.sum()) / res["start_balance"] * 100,
        "max_dd_pct": max_dd, "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0, "expectancy": float(pl.mean()) if n else 0.0,
        "max_losing_streak": streak, "halt_dates": res.get("halt_dates", []), "by_symbol": by_sym, "halts": res["halts"], "skipped": res["skipped"],
        "signals": res["signals"], "final_balance": res["final_balance"],
    }


def fmt_pf(x: float) -> str:
    return "inf" if x == float("inf") else f"{x:.2f}"


def summary_lines(m: dict) -> list[str]:
    return [
        f"Trades: {m['trades']}  (wins {m['wins']}, losses {m['losses']})",
        f"Win rate: {m['win_rate']:.1f}%  (95% margin of error about ±{m['win_rate_ci95']:.1f} points - small sample!)",
        f"Profit factor: {fmt_pf(m['profit_factor'])}   (1.0 = break-even; below 1.0 loses money)",
        f"Net P/L: {m['net_pnl']:+,.2f} USD  ({m['return_pct']:+.1f}% of start balance)",
        f"Max drawdown (equity, sampled at bar opens): {m['max_dd_pct']:.1f}%",
        f"Average win {m['avg_win']:+.2f} | average loss {m['avg_loss']:+.2f} | expectancy per trade {m['expectancy']:+.2f}",
        f"Longest losing streak: {m['max_losing_streak']}",
        f"Risk-manager events: daily-loss halts {m['halts']['daily']}, drawdown halts {m['halts']['drawdown']}; "
        f"signals {m['signals']}, skipped: {m['skipped']}",
    ]


def build_report(title_data: str, res: dict, assume: Assumptions, cfg_data: dict, extra: list[tuple[str, dict]],
                 notes: list[str]) -> str:
    m = metrics(res)
    p0, p1 = res["period"]
    L = ["# 88 Trader - Backtest report", "", f"Generated: {datetime.now():%Y-%m-%d %H:%M}",
         f"Data: {title_data}", f"Period tested: {p0} -> {p1} (UTC)",
         f"Timeframe: {cfg_data['trading']['timeframe']} | symbols: {', '.join(cfg_data['trading']['symbols'])} | "
         f"mode: {cfg_data['strategy']['mode']} | risk/trade {cfg_data['risk']['risk_per_trade_pct']}% | "
         f"sessions {cfg_data['trading']['sessions_utc']}",
         "", "## Assumptions",
         f"- Start balance {assume.start_balance:,.0f} USD; lot size from risk % and SL distance (same code as live).",
         f"- Fixed spread (points; 10 points = 1 pip): {json.dumps({s: assume.spread_points.get(s) for s in cfg_data['trading']['symbols']})}",
         f"- Slippage {assume.slippage_points} pt on entries/exits, +{assume.sl_extra_slippage_points} pt extra on stop-loss fills; "
         f"commission {assume.commission_per_lot} USD/lot/side; swaps NOT modelled.",
         "- If SL and TP are both inside one candle, the simulation assumes the STOP LOSS was hit first (pessimistic).",
         "- Signals use closed candles only; entry at the next candle's open. No parameter optimisation was done.",
         "", "## Results (main run)"] + [f"- {x}" for x in summary_lines(m)]
    L += ["", "## By symbol"] + [f"- {s}: {b['n']} trades, win {b['wins']/b['n']*100:.0f}%, P/L {b['pnl']:+,.2f}" for s, b in sorted(m["by_symbol"].items())]
    for name, mm in extra:
        L += ["", f"## {name}"] + [f"- {x}" for x in summary_lines(mm)[:5]]
    if m["halt_dates"]:
        notes.append(f"The 10% max-drawdown halt tripped on {', '.join(m['halt_dates'])}: in the main run the bot STOPS trading "
                     "from that date (as the live bot would until manually reset), so later months are not traded.")
    if notes:
        L += ["", "## Notes"] + [f"- {n}" for n in notes]
    L += ["", "## Caveat", CAVEAT_EN, "", CAVEAT_FA, ""]
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="88 Trader backtest")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--tf", default=None, help="H1 or H4")
    ap.add_argument("--synthetic", action="store_true", help="use SYNTHETIC example data (offline demo only)")
    ap.add_argument("--refresh", action="store_true", help="re-download data")
    ap.add_argument("--spread-mult", type=float, default=1.0)
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--mode", default=None, help="rsi_pullback or ema_cross")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--no-extra", action="store_true", help="skip stress / split runs")
    a = ap.parse_args(argv)

    base = Config().data
    symbols = a.symbols or base["trading"]["symbols"]
    tf = a.tf or base["trading"]["timeframe"]
    over = {"strategy.mode": a.mode} if a.mode else None
    cfgd = default_cfg(symbols, tf, over)
    notes: list[str] = []
    data: dict[str, pd.DataFrame] = {}
    if a.synthetic:
        title = SYNTHETIC_LABEL
        for i, s in enumerate(symbols):
            data[s] = synthetic_ohlc(s, bars=9000, seed=10 + i)
        notes.append("Synthetic data: results are meaningless for judging the strategy.")
    else:
        title = "REAL hourly candles from Yahoo Finance via yfinance (mid/bid-like prices, no broker spread in data)"
        for s in symbols:
            try:
                data[s] = load_or_download(s, Path("data") / "history", refresh=a.refresh)
            except Exception as e:
                print(f"! could not download {s}: {e}")
            if s in PROXY_NOTE and s in data:
                notes.append(PROXY_NOTE[s])
        if not data:
            print("No data available (no internet?). Try --synthetic for a demo of the tool.")
            return 1
    spreads = {k: v * a.spread_mult for k, v in DEFAULT_SPREAD_POINTS.items()}
    assume = Assumptions(spread_points=spreads, slippage_points=a.slippage, commission_per_lot=a.commission,
                         start_balance=a.balance)
    res = run_backtest(data, cfgd, assume)
    m = metrics(res)
    extra: list[tuple[str, dict]] = []
    if not a.no_extra:
        s2 = Assumptions(spread_points={k: v * 2 for k, v in spreads.items()}, slippage_points=a.slippage * 2,
                         commission_per_lot=a.commission, start_balance=a.balance)
        extra.append(("Stress test: spread x2 and slippage x2", metrics(run_backtest(data, cfgd, s2))))
        p0, p1 = res["period"]
        mid = p0 + (p1 - p0) / 2
        extra.append((f"First half only ({p0:%Y-%m-%d} -> {mid:%Y-%m-%d})", metrics(run_backtest(data, cfgd, assume, end=mid))))
        extra.append((f"Second half only ({mid:%Y-%m-%d} -> {p1:%Y-%m-%d})", metrics(run_backtest(data, cfgd, assume, start=mid))))
        alt = "ema_cross" if cfgd["strategy"]["mode"] == "rsi_pullback" else "rsi_pullback"
        c2 = copy.deepcopy(cfgd)
        c2["strategy"]["mode"] = alt
        extra.append(("Analysis only: same run but the drawdown halt is auto-reset (the live bot would stay halted)",
                      metrics(run_backtest(data, cfgd, assume, auto_reset_drawdown_halt=True))))
        extra.append((f"Other entry mode ({alt}) - for comparison only, NOT cherry-picked", metrics(run_backtest(data, c2, assume))))
    report = build_report(title, res, assume, cfgd, extra, notes)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = "synthetic" if a.synthetic else "real"
    (out / f"backtest_report_{tag}.md").write_text(report, encoding="utf-8")
    pd.DataFrame([t.to_dict() for t in res["trades"]]).to_csv(out / f"backtest_trades_{tag}.csv", index=False)
    print(report)
    print(f"\n(saved to {out}/backtest_report_{tag}.md)")
    return 0


if __name__ == "__main__":
    load_dotenv()
    sys.exit(main())
