# =============================================================
#  TharEX Bot — Configuration TEMPLATE
#  Copy this file to `config.py` and fill in the per-bot identity below.
#  config.py is git-ignored and must NEVER be committed (it holds live creds).
#
#  Per-bot identity = the ONLY values that differ between the 10 fleet bots:
#    MT5_LOGIN / MT5_PASSWORD / MT5_PATH   (this file)
#    _TAG = "[LIVEn] "                     (telegram_notifier.py)
#  Everything else (strategy, risk, sessions) is identical across the fleet.
# =============================================================

# MT5 Credentials — FILL THESE IN per bot (do not commit real values)
MT5_LOGIN    = 0                 # account number, e.g. 12345678
MT5_PASSWORD = "FILL_ME"         # account password
MT5_SERVER   = "VantageMarkets-Live 21"
MT5_PATH     = "C:/MetaTrader5-botN/terminal64.exe"   # per-bot terminal dir
MT5_TIMEOUT  = 30000

# Shared-host / VPS safety (optional; default False = local desktop behaviour).
# MT5_FORCE_LOGIN=True : connect() ONLY launches MT5_PATH + logs into this account
#   (never attaches to another terminal — vital on a host that runs other accounts).
# MT5_PORTABLE=True    : launch the terminal /portable (REQUIRED under wine on the VPS;
#   without it the MT5 Python IPC times out, -10005). Leave False on a local desktop.
MT5_FORCE_LOGIN = False
MT5_PORTABLE    = False

# =============================================================
#  Trading Config — EXACT match to TharEX EA v1.05 "Normal" (Nrm) mode.
#  Values taken 1:1 from the official .set file (InpNrm* / InpLot* inputs):
#     InpLotSize=0.01   InpNrmGridUSD=0.75   InpMaxLotCap=1.0   InpMaxTrades=100
#     InpLotBands=0.01-0.07=0.01, 0.08-0.90=0.02, 0.91-1.5=0.03
#     InpNrmTPTable=1=4,2=11,3=20,6=45,8=65,12=100,15=135,18=200,20=350,
#                   25=550,30=800,35=1200,40=2000   (target basket profit in USC)
#     InpSingleTPMode=true   InpCooldownMinutes=1   InpMaxSpreadPips=50
#  grid_spacing is in price units: 0.75 USD on XAUUSD = 75 pts (1 pt = 0.01 USD).
#  VantageMarkets-Live21 cent account uses .sc suffix for symbols.
SYMBOLS_CONFIG = [
    {
        "label":            "XAUUSD",
        "symbol":           "XAUUSD.sc",
        "magic_buy":        1425746731,
        "magic_sell":       1425746732,
        # Hedge magics — MUST be distinct from any other bot on the account/host.
        "magic_hedge_buy":  1425746733,   # BUY hedges (protect the SELL basket)
        "magic_hedge_sell": 1425746734,   # SELL hedges (protect the BUY basket)
        # TP table calibration: 1.0 = use the table as-is (cent acct, or a standard
        # acct where you want the real TharEX high TP). 0.01 = divide by 100.
        "tp_target_scale":  1.0,
        "base_lot":         0.01,       # InpLotSize
        "grid_spacing":     0.75,       # InpNrmGridUSD ($0.75 = 75 pts)
        "max_trades":       25,         # RISK CAP (was 100): normal days reach <=21
                                        # levels; 25 kills the 54-84 runaway baskets
                                        # that blew the account. Basket stops ADDING
                                        # at 25; the equity stop closes it if needed.
        "max_lot_cap":      1.0,        # InpMaxLotCap
        "cooldown_minutes": 1,          # InpCooldownMinutes (between level adds)
        # InpLotBands — step applied while the CURRENT lot value is inside [lo, hi]
        "lot_bands":        [(0.01, 0.07, 0.01),
                             (0.08, 0.90, 0.02),
                             (0.91, 1.50, 0.03)],
        # InpNrmTPTable — (open-level count, target basket profit in USC).
        # Levels between anchors use the nearest LOWER anchor (step/floor —
        # verified against 794 live TharEX baskets), converted to ONE shared
        # price TP via order_calc_profit (InpSingleTPMode=true).
        "tp_table":         [(1, 4), (2, 11), (3, 20), (6, 45), (8, 65),
                             (12, 100), (15, 135), (18, 200), (20, 350),
                             (25, 550), (30, 800), (35, 1200), (40, 2000)],
        "tp_trigger_buffer_pts": 25,    # InpTPTriggerBufferPts (documented; not applied to price)
        "dynamic_tp":            True,  # InpDynamicTP
        "dyn_tp_growth_pct":     8.0,   # InpDynTPGrowthPct
        "dyn_tp_max_x":          4.0,   # InpDynTPMaxX

        # ── HEDGE LAYER (loss-prevention; the NEW logic over base TharEX) ────────
        "hedge_enabled":       True,    # master switch (False = exact TharEX base)
        "hedge_trigger_step":  5,       # hedge every N levels (L5, L10, L15 ...). Each
                                        # hedge lot = that block's lot-sum so cumulative
                                        # hedge == cumulative basket = fully frozen.
        # ── v2.3 exit fixes (from the overnight test; see STATUS.md §3-4) ───────
        "hedge_hold_tp_until_unwound": True,  # Idea 1: while ANY hedge is open, do NOT
                                        # take the small basket TP (it crystallizes the
                                        # frozen loss). Close the whole structure only when
                                        # basket+hedges reach NET profit >= the money target
                                        # (DualGrid.check_hedged_net_exit). Normal TP resumes
                                        # once all hedges peel.
        "hedge_open_buffer":   0.40,    # Idea 2: protective stop placed on each hedge the
                                        # moment it opens, at entry +/- buffer (must exceed
                                        # the spread). A sharp reversal peels it at ~-buffer
                                        # instead of a big loss.
        "hedge_breakeven_arm_dist": 0.35,  # Idea 2: favourable move before the stop tightens
                                        # to exact entry (true break-even).
        "hedge_skip_block_near_tp": True,  # Idea 3: skip a new block hedge when the basket is
                                        # within one grid-step of its TP (about to resolve).
    },
]

