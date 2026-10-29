"""
MT5 Connector — TharEX Bot
Handles connection, login, tick data, order placement and position management.
"""

import MetaTrader5 as mt5
import logging
from datetime import datetime, timezone
from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH, MT5_TIMEOUT, SYMBOLS_CONFIG

try:
    # VPS / shared-host safety: when True, connect() ONLY launches MT5_PATH and logs
    # into the configured account — it never attaches to an already-open terminal
    # (which, on a host that also runs live bots, could be a different account).
    from config import MT5_FORCE_LOGIN
except ImportError:
    MT5_FORCE_LOGIN = False

try:
    # Launch the terminal in /portable mode (data kept inside the terminal dir).
    # REQUIRED on the VPS/wine fleet (matches how those terminals run); without it
    # the MT5 Python IPC pipe times out (-10005). Leave False on a local desktop
    # where you attach to an already-open non-portable terminal.
    from config import MT5_PORTABLE
except ImportError:
    MT5_PORTABLE = False

logger = logging.getLogger(__name__)

_SYMBOL_CACHE: dict = {}   # {label: resolved_broker_symbol}


def resolve_symbol(config_symbol: str, label: str) -> str:
    """Resolve a symbol name against the broker's actual market watch list.
    Tries the configured name first, then common suffix variants, then
    falls back to any symbol whose name contains the base label.
    """
    global _SYMBOL_CACHE
    if label in _SYMBOL_CACHE:
        return _SYMBOL_CACHE[label]

    base = label.upper().split(".")[0]   # "XAUUSD.pc" → "XAUUSD"
    candidates = [
        config_symbol, base,
        base + "+", base + ".pc", base + ".sc", base + "c",
        base + "m", base + ".pro",
    ]
    candidates = list(dict.fromkeys(candidates))    # deduplicate, preserve order

    all_syms  = mt5.symbols_get()
    all_names = [s.name for s in all_syms]
    tradeable = {s.name for s in all_syms if s.trade_mode != 0}

    for c in candidates:
        if c in tradeable:
            mt5.symbol_select(c, True)
            _SYMBOL_CACHE[label] = c
            logger.info(f"Symbol resolved [{label}]: {c}")
            return c

    for s in all_names:
        if base in s.upper() and s in tradeable:
            mt5.symbol_select(s, True)
            _SYMBOL_CACHE[label] = s
            logger.info(f"Symbol fallback [{label}]: {s}")
            return s

    raise ValueError(f"{label} symbol not found on this broker")


def get_symbol() -> str:
    """Backward-compat shim — resolves the first symbol in SYMBOLS_CONFIG."""
    cfg = SYMBOLS_CONFIG[0]
    return resolve_symbol(cfg["symbol"], cfg["label"])


