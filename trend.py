"""
Trend Filter — TharEX Bot
Higher-timeframe (default H1) EMA trend so the grid never opens a new basket
AGAINST a strong trend (the one-way days that blew the account). New baskets are
allowed only WITH the trend; ranging (FLAT) allows both sides — the grid's best
condition. Recovery of already-open baskets is NOT gated here (the level cap +
equity stop bound that).
"""

import logging
import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

_TF = {
    "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
}


def _ema(vals: list, period: int) -> float:
    k = 2.0 / (period + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def get_trend(symbol: str, tf_name: str = "H1", period: int = 50) -> str:
    """Return 'UP', 'DOWN' or 'FLAT'. Requires BOTH price-vs-EMA and EMA slope to
    agree, so we only call a trend when it's real (else FLAT = don't filter)."""
    tf = _TF.get(tf_name, mt5.TIMEFRAME_H1)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, period * 3)
    if rates is None or len(rates) < period + 5:
        return "FLAT"   # no data -> don't block trading
    closes = [float(r["close"]) for r in rates]
    ema_now = _ema(closes, period)
    ema_prev = _ema(closes[:-3], period)   # EMA a few bars ago (slope)
    price = closes[-1]
    if price > ema_now and ema_now >= ema_prev:
        return "UP"
    if price < ema_now and ema_now <= ema_prev:
        return "DOWN"
    return "FLAT"


def allows(side: str, trend: str) -> bool:
    """Is opening a NEW basket on `side` allowed under `trend`?
    Trend-aligned only: UP -> BUY only, DOWN -> SELL only, FLAT -> both."""
    if trend == "UP":
        return side == "BUY"
    if trend == "DOWN":
        return side == "SELL"
    return True
