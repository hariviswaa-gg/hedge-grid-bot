# Hedge Recovery Grid Bot — STATUS

**Last updated: 2026-10-29.** This file is the single source of truth for *what this bot
is, what has changed, and what is running right now*. Read it first.

Design/analysis doc (diagrams, overnight results, fix options):
https://claude.ai/artifact/RrS6Lh4hcks6zpz5m7e1nW

---

## 1. What this bot is

A loss-prevention **variant** of the TharEX grid bot. It clones the TharEX EA v1.05
"Normal" dual-grid logic 1:1 and layers a **hedge freeze** on top to stop deep-side
blow-ups (the LIVE1 "SELL-L84 wipe" failure mode).

- **Base grid (unchanged from TharEX):** dual BUY+SELL baskets, $0.75 martingale spacing,
  value-band lots (cap 1.0, max 25 levels), ONE shared money-target TP per side
  (`tp_table`), no per-position SL.
- **Hedge layer (new):** once a basket reaches a trigger depth, open an opposite-side
  "block hedge" sized so cumulative hedge volume == cumulative basket volume → the deep
  loss is **frozen** (delta-neutral). Hedges peel off at break-even on reversals.

7 modules (same shape as the TharEX fleet): `main.py`, `grid_engine.py`,
`mt5_connector.py`, `config.py` (git-ignored — secrets), `news_filter.py`,
`telegram_notifier.py`, `trend.py`. Use `config.example.py` as the template.

---

## 2. Version history (what changed, why)

| Ver | Change | Why |
|-----|--------|-----|
| v1 | Per-level hedge, each with its own TP | First attempt — churned |
| v2 | Hedge every 5 levels; hedge lot = that block's lot-sum (cumulative = basket = frozen); latest hedge no stop, older ones get break-even stop; basket drops its tiny TP while hedged; net-based master exit | Redesign after watching v1 churn |
| v2.1 | **Root-cause TP fix:** `tp_target_scale` 0.01 → **1.0** (use TP table as-is like real TharEX; 0.01 made every TP ~100x too tight → spread churn). Exit model: basket TP always visible, every hedge gets a break-even stop (armed once price moves `hedge_breakeven_arm_dist` in its favour) | TP was too close to entry + bleed |
| v2.2 | **Re-freeze** (`check_refreeze`): if a hedge peels and the still-deep basket goes unhedged, re-open a full-basket hedge once price resumes a grid-step against it. Test-phase: drawdown guard + daily-profit + giveback DISABLED | Close the exposure gap; isolate the hedge for testing |
| **v2.3** | **Exit fixes** (see §4) — the current version | Overnight test proved the freeze works but the EXIT loses money |

---

## 3. Overnight test result (2026-10-28 22:56 → 2026-10-29 09:02, demo)

Net **+$383.88 (+3.84%)**, $10,000 → $10,383.88. 0 crashes. Max depth BUY L17 / SELL L15.
Worst float −$615.78, lowest equity $9,895.77 (only −1% under start — hedge never let it
spiral).

**KEY FINDING — P&L split:**
- Base-grid scalps: **≈ +$643** (did all the earning)
- 7 hedge-cycle master exits: **NET −$361** (the hedge CAPS loss but, as exited in v2.2,
  is net-negative on deep cycles — dominated by ONE bad exit: SELL L15 @ 07:06 = **−$594**)

**Root cause of the bad exits:** a frozen basket taking its small TP realizes the full
frozen loss; and the newest/biggest block hedge is placed at the turning point, so its
break-even never arms before a sharp reversal → it gets swept at a big loss instead of
peeling at ~0. Green cycles = the reversal let the big hedge peel first.

---

## 4. v2.3 — the current exit fixes (config-flagged, base grid untouched)

Approved 2026-10-29. Three changes, all in the hedge open/exit logic only:

1. **Hold the TP while frozen** (`hedge_hold_tp_until_unwound=True`): while ANY hedge is
   open, the basket does NOT take its small TP (`GridSide.hold_tp`; `check_take_profit`
   skips it). The whole structure closes only when basket+hedges reach **net profit ≥ the
   money target** (`DualGrid.check_hedged_net_exit`). Once all hedges peel, normal TP resumes.
