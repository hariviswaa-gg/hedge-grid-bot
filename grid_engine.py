"""
Grid Engine — TharEX Bot
Exact reimplementation of TharEX EA v1.05 "Normal" (Nrm) mode, calibrated 1:1
from the official .set file (InpNrm* / InpLot* inputs).

Per side (BUY and SELL run as independent baskets — InpDirection=0 = both):
  - Base lot InpLotSize = 0.01
  - A recovery level is added when price moves InpNrmGridUSD ($0.75 = 75 pts)
    against the last entry on that side (respecting a 1-min cooldown and the
    100-level / 1.0-lot caps).
  - Lot progression by LOT-VALUE bands (InpLotBands), NOT by level number:
        lot in 0.01–0.07 -> +0.01
        lot in 0.08–0.90 -> +0.02
        lot in 0.91–1.50 -> +0.03
    capped at InpMaxLotCap (1.0).
  - Basket take-profit is a MONEY target in USC keyed by the open-level count
    (InpNrmTPTable), linearly interpolated between anchors, then converted to a
    SINGLE shared price TP (InpSingleTPMode=true) via MT5's order_calc_profit so
    it is correct for cent vs standard accounts. Every position on the side
    carries that identical TP; it is recomputed whenever a level is added
    (weighted-avg entry, total lots and target all change).
  - No SL on any position (basket-managed).
"""

import logging
import json
import os
from datetime import datetime, timezone, timedelta
from config import TRADE_LOG
from mt5_connector import (
    place_order, get_open_positions, get_tick,
    modify_position_tp, get_symbol_digits, profit_per_dollar_usc,
    close_ticket, modify_position_sl, get_positions_profit
)

logger = logging.getLogger(__name__)

ENTRY_COMMENT = "TharEX EA V2"
HEDGE_COMMENT = "HEDGE"

# Defaults mirror the .set file; per-symbol overrides come from SYMBOLS_CONFIG.
DEFAULT_LOT_BANDS = [(0.01, 0.07, 0.01), (0.08, 0.90, 0.02), (0.91, 1.50, 0.03)]
DEFAULT_TP_TABLE  = [(1, 4), (2, 11), (3, 20), (6, 45), (8, 65), (12, 100),
                     (15, 135), (18, 200), (20, 350), (25, 550), (30, 800),
                     (35, 1200), (40, 2000)]
DEFAULT_MAX_LOT   = 1.0
DEFAULT_MAX_LEVELS = 100


def _step_for_lot(lot: float, bands: list) -> float:
    """InpLotBands: the step to add is chosen by which band the CURRENT lot is in."""
    for lo, hi, st in bands:
        if lo - 1e-9 <= lot <= hi + 1e-9:
            return st
    return bands[-1][2]   # above the top band -> keep last band's step


def get_lot(level: int, base_lot: float = 0.01,
            bands: list = None, cap: float = DEFAULT_MAX_LOT) -> float:
    """Lot for the Nth level, walking the value-bands from L1 (InpLotBands)."""
    bands = bands or DEFAULT_LOT_BANDS
    lot = round(base_lot, 2)
    for _ in range(1, max(1, level)):
        lot = round(lot + _step_for_lot(lot, bands), 2)
        if lot >= cap:
            return round(cap, 2)
    return round(min(lot, cap), 2)


def tp_target_usc(level: int, table: list = None) -> float:
    """InpNrmTPTable — target basket profit (USC) for `level` open positions.

    STEP/FLOOR lookup: use the value of the nearest anchor <= level (clamped at
    the ends). This was VERIFIED against 794 live TharEX baskets on proof account
    26445194 — non-anchor levels sit at the lower anchor, not on a linear ramp
    (e.g. L4/L5 target ~20 = anchor 3, L9-L11 target ~65 = anchor 8). Real gross
    lands ~85-95% of these values; the small haircut is close-side spread + the
    25-pt trigger buffer.
    """
    table = table or DEFAULT_TP_TABLE
    if level <= table[0][0]:
        return float(table[0][1])
    if level >= table[-1][0]:
        return float(table[-1][1])
    val = float(table[0][1])
    for lvl, v in table:
        if lvl <= level:
            val = float(v)
        else:
            break
    return val