# Session Filter — ICT kill-zone operating window (UTC)
# When SESSION_FILTER_ENABLED, new entries open only within this window AND only
# on weekdays (Mon-Fri); recovery of open baskets runs 24/7. Winddown = last 60
# min of the window (recovery only). Set False for pure EA behaviour (24/7 entries).
SESSION_FILTER_ENABLED = True

# Operating window: 11:00 AM IST -> 08:30 PM IST = 05:30 -> 15:00 UTC.
# Winddown 14:00-15:00 UTC (07:30-08:30 PM IST): recovery only, no new L1s.
SESSIONS_UTC = [
    {"name": "TharEX Window", "start": 5.5, "winddown": 14.0, "end": 15.0},
]

# Drawdown Guard — force-close-all if EITHER the absolute floating loss hits
# DRAWDOWN_LIMIT_USC OR the percentage loss hits DRAWDOWN_LIMIT_PCT (first wins).
# Fleet default: 30%-only (absolute cap disabled via the 10_000_000 sentinel).
DRAWDOWN_LIMIT_USC = 10_000_000.0  # DISABLED sentinel — 30%-only via DRAWDOWN_LIMIT_PCT
DRAWDOWN_LIMIT_PCT = 30            # % safety backstop
# While balance is BELOW this, the drawdown guard is SKIPPED (let the grid rebuild
# instead of realizing the loss); re-enables once balance climbs back. 0 = always on.
DRAWDOWN_ENABLE_ABOVE_USC = 8000.0

# ── Trend Filter (H1) — only open new baskets with the H1 trend. False = off.
TREND_FILTER_ENABLED        = False
TREND_TIMEFRAME             = "H1"   # M15/M30/H1/H4/D1
TREND_EMA_PERIOD            = 50

# ── Profit Protection (high-water-mark giveback) — pause new baskets if balance
# gives back this many USC from its peak. 0 = off. Peak persisted in data/hwm.json.
PROFIT_GIVEBACK_LIMIT_USC   = 2000.0

# Weekly withdrawal reminder — each Friday close, if balance is above this base,
# Telegram suggests withdrawing the profit above it.
WEEKLY_WITHDRAW_BASE_USC    = 10000.0

# News Filter — disable NEW L1 entries N min before/after a high-impact
# USD/XAU/EUR event. Recovery levels are NOT blocked. 0 = disable.
NEWS_BLACKOUT_MINUTES = 30

# Spread Filter — skip any order if current spread exceeds this (points).
MAX_SPREAD_POINTS = 50

# Daily Profit Target (USC) — stop new L1 entries for the day when day P&L hits this.
DAILY_PROFIT_TARGET_USC = 1000

# Opposite Side Block — don't open a new L1 on one side if the other side has
# this many levels open.
OPPOSITE_SIDE_BLOCK_LEVEL = 5

# Grid Depth Alerts — Telegram notification thresholds (level counts per side)
GRID_DEPTH_ALERT_LEVELS = [5, 8, 10]

# Soft Float Loss Alert — Telegram warning (no close) when floating loss >= this %.
FLOAT_LOSS_ALERT_PCT = 20

# Weekend Position Handling — Friday at this UTC hour: close all if in profit,
# alert and carry over if in loss. 18:00 UTC = 23:30 IST Friday.
WEEKEND_CLOSE_UTC_HOUR = 18.0

# Scan Interval — 10s (proven, responsive for a 30pt grid). Do not lower below ~10s.
SCAN_INTERVAL_SECONDS = 10

# Logging
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/tharex_bot.log"
TRADE_LOG = "data/trade_log.json"

# Telegram — FILL THESE IN (do not commit real values)
TELEGRAM_BOT_TOKEN = "<YOUR_TELEGRAM_BOT_TOKEN>"
TELEGRAM_CHAT_ID   = "<YOUR_TELEGRAM_CHAT_ID>"
TELEGRAM_QUEUE     = "data/tg_queue.txt"
