"""
TharEX Bot — Main Loop
DEMO ACCOUNT 2: 25599084 @ VantageMarkets-Demo

Strategy (reverse-engineered from TharEX EA V2 on account 29456493):
  - Multi-symbol: XAUUSD + GBPUSD, each with independent dual-side BUY+SELL grids
  - 3-tier accelerating lots: +0.01 (L1-8), +0.02 (L9-17), +0.03 (L18+)
  - Grid spacing & basket TP margin are per-symbol (see SYMBOLS_CONFIG in config.py)
  - Basket TP: weighted avg entry +/- margin, set as MT5 TP on all positions
  - Session filter (config.SESSION_FILTER_ENABLED): when True, new L1s open only
    within London 07:00-10:00 + New York 12:00-15:00 UTC kill zones. When False
    (default, matches real TharEX EA), new L1s open 24/7; windows are used only
    for periodic P&L summaries. Recovery trades always run 24/7.

Run: python main.py
"""

import sys
import os
import time
import json
import logging
import signal
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Imports ───────────────────────────────────────────────────
from config import (
    MT5_LOGIN, MT5_SERVER,
    SCAN_INTERVAL_SECONDS,
    SESSIONS_UTC,
    SESSION_FILTER_ENABLED,
    SYMBOLS_CONFIG,
    DRAWDOWN_LIMIT_PCT,
    DRAWDOWN_LIMIT_USC,
    DRAWDOWN_ENABLE_ABOVE_USC,
    NEWS_BLACKOUT_MINUTES,
    MAX_SPREAD_POINTS,
    DAILY_PROFIT_TARGET_USC,
    OPPOSITE_SIDE_BLOCK_LEVEL,
    GRID_DEPTH_ALERT_LEVELS,
    FLOAT_LOSS_ALERT_PCT,
    WEEKEND_CLOSE_UTC_HOUR,
    TREND_FILTER_ENABLED,
    TREND_TIMEFRAME,
    TREND_EMA_PERIOD,
    PROFIT_GIVEBACK_LIMIT_USC,
    WEEKLY_WITHDRAW_BASE_USC,
)
from trend import get_trend, allows
from news_filter import is_news_blackout
from mt5_connector import (
    connect, disconnect, is_connected, get_account,
    get_tick, resolve_symbol, close_all_bot_positions,
)
from grid_engine import DualGrid
from telegram_notifier import (
    notify_bot_started, notify_bot_stopped,
    notify_connection_lost, notify_connection_restored,
    notify_error, notify_session_summary, notify_drawdown_triggered,
    notify_grid_depth_alert, notify_daily_profit_target,
    notify_weekend_status, notify_float_loss_warning,
    notify_profit_lock, notify_withdraw_reminder,
    notify_hedge_sweep,
)

# ── State ─────────────────────────────────────────────────────
running  = True
_SYMBOLS = []   # list of {cfg: dict, grid: DualGrid, symbol_live: str}

# Session tracking — for end-of-session Telegram summary
_was_in_session       = False
_session_name         = ""
_session_start_equity = 0.0
_day_start_balance    = 0.0
_last_day             = -1       # UTC day; resets daily tracker at midnight

# Drawdown halt — set when loss hits DRAWDOWN_LIMIT_PCT of balance.
# All positions closed; bot waits until next day's first session opens.
_drawdown_halt     = False
_drawdown_halt_day = -1          # UTC day number when halt was triggered

# Daily profit halt — set when day P&L hits DAILY_PROFIT_TARGET_USC.
# Only blocks new L1 entries; recovery continues. Resets at midnight UTC.
_profit_halt       = False

# Float loss soft alert — sent once per episode to avoid spam.
# Resets when floating loss drops back below FLOAT_LOSS_ALERT_PCT.
_float_alerted     = False

# Grid depth alerts — tracks which (label, side, threshold) alerts already sent.
# Cleared when that grid side resets to L0 (basket TP closed).
_depth_alerted: set = set()

# Weekend handling — set on Friday >= WEEKEND_CLOSE_UTC_HOUR, reset at midnight.
_weekend_handled   = False

# Profit high-water-mark giveback protection.
# _peak_balance = highest realized balance seen (persisted). If balance falls
# PROFIT_GIVEBACK_LIMIT_USC below it, _profit_lock_halt pauses new baskets.
_peak_balance      = 0.0
_profit_lock_halt  = False
_withdraw_reminded = False   # weekly reminder sent once per Friday