2. **Armored hedge** (`hedge_open_buffer=0.40`, `hedge_breakeven_arm_dist` 0.75→**0.35**):
   every hedge gets a PROTECTIVE stop the moment it opens, at entry ± buffer (buffer >
   ~0.29 spread so it can't trigger on open). `check_hedge_breakeven` later TIGHTENS it to
   exact entry. A sharp reversal peels the newest hedge at ~−buffer instead of a big loss.
3. **No late block hedge** (`hedge_skip_block_near_tp=True`): skip opening a new block
   hedge when the basket is within one grid-step of its TP.

Also hardened in `mt5_connector.connect()` (safe for all deployments):
- **Hard account guard** — after connect, if `account_info().login != MT5_LOGIN` → refuse
  to trade + disconnect. Critical on a shared host that also runs live-money bots.
- **`MT5_FORCE_LOGIN`** (config) — connect ONLY launches MT5_PATH + logs into the
  configured account (no attach to another terminal). Used on the VPS.
- **`MT5_PORTABLE`** (config) — launch the terminal `/portable` (REQUIRED under wine on
  the VPS; without it the MT5 Python IPC times out, −10005). False on local desktop.

---

## 5. Accounts & isolation (NEVER touches the live fleet)

- **Demo account:** 26349388 @ VantageMarkets-Demo (standard USD). Password lives ONLY in
  `config.py` (git-ignored). `tp_target_scale=1.0`.
- **Magic numbers:** 15000000 01/02 (real BUY/SELL), 03 (BUY hedge), 04 (SELL hedge) —
  distinct from the live fleet's 14257467xx.
- **Symbol:** XAUUSD.

---

## 6. Where it runs RIGHT NOW (2026-10-29)

- **Local (Windows desktop):** `d:\Trading BOT\Trading Signals\TharEX_Bot\hedge-grid-bot`.
  Ran the overnight v2.3 test; **STOPPED** on 2026-10-29 to hand over to the VPS (avoid
  double-trading the one demo account). Local run: open MT5 (non-portable) with Algo
  Trading ON, then `python main.py` (logs → `logs/run.out`, pid → `logs/bot.pid`).
- **VPS (`root@187.127.133.38`, Linux/wine):** NEW isolated prefix
  **`/root/.wine-hedgedemo`** (cloned from the idle `bot5` scaffold — full Python312 +
  MetaTrader5 5.0.47 + its own terminal `MetaTrader5-hedge`). Code at
  `/root/.wine-hedgedemo/drive_c/HedgeBot`. Terminal runs `/portable` on display `:21`
  (isolated from the fleet's `:20`). Control scripts in `/root/hedgedemo/`:
  `start.sh` (launches bot on :21), `stop.sh` (touches `run/stopped`, kills bot),
  `watchdog.sh` (restarts if the HedgeBot python for this prefix dies).
- **DEPLOY STATE:** code + config + scripts all uploaded and compile-verified under the
  VPS wine python. **Blocker being resolved:** the fresh terminal must be LOGGED INTO the
  demo account once (headless login hit the −10005 IPC timeout). A temporary **VNC**
  (x11vnc on display :21, port 5921) was stood up so the user can log the terminal in via
  GUI + enable Algo Trading. Once logged in, start the bot: `bash /root/hedgedemo/start.sh`
  then launch the watchdog detached (`setsid bash /root/hedgedemo/watchdog.sh … &`).

---

## 7. Operating cheatsheet

**VPS:**
- Start bot: `bash /root/hedgedemo/start.sh`
- Stop bot: `bash /root/hedgedemo/stop.sh`
- Logs: `tail -f /root/hedgedemo/logs/bot.log`
- Watchdog: `setsid bash /root/hedgedemo/watchdog.sh >/root/hedgedemo/logs/watchdog.out 2>&1 </dev/null &`
- VNC (temporary, tear down after setup): `187.127.133.38:5921`. Stop it:
  `pkill -f 'x11vnc.*5921'`.
- SSH is via local helper `vpsx.py` (creds parsed from `SeqXE_Bot/setup_vps.py`, never on
  the command line). `vpspush.py` uploads the 7 modules; `vps_deploy_scripts.py` uploads
  the control scripts. (These local-only helpers are git-ignored.)

**IMPORTANT — one terminal per account:** the demo account can only be traded from ONE MT5
terminal. When the VPS runs it, the local bot MUST be stopped and the local MT5 closed.

---

## 8. Open items / next steps

- [ ] Finish VPS cutover: user logs terminal into demo via VNC + Algo Trading ON → start
      bot + watchdog → verify "Connected: 26349388" + account guard passes + adopts positions.
- [ ] Tear down the temporary VNC after setup.
- [ ] Validate v2.3 live: the −$594-type sweeps should become small/green; compare a full
      session vs the +$384 baseline.
- [ ] Re-enable a drawdown backstop (e.g. 30%) before any real-money consideration.
- [ ] (Optional) VPS↔GitHub auto-sync.