def connect() -> bool:
    mt5.shutdown()
    # Local-desktop friendly: try to ATTACH to an already-running, logged-in
    # terminal first (bare initialize), then a non-portable launch+login at
    # MT5_PATH, then a plain launch. (The live VPS version used portable=True;
    # that spawns a second instance and conflicts with a desktop terminal.)
    if MT5_FORCE_LOGIN:
        # Deterministic: launch the configured terminal and log into the configured
        # account ONLY. No attach (shared host may have other accounts open).
        attempts = [
            ("path+login", lambda: mt5.initialize(path=MT5_PATH, login=int(MT5_LOGIN),
                                                  password=MT5_PASSWORD, server=MT5_SERVER,
                                                  portable=MT5_PORTABLE, timeout=MT5_TIMEOUT)),
            ("attach",     lambda: mt5.initialize(portable=MT5_PORTABLE)),
        ]
    else:
        attempts = [
            ("attach",        lambda: mt5.initialize()),
            ("path+login",    lambda: mt5.initialize(path=MT5_PATH, login=int(MT5_LOGIN),
                                                      password=MT5_PASSWORD, server=MT5_SERVER,
                                                      portable=MT5_PORTABLE, timeout=MT5_TIMEOUT)),
            ("path",          lambda: mt5.initialize(path=MT5_PATH, portable=MT5_PORTABLE,
                                                      timeout=MT5_TIMEOUT)),
        ]
    ok = False
    for name, attempt in attempts:
        try:
            ok = attempt()
        except Exception as e:
            ok = False
            logger.warning(f"MT5 initialize [{name}] raised: {e}")
        if ok:
            logger.info(f"MT5 initialize [{name}] OK")
            break
        logger.warning(f"MT5 initialize [{name}] failed: {mt5.last_error()}")
    if not ok:
        logger.error(f"MT5 initialize failed (all attempts): {mt5.last_error()}")
        return False

    info = mt5.account_info()
    if not info:
        logger.error(f"Account info failed: {mt5.last_error()}")
        return False

    if info.login != MT5_LOGIN:
        logger.warning(f"Connected to {info.login} but expected {MT5_LOGIN} — switching account...")
        switched = mt5.login(int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER)
        if not switched:
            logger.error(f"Login to {MT5_LOGIN} failed: {mt5.last_error()}")
            return False
        info = mt5.account_info()

    # HARD ACCOUNT GUARD — never operate on anything but the configured account.
    # Critical on a shared host that also runs live-money bots: if the terminal is
    # somehow on a different login, refuse to trade and disconnect.
    if not info or info.login != MT5_LOGIN:
        logger.critical(
            f"ACCOUNT GUARD TRIPPED: connected to {getattr(info, 'login', None)} "
            f"but configured for {MT5_LOGIN} — refusing to trade, disconnecting"
        )
        mt5.shutdown()
        return False

    logger.info(f"Connected: {info.login} @ {info.server} | Balance: {info.balance} {info.currency}")
    return True


def disconnect():
    mt5.shutdown()


def is_connected() -> bool:
    return mt5.terminal_info() is not None


def get_account() -> dict:
    info = mt5.account_info()
    if not info:
        return {}
    return {
        "login":    info.login,
        "balance":  info.balance,
        "equity":   info.equity,
        "profit":   info.profit,
        "currency": info.currency,
        "server":   info.server,
        "leverage": info.leverage,
    }


def get_tick(symbol: str) -> dict:
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return {}
    info  = mt5.symbol_info(symbol)
    point = info.point if info else 0.00001
    return {
        "bid":           tick.bid,
        "ask":           tick.ask,
        "spread_points": round((tick.ask - tick.bid) / point),
        "time":          datetime.fromtimestamp(tick.time, tz=timezone.utc),
    }


def get_open_positions(symbol: str, magic: int) -> list:
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    return [p for p in positions if p.magic == magic]


def get_all_open_positions(symbol: str, magic_buy: int, magic_sell: int) -> list:
    """Return all positions belonging to this bot (both sides)."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    return [p for p in positions if p.magic in (magic_buy, magic_sell)]


def _get_filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if not info:
        return mt5.ORDER_FILLING_FOK
    fm = info.filling_mode
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def place_order(symbol: str, order_type: str, lot: float,
                magic: int, comment: str = "", sl: float = 0.0, tp: float = 0.0) -> dict:
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return {"success": False, "error": "No tick data"}

    price = tick.ask if order_type == "BUY" else tick.bid
    otype = mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       round(float(lot), 2),
        "type":         otype,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "magic":        magic,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _get_filling_mode(symbol),
    }

    result = mt5.order_send(request)
    if result is None:
        return {"success": False, "error": str(mt5.last_error())}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "retcode": result.retcode, "error": result.comment}

    # Vantage returns result.price == 0.00 on this fill mode; fall back to the
    # request ask/bid so weighted-avg/TP stay sane (0.00 -> garbage TP -> Invalid-stops flood).
    fill_price = result.price if result.price else price
    return {"success": True, "ticket": result.order, "price": fill_price, "volume": result.volume}


def modify_position_tp(symbol: str, ticket: int, new_tp: float) -> bool:
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 2
    new_tp = round(new_tp, digits)

    # Skip the modify entirely if the position's TP is already at the target.
    # Sending an unchanged TP still hits the trade server (it replies "No changes")
    # and is exactly what triggered the broker's HFT / order-limit warning. Only
    # send a request when the TP is genuinely changing.
    pos = mt5.positions_get(ticket=ticket)
    if pos and round(pos[0].tp, digits) == new_tp:
        return True

    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   symbol,
        "position": ticket,
        "sl":       0.0,
        "tp":       new_tp,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return True
    err = result.comment if result else str(mt5.last_error())
    if err == "No changes":
        return True
    logger.warning(f"Failed to modify TP #{ticket} to {new_tp}: {err}")
    return False


def modify_position_sl(symbol: str, ticket: int, new_sl: float) -> bool:
    """Set a stop-loss on a position (keeps its tp as-is). Used for the break-even
    stops placed on retired hedges."""
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 2
    new_sl = round(new_sl, digits)
    pos = mt5.positions_get(ticket=ticket)
    if pos and round(pos[0].sl, digits) == new_sl:
        return True
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   symbol,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos[0].tp if pos else 0.0,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return True
    err = result.comment if result else str(mt5.last_error())
    if err == "No changes":
        return True
    logger.warning(f"Failed to modify SL #{ticket} to {new_sl}: {err}")
    return False


def get_positions_profit(symbol: str, magics) -> float:
    """Sum the floating profit of open positions on `symbol` whose magic is in `magics`."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return 0.0
    mset = set(m for m in magics if m is not None)
    return float(sum(p.profit for p in positions if p.magic in mset))