def handle_shutdown(sig, frame):
    global running
    log.info("Shutdown signal received — stopping bot...")
    running = False


signal.signal(signal.SIGINT,  handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ── New-entry pause flag ──────────────────────────────────────
# Toggled by `_botctl.sh pause/resume` and the Telegram "Stop/Start Trades"
# buttons. While the flag file exists, NO new L1 baskets open, but recovery of
# any already-open basket keeps running 24/7 until it closes. This is a soft
# "stop new trades" — distinct from stopping the whole bot process.
PAUSE_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trading.paused")


def new_entries_paused() -> bool:
    return os.path.exists(PAUSE_FLAG)


# --- Drawdown guard ON/OFF flag (Telegram-controlled, per bot) -----
# Absent = drawdown guard ON (default); present = OFF (guard skipped).
DRAWDOWN_OFF_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "drawdown.off")


def drawdown_off() -> bool:
    return os.path.exists(DRAWDOWN_OFF_FLAG)


# --- Daily profit anchor (persisted) --------------------------
# Persist _day_start_balance so a mid-day RESTART reloads the true-midnight
# anchor instead of re-snapping to the current balance.
DAY_ANCHOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_anchor.txt")


def load_day_anchor():
    """Return (date_str, balance) from disk, or (None, None)."""
    try:
        with open(DAY_ANCHOR_FILE) as _f:
            _d, _b = _f.read().split()
            return _d, float(_b)
    except Exception:
        return None, None


def save_day_anchor(date_str, balance):
    try:
        with open(DAY_ANCHOR_FILE, "w") as _f:
            _f.write("%s %.2f" % (date_str, balance))
    except Exception as _e:
        log.error("day_anchor save failed: %s" % _e)


# ── Profit high-water-mark (giveback) protection ──────────────
HWM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hwm.json")


def _load_hwm() -> float:
    try:
        with open(HWM_FILE) as f:
            return float(json.load(f).get("peak", 0.0))
    except Exception:
        return 0.0


def _save_hwm(peak: float):
    try:
        os.makedirs(os.path.dirname(HWM_FILE), exist_ok=True)
        with open(HWM_FILE, "w") as f:
            json.dump({"peak": peak}, f)
    except Exception:
        pass


# ── Session Helpers ───────────────────────────────────────────
def _utc_hour() -> float:
    now = datetime.now(tz=timezone.utc)
    return now.hour + now.minute / 60.0


def _active_session() -> dict | None:
    """Returns the matching session dict if we're inside any kill zone, else None."""
    h = _utc_hour()
    for s in SESSIONS_UTC:
        if s["start"] <= h < s["end"]:
            return s
    return None


def in_session() -> bool:
    return _active_session() is not None


def in_winddown() -> bool:
    """True when inside a session but past its winddown threshold (no new L1s)."""
    h = _utc_hour()
    return any(s["winddown"] <= h < s["end"] for s in SESSIONS_UTC)


def _grid_summary() -> str:
    """One-line summary of all symbol grid levels for Telegram / logs."""
    return " | ".join(
        f"{s['cfg']['label']} B:L{s['grid'].buy.level_count} S:L{s['grid'].sell.level_count}"
        for s in _SYMBOLS
    )


# ── Status Log ────────────────────────────────────────────────
def log_scan_status():
    acct     = get_account()
    balance  = acct.get('balance', 0) if acct else 0
    equity   = acct.get('equity',  0) if acct else 0
    floating = acct.get('profit',  0) if acct else 0
    log.info(
        f"[Status] {_grid_summary()} "
        f"| Bal:{balance:.2f} Eq:{equity:.2f} Float:{floating:+.2f}"
    )