class GridSide:
    """One side (BUY or SELL) of the dual basket-recovery grid."""

    def __init__(self, side: str, magic: int, cfg: dict):
        self.side       = side
        self.magic      = magic
        self.base_lot   = cfg['base_lot']
        self.spacing    = cfg['grid_spacing']                       # price units (0.75)
        self.bands      = cfg.get('lot_bands', DEFAULT_LOT_BANDS)
        self.cap        = cfg.get('max_lot_cap', DEFAULT_MAX_LOT)
        self.max_trades = cfg.get('max_trades', DEFAULT_MAX_LEVELS)
        self.tp_table   = cfg.get('tp_table', DEFAULT_TP_TABLE)
        self.cooldown   = cfg.get('cooldown_minutes', 0)
        self.tp_scale   = cfg.get('tp_target_scale', 1.0)   # 1.0=cent acct, 0.01=std USD
        self.active     = False
        self.levels     = []
        self.basket_tp  = 0.0
        self.last_entry_time = None      # datetime of the most recent level add

        # ── Hedge layer v2 (loss-prevention) ──────────────────────────
        # Every `hedge_step` levels (L5, L10, L15...) open ONE opposite "hedge"
        # sized so the CUMULATIVE hedge volume == cumulative basket volume — the
        # deep-side loss is then fully frozen (delta-neutral). The newest hedge
        # carries NO stop (it holds the freeze); when the next hedge is placed the
        # previous hedge(s) get a BREAK-EVEN stop (close at ~0 / least loss on a
        # pull-back). Once hedged, the basket drops its tiny per-cycle TP; the whole
        # structure closes together via DualGrid.check_master_exit() when it returns
        # to net profit.
        self.hedge_enabled = cfg.get('hedge_enabled', False)
        self.hedge_step    = cfg.get('hedge_trigger_step', cfg.get('hedge_trigger_level', 5))
        self.hedge_be_arm  = cfg.get('hedge_breakeven_arm_dist', cfg.get('grid_spacing', 0.75))
        self.hedge_side    = "BUY" if side == "SELL" else "SELL"   # opposite of this basket
        self.hedge_magic   = (cfg.get('magic_hedge_buy') if self.hedge_side == "BUY"
                              else cfg.get('magic_hedge_sell'))
        self.hedges        = []          # [{ticket, lot, price, level, sl}]
        self.refreeze_ref  = None        # price ref while deep & unhedged (re-freeze trigger)

        # ── v2.3 exit fixes (from the overnight test) ─────────────────
        self.hedge_hold     = cfg.get('hedge_hold_tp_until_unwound', False)   # Idea 1
        self.hedge_open_buf = cfg.get('hedge_open_buffer', 0.0)               # Idea 2
        self.hedge_skip_near_tp = cfg.get('hedge_skip_block_near_tp', False)  # Idea 3

    @property
    def level_count(self) -> int:
        return len(self.levels)

    @property
    def hedge_count(self) -> int:
        return len(self.hedges)

    @property
    def hedged(self) -> bool:
        return self.hedge_enabled and len(self.hedges) > 0

    @property
    def hold_tp(self) -> bool:
        """Idea 1: while ANY hedge is open (basket frozen), DON'T take the small
        basket TP — realizing it crystallizes the frozen loss. Exit via the
        net-profit master exit (DualGrid.check_hedged_net_exit) instead. Once all
        hedges have peeled (unwound), hold_tp is False and the normal TP resumes."""
        return self.hedge_enabled and self.hedge_hold and self.hedge_count > 0

    @property
    def next_level(self) -> int:
        return self.level_count + 1

    def lot_for(self, level: int) -> float:
        return get_lot(level, self.base_lot, self.bands, self.cap)

    def cooldown_ok(self) -> bool:
        if not self.cooldown or self.last_entry_time is None:
            return True
        elapsed = (datetime.now(tz=timezone.utc) - self.last_entry_time).total_seconds()
        return elapsed >= self.cooldown * 60

    def at_max(self) -> bool:
        return self.level_count >= self.max_trades

    def needs_recovery(self, current_price: float) -> bool:
        """True if price moved one grid step against the last open level."""
        if not self.levels:
            return False
        last_price = self.levels[-1]['price']
        if self.side == "BUY":
            return current_price <= last_price - self.spacing
        else:
            return current_price >= last_price + self.spacing

    def calculate_basket_tp(self, symbol: str) -> float:
        """Money-target TP (InpNrmTPTable) expressed as ONE shared price.

        Solve for the price P where total basket profit == target_usc. Profit is
        linear in P, so move = target / (USC profit per $1 move on total lots).
        """
        if not self.levels:
            return 0.0
        total_lots = sum(lv['lot'] for lv in self.levels)
        weighted_avg = sum(lv['lot'] * lv['price'] for lv in self.levels) / total_lots
        target = tp_target_usc(self.level_count, self.tp_table) * self.tp_scale

        ppd = profit_per_dollar_usc(symbol, total_lots)   # USC per +$1.00 move
        if ppd <= 0:
            return 0.0
        move = target / ppd

        if self.side == "BUY":
            return weighted_avg + move
        else:
            return weighted_avg - move

    def update_all_tps(self, symbol: str):
        tp = self.calculate_basket_tp(symbol)
        digits = get_symbol_digits(symbol)
        tp = round(tp, digits)
        self.basket_tp = tp

        # v2.1: the basket ALWAYS carries its (now high, faithful) TP — hedged or
        # not. The basket closes at its own TP; hedges peel off at their break-even
        # stops. (The high TP no longer churns, so there's nothing to suppress.)

        # If the basket is already in profit PAST its target, the TP would sit
        # behind the market and the broker rejects it ("Invalid stops"). Skip the
        # modify — DualGrid.check_take_profit() closes it at market instead.
        _tk = get_tick(symbol)
        if _tk:
            _close_px = _tk['bid'] if self.side == "BUY" else _tk['ask']
            if (self.side == "BUY" and _close_px >= tp) or (self.side == "SELL" and _close_px <= tp):
                return

        # Only push the TP to positions NOT already at the target value — re-sending
        # an identical TP to every position on every sync is what flooded the broker
        # with redundant order-modify requests (the HFT/limit warning). With this
        # guard a steady basket sends ZERO requests; only a new level (which shifts
        # the weighted-avg/target and thus the TP) triggers a one-time update.
        updated = 0
        for lv in self.levels:
            if round(lv.get('basket_tp', 0.0), digits) == tp:
                continue
            if modify_position_tp(symbol, lv['ticket'], tp):
                lv['basket_tp'] = tp
                updated += 1

        if updated:
            target = tp_target_usc(self.level_count, self.tp_table)
            logger.info(
                f"[{self.side}] Basket TP -> {tp:.{digits}f} "
                f"(target {target:.0f} USC | {updated} updated | levels: {self.level_count} "
                f"| total lots: {sum(lv['lot'] for lv in self.levels):.2f}L)"
            )

    def open_entry(self, symbol: str, level: int = None) -> bool:
        if level is None:
            level = self.next_level
        if self.at_max():
            logger.warning(f"[{self.side}] Max levels ({self.max_trades}) reached — skip add")
            return False
        lot = self.lot_for(level)

        result = place_order(
            symbol=symbol,
            order_type=self.side,
            lot=lot,
            magic=self.magic,
            comment=ENTRY_COMMENT,
            sl=0.0,
            tp=0.0
        )

        if not result.get("success"):
            logger.error(f"[{self.side}] Level {level} FAILED: {result.get('error')}")
            return False

        digits = get_symbol_digits(symbol)
        entry = {
            "ticket":    result["ticket"],
            "level":     level,
            "lot":       lot,
            "price":     result["price"],
            "comment":   ENTRY_COMMENT,
            "basket_tp": 0.0,
            "time":      datetime.now(tz=timezone.utc).isoformat(),
        }
        self.levels.append(entry)
        self.active = True
        self.last_entry_time = datetime.now(tz=timezone.utc)

        logger.info(
            f"[{self.side}] Level {level} OPENED | "
            f"Lot: {lot} | Price: {result['price']:.{digits}f} | Ticket: {result['ticket']}"
        )
        _log_trade(entry, self.side, "OPEN")
        self.update_all_tps(symbol)

        # v2: hedge once every `hedge_step` levels (L5, L10, L15 ...).
        if self.hedge_enabled and level >= self.hedge_step and level % self.hedge_step == 0:
            self._place_block_hedge(symbol, level)
        return True

    def _place_block_hedge(self, symbol: str, level: int):
        """At each hedge_step mark, open ONE new hedge sized so the TOTAL hedge
        volume equals the TOTAL basket volume — fully frozen. The hedge opens with
        NO stop; DualGrid.check_hedge_breakeven() arms a break-even stop at its
        entry once price has moved in its favour (so it can't trigger on the spread
        at open and peels off at ~0 on a pull-back)."""
        if self.hedge_magic is None:
            logger.error(f"[{self.side}] Hedge magic not configured — cannot hedge L{level}")
            return
        digits = get_symbol_digits(symbol)

        # Idea 3: don't freeze a huge new block when the basket is already within
        # one grid-step of its TP (about to resolve) — freezing then unwinding is
        # pure cost. (Rare at a fresh mark, where price has just moved AGAINST; acts
        # as a safe guard for the near-TP edge.)
        if self.hedge_skip_near_tp and self.basket_tp > 0:
            _tk = get_tick(symbol)
            if _tk:
                _px = _tk['bid'] if self.side == "BUY" else _tk['ask']
                _near = ((self.side == "BUY" and 0 <= self.basket_tp - _px <= self.spacing) or
                         (self.side == "SELL" and 0 <= _px - self.basket_tp <= self.spacing))
                if _near:
                    logger.info(
                        f"[{self.side}] Block hedge skipped at L{level} — basket within one "
                        f"grid-step of its TP ({_px:.{digits}f} vs {self.basket_tp:.{digits}f})"
                    )
                    return

        # Size the new hedge so cumulative hedge == cumulative basket (freeze).
        total_basket = round(sum(lv["lot"] for lv in self.levels), 2)
        total_hedge  = round(sum(h["lot"] for h in self.hedges), 2)
        new_lot = round(total_basket - total_hedge, 2)
        if new_lot <= 0:
            logger.info(f"[{self.side}] Block hedge skipped (basket {total_basket}L <= hedge {total_hedge}L)")
            return

        result = place_order(
            symbol=symbol, order_type=self.hedge_side, lot=new_lot,
            magic=self.hedge_magic, comment=HEDGE_COMMENT, sl=0.0, tp=0.0,
        )
        if not result.get("success"):
            logger.error(f"[{self.side}] BLOCK HEDGE at L{level} FAILED: {result.get('error')}")
            return

        # Idea 2 (armored hedge): give the hedge a PROTECTIVE stop the moment it
        # opens, at entry +/- buffer (buffer > spread so it can't trigger on open).
        # A sharp reversal then peels it at ~ -buffer instead of letting it ride into
        # a big loss (the SELL-L15 0.9L hedge swept at ~-$831). check_hedge_breakeven
        # later TIGHTENS this to the exact entry (true break-even) once price moves
        # favourably. If the broker rejects the stop (too close / fast market), fall
        # back to no stop — the break-even logic still arms it later.
        prot = 0.0
        if self.hedge_open_buf and self.hedge_open_buf > 0:
            if self.hedge_side == "SELL":          # SELL hedge: protective SL ABOVE entry
                _cand = round(result["price"] + self.hedge_open_buf, digits)
            else:                                  # BUY hedge: protective SL BELOW entry
                _cand = round(result["price"] - self.hedge_open_buf, digits)
            if modify_position_sl(symbol, result["ticket"], _cand):
                prot = _cand

        self.hedges.append({
            "ticket":  result["ticket"],
            "lot":     new_lot,
            "price":   result["price"],
            "level":   level,
            "sl":      prot,
            "time":    datetime.now(tz=timezone.utc).isoformat(),
        })
        logger.info(
            f"[{self.side}] BLOCK HEDGE opened ({self.hedge_side} {new_lot}L @ "
            f"{result['price']:.{digits}f}) at L{level} — basket {total_basket}L now "
            f"fully frozen ({self.hedge_count} hedge(s))"
            + (f" | protective stop {prot:.{digits}f}" if prot else " | no protective stop (broker rejected)")
        )

    def sweep_hedges(self, symbol: str) -> int:
        """Close every open hedge on this side at market (the master exit for
        hedges that did not peel off on their own tp). Returns the count closed."""
        closed = 0
        for h in list(self.hedges):
            res = close_ticket(symbol, h["ticket"])
            if res.get("success"):
                closed += 1
                logger.info(f"[{self.side}] HEDGE swept #{h['ticket']} ({h['lot']}L)")
            else:
                logger.error(f"[{self.side}] HEDGE sweep FAILED #{h['ticket']}: {res.get('error')}")
        self.hedges = []
        return closed

    def close_basket_at_market(self, symbol: str) -> tuple:
        """Close every level on this side at market (active take-profit), then
        sweep this side's hedges (master exit). Returns (levels_closed, hedges_swept)."""
        closed = 0
        for lv in list(self.levels):
            if close_ticket(symbol, lv['ticket']).get('success'):
                closed += 1
            else:
                logger.error(f"[{self.side}] market close FAILED #{lv['ticket']}")
        swept = self.sweep_hedges(symbol)
        self.levels          = []
        self.active          = False
        self.basket_tp       = 0.0
        self.last_entry_time = None
        return closed, swept

    def sync_with_mt5(self, symbol: str):
        """Rebuild grid state from live MT5 positions (for restarts/reconnects).
        Levels are assigned by open-time order (1..N); lot is the position's real
        volume, so the TP recompute always uses the true basket."""
        positions = get_open_positions(symbol, self.magic)
        self.levels = []
        ordered = sorted(positions, key=lambda x: x.time)
        for i, p in enumerate(ordered, start=1):
            self.levels.append({
                "ticket":    p.ticket,
                "level":     i,
                "lot":       p.volume,
                "price":     p.price_open,
                "comment":   p.comment,
                "basket_tp": p.tp,
                "time":      datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            })

        self.active = len(self.levels) > 0
        self.basket_tp = self.levels[0]['basket_tp'] if self.levels else 0.0
        if ordered:
            # MT5 position.time is in BROKER-SERVER time (Vantage Live = GMT+3),
            # NOT UTC. Interpreting it as UTC put last_entry_time hours in the
            # FUTURE, so cooldown_ok() was always False and recovery NEVER fired.
            # Derive the position's true age from the broker clock (tick.time is
            # the SAME server frame, so the offset cancels) and store
            # last_entry_time in real UTC so cooldown_ok() is correct.
            last_open_srv  = datetime.fromtimestamp(ordered[-1].time, tz=timezone.utc)
            tk             = get_tick(symbol)
            broker_now_srv = tk['time'] if tk and tk.get('time') else last_open_srv
            age            = (broker_now_srv - last_open_srv).total_seconds()
            self.last_entry_time = datetime.now(tz=timezone.utc) - timedelta(seconds=max(0.0, age))

        # Rebuild this side's live hedges from MT5 (closed ones simply vanish).
        # A hedge with a stop set = already retired (break-even); no stop = the
        # latest hedge holding the freeze.
        self.hedges = []
        if self.hedge_magic is not None:
            for hp in sorted(get_open_positions(symbol, self.hedge_magic), key=lambda x: x.time):
                self.hedges.append({
                    "ticket":  hp.ticket,
                    "lot":     hp.volume,
                    "price":   hp.price_open,
                    "level":   None,
                    "sl":      hp.sl,      # non-zero = break-even stop already armed
                    "time":    datetime.fromtimestamp(hp.time, tz=timezone.utc).isoformat(),
                })

        if self.active:
            self.update_all_tps(symbol)

    def reset(self):
        self.active     = False
        self.levels     = []
        self.basket_tp  = 0.0
        self.last_entry_time = None
        self.hedges     = []
        self.refreeze_ref = None