def get_symbol_digits(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    return info.digits if info else 5


def profit_per_dollar_usc(symbol: str, lots: float) -> float:
    """Account-currency profit (USC on a cent account) for a +$1.00 price move
    on `lots`, via MT5's own order_calc_profit — so the money-target basket TP
    is correct for cent vs standard accounts without hard-coding contract sizes.
    Magnitude is direction-independent, so we use a BUY calc and take abs()."""
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    ref = (tick.ask if tick and tick.ask else
           (info.ask if info and info.ask else 1000.0)) or 1000.0
    p = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, float(lots), ref, ref + 1.0)
    if p is None:
        logger.warning(f"order_calc_profit failed for {symbol} {lots}L: {mt5.last_error()}")
        return 0.0
    return abs(p)


def close_all_bot_positions(symbols: list) -> tuple:
    """Close every open position belonging to this bot across all symbols.

    symbols: list of {symbol_live: str, cfg: {magic_buy, magic_sell, ...}}
    Returns (closed_count, failed_count).
    """
    closed = 0
    failed = 0
    for s in symbols:
        sym    = s['symbol_live']
        cfg    = s['cfg']
        # Include the hedge magics so a drawdown/weekend close also flattens hedges.
        magics = tuple(m for m in (cfg.get('magic_buy'), cfg.get('magic_sell'),
                                   cfg.get('magic_hedge_buy'), cfg.get('magic_hedge_sell'))
                       if m is not None)
        positions = mt5.positions_get(symbol=sym)
        if not positions:
            continue
        for p in positions:
            if p.magic not in magics:
                continue
            result = close_position(p.ticket, sym, p.volume, p.type)
            if result.get('success'):
                closed += 1
                logger.info(f"Drawdown close: #{p.ticket} {sym} {p.volume}L")
            else:
                failed += 1
                logger.error(f"Drawdown close FAILED #{p.ticket}: {result.get('error')}")
    return closed, failed


def close_position(ticket: int, symbol: str, lot: float, order_type: int) -> dict:
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return {"success": False, "error": "No tick data"}

    close_type = mt5.ORDER_TYPE_SELL if order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price      = tick.bid if order_type == mt5.ORDER_TYPE_BUY else tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       round(float(lot), 2),
        "type":         close_type,
        "position":     ticket,
        "price":        price,
        "magic":        0,
        "comment":      "TharEX basket close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else str(mt5.last_error())
        return {"success": False, "error": err}
    return {"success": True, "ticket": ticket}


def close_ticket(symbol: str, ticket: int) -> dict:
    """Close a single position by ticket — looks up its live volume and side.
    Used by the hedge layer to sweep an individual hedge. Treats an
    already-closed ticket as success (it peeled off on its own TP)."""
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return {"success": True, "note": "already closed"}
    p = pos[0]
    return close_position(p.ticket, symbol, p.volume, p.type)