def print_status():
    acct    = get_account()
    now     = datetime.now(tz=timezone.utc)
    balance = acct.get('balance', 0) if acct else 0
    equity  = acct.get('equity',  0) if acct else 0

    print("\n" + "=" * 66)
    print(f"  TharEX Bot | {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 66)
    print(f"  Account  : {acct.get('login')} @ {acct.get('server')}")
    print(f"  Balance  : {balance:.2f} {acct.get('currency', '')}")
    print(f"  Equity   : {equity:.2f}  Float: {acct.get('profit', 0):+.2f}")
    print(f"  {'─' * 60}")
    for s in _SYMBOLS:
        g  = s['grid']
        st = g.status()
        lbl = s['cfg']['label']
        print(
            f"  {lbl:<8} : BUY L{st['buy_levels']:<3} TP:{st['buy_basket_tp']}"
            f" | SELL L{st['sell_levels']:<3} TP:{st['sell_basket_tp']}"
            f" | Hedges S-side:{st['sell_hedges']} B-side:{st['buy_hedges']}"
        )
    print(f"  {'─' * 60}")
    h   = _utc_hour()
    cur = _active_session()
    if cur is None:
        sess = "CLOSED (between kill zones)"
    elif in_winddown():
        sess = f"WINDDOWN — {cur['name']} (recovery only)"
    else:
        sess = f"ACTIVE — {cur['name']}"
    print(f"  Session  : {sess}  ({h:.2f} UTC)")
    print("=" * 66 + "\n")


# ── Main Loop ─────────────────────────────────────────────────
def main():
    global running, _SYMBOLS
    global _was_in_session, _session_name, _session_start_equity, _day_start_balance, _last_day
    global _drawdown_halt, _drawdown_halt_day
    global _profit_halt, _float_alerted, _depth_alerted, _weekend_handled
    global _peak_balance, _profit_lock_halt, _withdraw_reminded

    log.info("=" * 62)
    log.info("  TharEX Bot Starting — Multi-Symbol")
    log.info(f"  Account  : {MT5_LOGIN} @ {MT5_SERVER}")
    for s in SESSIONS_UTC:
        log.info(f"  Session  : {s['name']} {s['start']:05.2f}-{s['end']:05.2f} UTC | Winddown: {s['winddown']:05.2f} UTC")
    for sc in SYMBOLS_CONFIG:
        log.info(
            f"  Symbol   : {sc['label']} | lot={sc['base_lot']} "
            f"grid={sc['grid_spacing']} maxLots={sc.get('max_lot_cap')} "
            f"maxLevels={sc.get('max_trades')} tpAnchors={len(sc.get('tp_table', []))} "
            f"cooldown={sc.get('cooldown_minutes')}m"
        )
    log.info(
        f"  Risk     : stop {DRAWDOWN_LIMIT_USC:.0f} USC / {DRAWDOWN_LIMIT_PCT}% | "
        f"trend {TREND_TIMEFRAME}({TREND_EMA_PERIOD})={'ON' if TREND_FILTER_ENABLED else 'OFF'} | "
        f"profit-lock giveback {PROFIT_GIVEBACK_LIMIT_USC:.0f} USC | "
        f"weekly-withdraw base {WEEKLY_WITHDRAW_BASE_USC:.0f}"
    )
    for sc in SYMBOLS_CONFIG:
        if sc.get('hedge_enabled'):
            log.info(
                f"  Hedge    : {sc['label']} ON (v2.3 exit-fix) | every {sc.get('hedge_trigger_step', 5)} levels "
                f"| hedge-lot=block-sum | arm {sc.get('hedge_breakeven_arm_dist', 0.75)} "
                f"| hold-TP-while-frozen={sc.get('hedge_hold_tp_until_unwound', False)} "
                f"| open-stop-buffer {sc.get('hedge_open_buffer', 0.0)} "
                f"| skip-near-TP={sc.get('hedge_skip_block_near_tp', False)} "
                f"| magics B:{sc.get('magic_hedge_buy')}/S:{sc.get('magic_hedge_sell')} "
                f"| tp-scale {sc.get('tp_target_scale', 1.0)}"
            )
        else:
            log.info(f"  Hedge    : {sc['label']} OFF (base TharEX behaviour)")
    log.info("=" * 62)

    # Retry initial connection
    while running:
        if connect():
            break
        log.warning("MT5 not ready — retrying in 20s...")
        time.sleep(20)
    if not running:
        sys.exit(0)

    # Resolve and initialise all symbols
    while running:
        try:
            _SYMBOLS = []
            for sc in SYMBOLS_CONFIG:
                sym_live = resolve_symbol(sc['symbol'], sc['label'])
                g = DualGrid(sc)
                g.sync(sym_live)
                _SYMBOLS.append({'cfg': sc, 'grid': g, 'symbol_live': sym_live})
            log.info(f"Symbols ready: {[s['symbol_live'] for s in _SYMBOLS]}")
            break
        except ValueError as e:
            log.warning(f"Symbol resolution failed: {e} — retrying in 10s...")
            time.sleep(10)
    if not running:
        sys.exit(0)

    try:
        acct0 = get_account()
        if acct0:
            notify_bot_started(
                acct0.get('login'), acct0.get('server'),
                acct0.get('balance', 0), acct0.get('currency', ''),
                SYMBOLS_CONFIG,
            )
    except Exception:
        pass

    # Initialise profit high-water-mark (persisted peak vs current balance)
    try:
        _a0 = get_account()
        _peak_balance = max(_load_hwm(), _a0.get('balance', 0.0) if _a0 else 0.0)
        log.info(f"Profit HWM initialised: peak={_peak_balance:.0f} | giveback limit={PROFIT_GIVEBACK_LIMIT_USC:.0f}")
    except Exception:
        _peak_balance = _load_hwm()

    scan_count = 0

    while running:
        try:
            if not is_connected():
                log.warning("MT5 disconnected — reconnecting...")
                try:
                    notify_connection_lost()
                except Exception:
                    pass
                if not connect():
                    time.sleep(30)
                    continue
                try:
                    notify_connection_restored(MT5_LOGIN, MT5_SERVER)
                except Exception:
                    pass
                for s in _SYMBOLS:
                    s['grid'].sync(s['symbol_live'])

            scan_count += 1

            # Sync all grids from live MT5 positions
            for s in _SYMBOLS:
                s['grid'].sync(s['symbol_live'])

            # ── RE-FREEZE (deep basket that lost its hedge) ───────
            # If a hedge peeled off and price resumes against a still-deep basket by
            # a grid step, re-open a full-basket hedge immediately (close the gap).
            for s in _SYMBOLS:
                try:
                    s['grid'].check_refreeze(s['symbol_live'])
                except Exception as _e:
                    log.error(f"Re-freeze check error: {_e}")

            # ── HEDGE BREAK-EVEN STOPS ────────────────────────────
            # Arm a break-even stop on each hedge once price has moved in its favour,
            # so hedges peel off at ~0 on the reversal while the basket runs to its TP.
            for s in _SYMBOLS:
                try:
                    s['grid'].check_hedge_breakeven(s['symbol_live'])
                except Exception as _e:
                    log.error(f"Hedge break-even check error: {_e}")

            # ── HEDGED NET EXIT (Idea 1 master exit) ──────────────
            # A frozen (hedged) basket is NOT closed at its small TP (that realizes
            # the frozen loss). Close the whole structure only when basket+hedges
            # reach NET profit. Must run BEFORE check_take_profit (which now skips
            # frozen sides).
            for s in _SYMBOLS:
                try:
                    s['grid'].check_hedged_net_exit(s['symbol_live'])
                except Exception as _e:
                    log.error(f"Hedged net-exit check error: {_e}")

            # ── ACTIVE TAKE-PROFIT (unhedged / unwound sides) ─────
            # Close a basket at market the instant it reaches its (now high, faithful)
            # TP. Frozen sides are skipped here (handled by the net exit above); once a
            # basket's hedges have all peeled, its normal TP resumes.
            for s in _SYMBOLS:
                try:
                    s['grid'].check_take_profit(s['symbol_live'])
                except Exception as _e:
                    log.error(f"Take-profit check error: {_e}")

            # ── HEDGE SWEEP (master exit) ─────────────────────────
            # A side whose basket just closed on its shared TP but still has open
            # hedges → close them. Run before new-entry logic so the next basket
            # starts clean. Peeled-off hedges already closed on their own TP.
            for s in _SYMBOLS:
                try:
                    _swept = s['grid'].sweep_orphan_hedges(s['symbol_live'])
                    if _swept:
                        log.info(f"[{s['cfg']['label']}] Swept {_swept} orphan hedge(s) after basket close")
                        try:
                            notify_hedge_sweep(s['cfg']['label'], _swept)
                        except Exception:
                            pass
                except Exception as _e:
                    log.error(f"Hedge sweep error: {_e}")

            # ── GRID DEPTH ALERTS ─────────────────────────────────
            for s in _SYMBOLS:
                lbl = s['cfg']['label']
                for side_obj, side_name in ((s['grid'].buy, "BUY"), (s['grid'].sell, "SELL")):
                    lvl = side_obj.level_count
                    # Clear alerts for this side when it resets to 0
                    if lvl == 0:
                        for th in GRID_DEPTH_ALERT_LEVELS:
                            _depth_alerted.discard((lbl, side_name, th))
                    else:
                        for th in GRID_DEPTH_ALERT_LEVELS:
                            if lvl >= th and (lbl, side_name, th) not in _depth_alerted:
                                _depth_alerted.add((lbl, side_name, th))
                                log.warning(f"[{lbl}] {side_name} reached L{lvl} — sending depth alert")
                                try:
                                    _a = get_account()
                                    notify_grid_depth_alert(
                                        label    = lbl,
                                        side     = side_name,
                                        level    = lvl,
                                        balance  = _a.get('balance', 0) if _a else 0,
                                        equity   = _a.get('equity',  0) if _a else 0,
                                        floating = _a.get('profit',  0) if _a else 0,
                                        currency = _a.get('currency', 'USC') if _a else 'USC',
                                    )
                                except Exception:
                                    pass

            # ── DRAWDOWN GUARD ────────────────────────────────────
            # Trigger: floating loss >= DRAWDOWN_LIMIT_PCT % of balance.
            # Action : close all positions, halt trading until next day's
            #          first session opens (bot keeps running, just idle).
            acct_now = get_account()
            now_utc  = datetime.now(tz=timezone.utc)

            if acct_now and not _drawdown_halt and not drawdown_off() and acct_now.get('balance', 0.0) >= DRAWDOWN_ENABLE_ABOVE_USC:
                _bal      = acct_now.get('balance', 0.0)
                _eq       = acct_now.get('equity',  0.0)
                _loss_usc = _bal - _eq                                   # InpNrmDrawdownUSC basis
                _loss_pct = ((_bal - _eq) / _bal * 100) if _bal > 0 else 0.0
                _hit_usc  = _loss_usc >= DRAWDOWN_LIMIT_USC
                _hit_pct  = _bal > 0 and _loss_pct >= DRAWDOWN_LIMIT_PCT
                if _hit_usc or _hit_pct:
                    _why = "abs USC" if _hit_usc else "percent"
                    log.critical(
                        f"DRAWDOWN LIMIT HIT ({_why}): loss={_loss_usc:.0f} USC / {_loss_pct:.1f}% "
                        f"(balance={_bal:.2f} equity={_eq:.2f} "
                        f"caps={DRAWDOWN_LIMIT_USC:.0f} USC / {DRAWDOWN_LIMIT_PCT}%) "
                        f"— closing all positions, halting until next day session"
                    )
                    _closed, _failed = close_all_bot_positions(_SYMBOLS)
                    for s in _SYMBOLS:
                        s['grid'].buy.reset()
                        s['grid'].sell.reset()
                    _drawdown_halt     = True
                    _drawdown_halt_day = now_utc.day
                    try:
                        notify_drawdown_triggered(
                            balance   = _bal,
                            equity    = _eq,
                            limit_pct = DRAWDOWN_LIMIT_PCT,
                            closed    = _closed,
                            currency  = acct_now.get('currency', 'USD'),
                        )
                    except Exception:
                        pass

            # ── DRAWDOWN HALT — skip trading, wait for next day session ──
            if _drawdown_halt:
                if now_utc.day != _drawdown_halt_day and in_session():
                    log.info("Drawdown halt cleared — new day session open, resuming trading")
                    _drawdown_halt = False
                    try:
                        from telegram_notifier import notify_drawdown_resumed
                        notify_drawdown_resumed()
                    except Exception:
                        pass
                else:
                    if scan_count % 6 == 0:
                        log.info(
                            f"Drawdown halt active (triggered UTC day {_drawdown_halt_day}) "
                            f"— waiting for next day session open"
                        )
                    time.sleep(SCAN_INTERVAL_SECONDS)
                    continue

            # ── FLOAT LOSS SOFT ALERT ────────────────────────────
            if acct_now:
                _bal_f = acct_now.get('balance', 0.0)
                _eq_f  = acct_now.get('equity',  0.0)
                _loss_f = ((_bal_f - _eq_f) / _bal_f * 100) if _bal_f > 0 else 0.0
                if _loss_f >= FLOAT_LOSS_ALERT_PCT and not _float_alerted:
                    _float_alerted = True
                    log.warning(f"FLOAT LOSS WARNING: {_loss_f:.1f}% of balance — sending alert")
                    try:
                        notify_float_loss_warning(
                            balance   = _bal_f,
                            equity    = _eq_f,
                            loss_pct  = _loss_f,
                            alert_pct = FLOAT_LOSS_ALERT_PCT,
                            currency  = acct_now.get('currency', 'USC'),
                            grid_summary = _grid_summary(),
                        )
                    except Exception:
                        pass
                elif _loss_f < FLOAT_LOSS_ALERT_PCT:
                    _float_alerted = False   # reset when loss recovers

            # ── PROFIT HIGH-WATER-MARK GIVEBACK ──────────────────
            # Track the peak REALIZED balance; if we give back too much of it,
            # pause new baskets to lock the remaining gains (recovery still runs).
            if acct_now and PROFIT_GIVEBACK_LIMIT_USC > 0:
                _bal_h = acct_now.get('balance', 0.0)
                if _bal_h > _peak_balance:
                    _peak_balance = _bal_h
                    _save_hwm(_peak_balance)
                _giveback = _peak_balance - _bal_h
                if _giveback >= PROFIT_GIVEBACK_LIMIT_USC and not _profit_lock_halt:
                    _profit_lock_halt = True
                    log.warning(
                        f"PROFIT LOCK: bal={_bal_h:.0f} is {_giveback:.0f} below peak "
                        f"{_peak_balance:.0f} (limit {PROFIT_GIVEBACK_LIMIT_USC:.0f}) — pausing new baskets"
                    )
                    try:
                        notify_profit_lock(_bal_h, _peak_balance, _giveback,
                                           PROFIT_GIVEBACK_LIMIT_USC, acct_now.get('currency', 'USC'))
                    except Exception:
                        pass
                elif _profit_lock_halt and _giveback <= PROFIT_GIVEBACK_LIMIT_USC * 0.5:
                    _profit_lock_halt = False
                    log.info(f"Profit lock cleared — balance {_bal_h:.0f} recovered (peak {_peak_balance:.0f})")

            # ── DAILY & SESSION TRACKING ──────────────────────────
            if acct_now:
                now_utc = datetime.now(tz=timezone.utc)

                # Reset daily baseline at midnight UTC
                if now_utc.day != _last_day:
                    _last_day          = now_utc.day
                    _today_str         = now_utc.strftime("%Y-%m-%d")
                    _a_date, _a_bal    = load_day_anchor()
                    if _a_date == _today_str and _a_bal is not None:
                        _day_start_balance = _a_bal            # same UTC day across a restart: keep true-midnight anchor
                        _reset_kind        = "anchor loaded (persisted)"
                    else:
                        _day_start_balance = acct_now.get('balance', 0.0)
                        save_day_anchor(_today_str, _day_start_balance)
                        _reset_kind        = "new day, saved"
                        _peak_balance      = _day_start_balance   # re-baseline HWM each new UTC day so the profit-lock cannot deadlock while flat
                        _save_hwm(_peak_balance)
                        _profit_lock_halt  = False
                    _profit_halt       = False
                    _weekend_handled   = False
                    _withdraw_reminded = False
                    log.info(f"Day reset — day_start_balance={_day_start_balance:.2f} ({_reset_kind})")

                cur_sess = _active_session()

                # Session just started
                if cur_sess is not None and not _was_in_session:
                    _was_in_session       = True
                    _session_name         = cur_sess['name']
                    _session_start_equity = acct_now.get('equity', 0.0)
                    log.info(f"Session started: {_session_name} | equity={_session_start_equity:.2f}")

                # Session just ended — send Telegram summary
                elif cur_sess is None and _was_in_session:
                    _was_in_session = False
                    bal      = acct_now.get('balance', 0.0)
                    eq       = acct_now.get('equity',  0.0)
                    floating = acct_now.get('profit',  0.0)
                    curr     = acct_now.get('currency', 'USD')
                    sess_pnl = eq - _session_start_equity
                    day_pnl  = bal - _day_start_balance
                    log.info(
                        f"Session ended: {_session_name} | "
                        f"sess_pnl={sess_pnl:+.2f} day_pnl={day_pnl:+.2f} bal={bal:.2f}"
                    )
                    try:
                        notify_session_summary(
                            session_name      = _session_name,
                            session_profit    = sess_pnl,
                            day_profit        = day_pnl,
                            day_start_balance = _day_start_balance,
                            balance           = bal,
                            equity            = eq,
                            floating          = floating,
                            currency          = curr,
                            grid_summary      = _grid_summary(),
                        )
                    except Exception as _e:
                        log.error(f"Session summary notify failed: {_e}")

                # ── DAILY PROFIT TARGET CHECK ─────────────────────
                # DAILY_PROFIT_TARGET_USC <= 0 disables it (test phase).
                if not _profit_halt and _day_start_balance > 0 and DAILY_PROFIT_TARGET_USC > 0:
                    _day_pnl = acct_now.get('balance', 0.0) - _day_start_balance
                    if _day_pnl >= DAILY_PROFIT_TARGET_USC:
                        _profit_halt = True
                        log.info(
                            f"DAILY PROFIT TARGET HIT: day_pnl={_day_pnl:+.2f} "
                            f">= {DAILY_PROFIT_TARGET_USC} USC — halting new L1s for today"
                        )
                        try:
                            notify_daily_profit_target(
                                day_profit   = _day_pnl,
                                target       = DAILY_PROFIT_TARGET_USC,
                                balance      = acct_now.get('balance', 0.0),
                                currency     = acct_now.get('currency', 'USC'),
                                grid_summary = _grid_summary(),
                            )
                        except Exception:
                            pass

                # ── WEEKEND CLOSE / ALERT ─────────────────────────
                # Friday only, once per day
                if now_utc.weekday() == 4 and not _weekend_handled:
                    _utc_h = now_utc.hour + now_utc.minute / 60.0
                    if _utc_h >= WEEKEND_CLOSE_UTC_HOUR:
                        _weekend_handled = True
                        _w_bal  = acct_now.get('balance', 0.0)
                        _w_eq   = acct_now.get('equity',  0.0)
                        _w_fl   = acct_now.get('profit',  0.0)
                        _w_curr = acct_now.get('currency', 'USC')
                        if _w_fl >= 0:
                            log.info(f"WEEKEND CLOSE: floating={_w_fl:+.2f} — closing all positions")
                            _w_closed, _ = close_all_bot_positions(_SYMBOLS)
                            for s in _SYMBOLS:
                                s['grid'].buy.reset()
                                s['grid'].sell.reset()
                        else:
                            log.info(f"WEEKEND ALERT: floating={_w_fl:+.2f} — in loss, carrying over")
                            _w_closed = 0
                        try:
                            notify_weekend_status(
                                balance   = _w_bal,
                                equity    = _w_eq,
                                floating  = _w_fl,
                                currency  = _w_curr,
                                grid_summary = _grid_summary(),
                                closed    = _w_closed,
                                in_profit = _w_fl >= 0,
                            )
                        except Exception:
                            pass
                        # Weekly withdrawal reminder — bank the profit above base
                        _sugg = _w_bal - WEEKLY_WITHDRAW_BASE_USC
                        if WEEKLY_WITHDRAW_BASE_USC > 0 and _sugg > 0 and not _withdraw_reminded:
                            _withdraw_reminded = True
                            log.info(f"Weekly withdrawal reminder: suggest {_sugg:.0f} {_w_curr}")
                            try:
                                notify_withdraw_reminder(_w_bal, WEEKLY_WITHDRAW_BASE_USC, _sugg, _w_curr)
                            except Exception:
                                pass

            # ── SESSION & RECOVERY LOGIC ──────────────────────────
            # SESSION_FILTER_ENABLED=True (Trading-Rules #1/#2): new L1 entries only
            # inside the 04:30-17:30 UTC window AND on weekdays (Mon-Fri). Recovery of
            # existing baskets always runs 24/7. False = pure EA (24/7 new entries).
            if SESSION_FILTER_ENABLED:
                is_weekday  = datetime.now(tz=timezone.utc).weekday() < 5
                sess_active = in_session() and is_weekday
                sess_wind   = in_winddown()
            else:
                sess_active = True
                sess_wind   = False

            for s in _SYMBOLS:
                g    = s['grid']
                sym  = s['symbol_live']
                lbl  = s['cfg']['label']

                # Spread check — applied to both new entries and recovery
                tick        = get_tick(sym)
                spread      = tick.get('spread_points', 0) if tick else 0
                spread_wide = spread > MAX_SPREAD_POINTS

                if spread_wide and scan_count % 6 == 0:
                    log.warning(f"[{lbl}] Spread {spread}pts > {MAX_SPREAD_POINTS}pts limit")

                # H1 trend — new baskets only WITH the trend (FLAT = both allowed)
                trend = get_trend(sym, TREND_TIMEFRAME, TREND_EMA_PERIOD) if TREND_FILTER_ENABLED else "FLAT"

                # New L1 entries — gated by session, PAUSE flag, PROFIT LOCK, profit halt, spread, news, trend, opposite side
                if sess_active and not sess_wind:
                    if new_entries_paused():
                        if scan_count % 6 == 0 and (not g.buy.active or not g.sell.active):
                            log.info(f"[{lbl}] New trades PAUSED — recovery only; existing baskets keep running")
                    elif _profit_lock_halt:
                        if scan_count % 6 == 0 and (not g.buy.active or not g.sell.active):
                            log.info(f"[{lbl}] PROFIT LOCK active — new baskets paused (recovery continues)")
                    elif not g.buy.active or not g.sell.active:
                        if _profit_halt:
                            if scan_count % 6 == 0:
                                log.info(f"[{lbl}] Daily profit target hit — no new L1 entries today")
                        elif spread_wide:
                            pass  # already warned above
                        else:
                            blocked, reason = is_news_blackout(NEWS_BLACKOUT_MINUTES)
                            if blocked:
                                if scan_count % 6 == 0:
                                    log.warning(f"[{lbl}] {reason} — L1 entry blocked")
                            else:
                                if not g.buy.active:
                                    if TREND_FILTER_ENABLED and not allows("BUY", trend):
                                        if scan_count % 6 == 0:
                                            log.info(f"[{lbl}] Trend {trend} — BUY L1 blocked (with-trend only)")
                                    elif g.sell.level_count >= OPPOSITE_SIDE_BLOCK_LEVEL:
                                        if scan_count % 6 == 0:
                                            log.warning(
                                                f"[{lbl}] SELL at L{g.sell.level_count} "
                                                f">= {OPPOSITE_SIDE_BLOCK_LEVEL} — BUY L1 blocked"
                                            )
                                    else:
                                        log.info(f"[{lbl}] BUY side inactive — starting new L1 entry (trend {trend})")
                                        g.start_side(sym, "BUY")
                                if not g.sell.active:
                                    if TREND_FILTER_ENABLED and not allows("SELL", trend):
                                        if scan_count % 6 == 0:
                                            log.info(f"[{lbl}] Trend {trend} — SELL L1 blocked (with-trend only)")
                                    elif g.buy.level_count >= OPPOSITE_SIDE_BLOCK_LEVEL:
                                        if scan_count % 6 == 0:
                                            log.warning(
                                                f"[{lbl}] BUY at L{g.buy.level_count} "
                                                f">= {OPPOSITE_SIDE_BLOCK_LEVEL} — SELL L1 blocked"
                                            )
                                    else:
                                        log.info(f"[{lbl}] SELL side inactive — starting new L1 entry (trend {trend})")
                                        g.start_side(sym, "SELL")

                # Recovery — spread check only, runs 24/7 (continues on existing baskets even after the daily profit halt; only NEW L1 is blocked)
                if g.buy.active or g.sell.active:
                    if not spread_wide:
                        g.check_recovery(sym)

            if scan_count % 6 == 0:
                if sess_active:
                    if sess_wind:
                        cur = _active_session()
                        log.info(f"Winddown ({cur['winddown']:.1f}-{cur['end']:.1f} UTC) — recovery only")
                else:
                    windows = ", ".join(f"{s['start']:.0f}-{s['end']:.0f}" for s in SESSIONS_UTC)
                    log.info(f"Outside kill zones ({windows} UTC) — waiting...")

            if scan_count % 6 == 0:
                log_scan_status()

            if scan_count % 60 == 0:
                print_status()

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
            try:
                notify_error(str(e))
            except Exception:
                pass

        time.sleep(SCAN_INTERVAL_SECONDS)

    try:
        notify_bot_stopped()
    except Exception:
        pass
    log.info("TharEX Bot stopped.")
    print_status()
    disconnect()


if __name__ == "__main__":
    main()