class DualGrid:
    """Manages BUY and SELL baskets for a single symbol, fully independent."""

    def __init__(self, sym_cfg: dict):
        self.buy  = GridSide("BUY",  sym_cfg['magic_buy'],  sym_cfg)
        self.sell = GridSide("SELL", sym_cfg['magic_sell'], sym_cfg)

    def sync(self, symbol: str):
        self.buy.sync_with_mt5(symbol)
        self.sell.sync_with_mt5(symbol)
        logger.info(
            f"Grid synced [{symbol}] | BUY L{self.buy.level_count} "
            f"| SELL L{self.sell.level_count} "
            f"| Hedges SELL-side:{self.sell.hedge_count} BUY-side:{self.buy.hedge_count}"
        )

    def sweep_orphan_hedges(self, symbol: str) -> int:
        """Master exit: if a side's basket has fully closed (hit its shared tp)
        but hedges are still open, close them. Returns the number swept."""
        swept = 0
        for side in (self.buy, self.sell):
            if side.level_count == 0 and side.hedge_count > 0:
                logger.info(
                    f"[{side.side}] basket flat with {side.hedge_count} hedge(s) still open "
                    f"— sweeping (master exit)"
                )
                swept += side.sweep_hedges(symbol)
        return swept

    def check_take_profit(self, symbol: str):
        """Close any basket that has reached its money-target at MARKET, rather
        than relying only on the broker TP (tight targets can land behind a fast
        market). Closing a basket also sweeps that side's hedges (master exit)."""
        tick = get_tick(symbol)
        if not tick:
            return
        bid, ask = tick['bid'], tick['ask']
        for side in (self.buy, self.sell):
            if not side.active or not side.levels or side.basket_tp <= 0:
                continue
            # Idea 1: a frozen (hedged) basket does NOT take its small TP here —
            # that would realize the frozen loss. check_hedged_net_exit() closes the
            # whole structure only when it reaches NET profit.
            if side.hold_tp:
                continue
            hit = (bid >= side.basket_tp) if side.side == "BUY" else (ask <= side.basket_tp)
            if hit:
                _tp = side.basket_tp
                closed, swept = side.close_basket_at_market(symbol)
                logger.info(
                    f"[{side.side}] Target {_tp} reached — closed {closed} "
                    f"level(s) at market" + (f" + swept {swept} hedge(s)" if swept else "")
                )

    def check_hedged_net_exit(self, symbol: str):
        """Idea 1 master exit. A frozen (hedged) basket is NOT closed at its small
        basket TP (that crystallizes the frozen loss — the overnight -$594 case).
        Instead close the WHOLE structure (basket magic + hedge magic) only when
        their NET floating profit reaches the basket's money target — i.e. the
        freeze has unwound (hedges peeled at ~0) into net profit. Target is the same
        money figure the basket TP uses, in account currency."""
        for side in (self.buy, self.sell):
            if not side.hold_tp or side.hedge_magic is None:
                continue
            target = tp_target_usc(side.level_count, side.tp_table) * side.tp_scale
            if target <= 0:
                continue
            net = get_positions_profit(symbol, [side.magic, side.hedge_magic])
            if net >= target:
                closed, swept = side.close_basket_at_market(symbol)
                logger.info(
                    f"[{side.side}] NET EXIT — net {net:.2f} >= target {target:.2f} "
                    f"(closed {closed} level(s) + swept {swept} hedge(s))"
                )

    def check_hedge_breakeven(self, symbol: str):
        """TIGHTEN each hedge's stop to its exact entry (true break-even) once price
        has moved `hedge_be_arm` in the hedge's favour. The hedge opens with a
        PROTECTIVE stop at entry +/- buffer (Idea 2); this upgrades it to entry so it
        peels at ~0 on a pull-back, while the basket runs to its own TP. Works off the
        stop's VALUE (not a flag), so it survives a sync from MT5."""
        tick = get_tick(symbol)
        if not tick:
            return
        bid, ask = tick['bid'], tick['ask']
        digits = get_symbol_digits(symbol)
        for side in (self.buy, self.sell):
            for h in side.hedges:
                be = round(h['price'], digits)
                if side.hedge_side == "SELL":    # SELL hedge: SL ABOVE entry; entry is tightest
                    if h.get('sl') and h['sl'] <= be + 1e-9:
                        continue                 # already at / below break-even
                    moved = h['price'] - ask     # favourable = price falls
                else:                            # BUY hedge: SL BELOW entry; entry is tightest
                    if h.get('sl') and h['sl'] >= be - 1e-9:
                        continue
                    moved = bid - h['price']     # favourable = price rises
                if moved >= side.hedge_be_arm:
                    if modify_position_sl(symbol, h['ticket'], be):
                        h['sl'] = be
                        logger.info(f"[{side.side}] HEDGE #{h['ticket']} armed -> break-even stop {be}")

    def check_refreeze(self, symbol: str):
        """Re-freeze a deep basket that lost its hedge (a hedge peeled off at
        break-even). As soon as price resumes AGAINST the basket by one grid step
        from where it was left unhedged, re-open a full-basket hedge — closing the
        exposure gap without waiting for the next 5-level mark. The grid-step buffer
        + the cooldown keep it from churning on noise."""
        tick = get_tick(symbol)
        if not tick:
            return
        price = (tick['bid'] + tick['ask']) / 2
        for side in (self.buy, self.sell):
            deep_unhedged = (side.hedge_enabled and side.active
                             and side.level_count >= side.hedge_step
                             and side.hedge_count == 0)
            if not deep_unhedged:
                side.refreeze_ref = None
                continue
            if side.refreeze_ref is None:          # first scan unhedged -> arm reference
                side.refreeze_ref = price
                continue
            moved = (price - side.refreeze_ref) if side.side == "SELL" else (side.refreeze_ref - price)
            if moved >= side.spacing and side.cooldown_ok():
                logger.info(
                    f"[{side.side}] RE-FREEZE — deep ({side.level_count}L) & unhedged, price "
                    f"resumed {moved:.2f} against — re-hedging full basket"
                )
                side._place_block_hedge(symbol, side.level_count)
                side.refreeze_ref = None

    def start_side(self, symbol: str, side: str) -> bool:
        grid = self.buy if side == "BUY" else self.sell
        if grid.active:
            return False
        logger.info(f"[{side}] Starting new L1 entry...")
        return grid.open_entry(symbol, level=1)

    def check_recovery(self, symbol: str):
        tick = get_tick(symbol)
        if not tick:
            return
        price = (tick['bid'] + tick['ask']) / 2

        for side in [self.buy, self.sell]:
            if not side.active:
                continue
            if side.at_max():
                continue
            if not side.needs_recovery(price):
                continue
            if not side.cooldown_ok():
                continue
            next_lot = side.lot_for(side.next_level)
            logger.info(
                f"[{side.side}] Recovery at {price} | "
                f"Adding level {side.next_level} (lot {next_lot:.2f})"
            )
            side.open_entry(symbol)

    def status(self) -> dict:
        return {
            "buy_levels":     self.buy.level_count,
            "sell_levels":    self.sell.level_count,
            "buy_active":     self.buy.active,
            "sell_active":    self.sell.active,
            "buy_basket_tp":  self.buy.basket_tp,
            "sell_basket_tp": self.sell.basket_tp,
            "sell_hedges":    self.sell.hedge_count,
            "buy_hedges":     self.buy.hedge_count,
        }


def _log_trade(entry: dict, side: str, action: str):
    os.makedirs(os.path.dirname(TRADE_LOG) if os.path.dirname(TRADE_LOG) else ".", exist_ok=True)
    record = {"action": action, "side": side, "time": datetime.now(tz=timezone.utc).isoformat(), **entry}
    log = []
    if os.path.exists(TRADE_LOG):
        try:
            with open(TRADE_LOG, 'r') as f:
                log = json.load(f)
        except Exception:
            pass
    log.append(record)
    with open(TRADE_LOG, 'w') as f:
        json.dump(log, f, indent=2)
