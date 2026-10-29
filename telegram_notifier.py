"""
Telegram Notifier — TharEX Bot
Writes base64-encoded messages to a queue file read by the shell daemon.
Tag: [HEDGE]
"""

import base64
import os
from config import TELEGRAM_QUEUE

_TAG = "[HEDGE] "


def _enqueue(text: str):
    """Encode message as base64 and append to queue file for daemon pickup."""
    os.makedirs(os.path.dirname(TELEGRAM_QUEUE) if os.path.dirname(TELEGRAM_QUEUE) else ".", exist_ok=True)
    encoded = base64.b64encode((_TAG + text).encode("utf-8")).decode("ascii")
    with open(TELEGRAM_QUEUE, "a") as f:
        f.write(encoded + "\n")


def notify_bot_started(login, server, balance, currency, symbols_config: list):
    sym_lines = "\n".join(
        f"  {sc['label']}: lot={sc['base_lot']} "
        f"grid={sc['grid_spacing']} maxLots={sc.get('max_lot_cap')} "
        f"maxLevels={sc.get('max_trades')}"
        for sc in symbols_config
    )
    _enqueue(
        f"TharEX Bot STARTED\n"
        f"Account: {login} @ {server}\n"
        f"Balance: {balance:.2f} {currency}\n"
        f"Symbols:\n{sym_lines}\n"
        f"Mode: TharEX v1.05 exact | window 05:30-15:00 UTC = 11:00-20:30 IST (Mon-Fri)"
    )


def notify_bot_stopped():
    _enqueue("TharEX Bot STOPPED")


def notify_connection_lost():
    _enqueue("MT5 CONNECTION LOST — reconnecting...")


def notify_connection_restored(login, server):
    _enqueue(f"MT5 Connection restored: {login} @ {server}")


def notify_error(message: str):
    _enqueue(f"ERROR: {message}")


def notify_drawdown_triggered(balance: float, equity: float,
                              limit_pct: float, closed: int, currency: str):
    loss_pct = (balance - equity) / balance * 100 if balance > 0 else 0
    _enqueue(
        f"⛔ DRAWDOWN LIMIT HIT — Trading Halted\n"
        f"{'─' * 28}\n"
        f"Balance  : {balance:.2f} {currency}\n"
        f"Equity   : {equity:.2f} {currency}\n"
        f"Loss     : {loss_pct:.1f}% of balance (limit: {limit_pct}%)\n"
        f"{'─' * 28}\n"
        f"Closed {closed} positions.\n"
        f"Bot resumes at next day market open."
    )


def notify_drawdown_resumed():
    _enqueue(
        f"✅ Drawdown Halt Cleared\n"
        f"New day session open — trading resumed."
    )


def notify_grid_depth_alert(label: str, side: str, level: int,
                            balance: float, equity: float,
                            floating: float, currency: str):
    _enqueue(
        f"⚠️ GRID DEPTH ALERT — {label} {side} at L{level}\n"
        f"{'─' * 28}\n"
        f"💰 Balance  : {balance:.2f} {currency}\n"
        f"⚖️  Equity   : {equity:.2f} {currency}\n"
        f"📉 Floating : {floating:+.2f} {currency}\n"
        f"{'─' * 28}\n"
        f"Monitor closely — recovery in progress."
    )


def notify_daily_profit_target(day_profit: float, target: float,
                                balance: float, currency: str, grid_summary: str):
    _enqueue(
        f"🎯 DAILY PROFIT TARGET HIT\n"
        f"{'─' * 28}\n"
        f"Day P&L  : +{day_profit:.2f} {currency}\n"
        f"Target   : +{target:.2f} {currency}\n"
        f"Balance  : {balance:.2f} {currency}\n"
        f"{'─' * 28}\n"
        f"No new L1 entries for today.\n"
        f"Recovery continues: {grid_summary}\n"
        f"Resumes tomorrow at London Open."
    )


def notify_weekend_status(balance: float, equity: float, floating: float,
                          currency: str, grid_summary: str,
                          closed: int, in_profit: bool):
    if in_profit:
        _enqueue(
            f"🔒 WEEKEND CLOSE — Positions Locked In\n"
            f"{'─' * 28}\n"
            f"💰 Balance  : {balance:.2f} {currency}\n"
            f"📈 Floating : {floating:+.2f} {currency}\n"
            f"{'─' * 28}\n"
            f"Closed {closed} positions — profit secured.\n"
            f"Trading resumes Monday London Open (07:00 UTC)."
        )
    else:
        _enqueue(
            f"⚠️ WEEKEND ALERT — Positions Carrying Over\n"
            f"{'─' * 28}\n"
            f"💰 Balance  : {balance:.2f} {currency}\n"
            f"⚖️  Equity   : {equity:.2f} {currency}\n"
            f"📉 Floating : {floating:+.2f} {currency}\n"
            f"{'─' * 28}\n"
            f"In loss — positions kept open for recovery.\n"
            f"Open grids: {grid_summary}\n"
            f"No new entries until Monday London Open."
        )


def notify_float_loss_warning(balance: float, equity: float, loss_pct: float,
                               alert_pct: float, currency: str, grid_summary: str):
    _enqueue(
        f"🚨 FLOAT LOSS WARNING — {loss_pct:.1f}% of Balance\n"
        f"{'─' * 28}\n"
        f"💰 Balance  : {balance:.2f} {currency}\n"
        f"⚖️  Equity   : {equity:.2f} {currency}\n"
        f"📉 Floating : {equity - balance:+.2f} {currency}\n"
        f"{'─' * 28}\n"
        f"Alert at {alert_pct}% | Hard stop at 30%\n"
        f"Open grids: {grid_summary}\n"
        f"Consider manual review."
    )


def notify_profit_lock(balance: float, peak: float, giveback: float,
                       limit: float, currency: str):
    _enqueue(
        f"🔒 PROFIT LOCK — New Trades Paused\n"
        f"{'─' * 28}\n"
        f"Peak balance : {peak:.0f} {currency}\n"
        f"Now          : {balance:.0f} {currency}\n"
        f"Given back   : {giveback:.0f} {currency} (limit {limit:.0f})\n"
        f"{'─' * 28}\n"
        f"No new baskets until balance recovers. Open baskets still run.\n"
        f"Consider withdrawing your remaining profit to your bank."
    )


def notify_withdraw_reminder(balance: float, base: float, suggested: float, currency: str):
    _enqueue(
        f"🏦 WEEKLY WITHDRAWAL REMINDER\n"
        f"{'─' * 28}\n"
        f"Balance : {balance:.0f} {currency}\n"
        f"Base    : {base:.0f} {currency}\n"
        f"{'─' * 28}\n"
        f"Suggested withdrawal: {suggested:.0f} {currency}\n"
        f"Move it to your bank — profit is only YOURS once it leaves the account."
    )


def notify_session_summary(session_name: str, session_profit: float,
                           day_profit: float, day_start_balance: float,
                           balance: float, equity: float, floating: float,
                           currency: str, grid_summary: str):
    day_pct      = (day_profit / day_start_balance * 100) if day_start_balance > 0 else 0.0
    sess_icon    = "✅" if session_profit >= 0 else "❌"
    day_icon     = "✅" if day_profit >= 0 else "❌"
    float_icon   = "📈" if floating >= 0 else "📉"

    _enqueue(
        f"📊 Session Closed — {session_name}\n"
        f"{'─' * 28}\n"
        f"{sess_icon} Session P&L : {session_profit:+.2f} {currency}\n"
        f"{day_icon} Today's P&L  : {day_profit:+.2f} {currency} ({day_pct:+.2f}%)\n"
        f"{'─' * 28}\n"
        f"💰 Balance    : {balance:.2f} {currency}\n"
        f"⚖️  Equity     : {equity:.2f} {currency}\n"
        f"{float_icon} Floating   : {floating:+.2f} {currency}\n"
        f"{'─' * 28}\n"
        f"📌 Open Grids : {grid_summary}"
    )


def notify_hedge_sweep(label: str, count: int):
    _enqueue(
        f"🧹 HEDGE SWEEP — {label}\n"
        f"Basket closed on its TP; swept {count} un-peeled hedge(s) at market."
    )
