# Hedge Grid Bot (demo test)

An **isolated clone of the TharEX grid bot** plus a new **hedge layer** that aims to
stop the deep-side martingale blowups. The base behaves exactly like the live TharEX
bot; the hedge logic is extra and sits behind a config switch (`hedge_enabled`).

Design + pros/cons + worked example: the understanding doc
<https://claude.ai/artifact/RrS6Lh4hcks6zpz5m7e1nW>.

## The new logic (hedge layer)

The base runs two independent baskets per symbol (BUY + SELL), a `$0.75` martingale grid
with one shared money-target TP per side (no per-position SL). On top of that:

1. **Trigger.** Once a side (the one the market is running away from) reaches
   `hedge_trigger_level` (default **L5**), hedging turns on for that basket.
2. **Pairing.** Every *new* level on that side also opens an **equal-lot opposite order**
   (a hedge) carrying its **own** take-profit, `hedge_tp_distance` price units on the
   favourable side (default `0.75`). Each new pair is ~delta-neutral, so the deep-side
   floating loss stops growing.
3. **Peel off.** A hedge that runs into profit hits its own TP and closes on its own.
4. **Master sweep.** When the basket's shared TP closes the side, any hedges still open are
   swept at market (`DualGrid.sweep_orphan_hedges`, run each scan right after sync).

Hedges use **separate magic numbers** (`magic_hedge_buy` / `magic_hedge_sell`) so they are
never confused with the real BUY/SELL baskets. Symmetric for both trend directions.

> ⚠️ Known trade-off (see the doc): because each hedge matches its level's price *and* lot,
> a hedged pair nets ~0 at any close. So a deep trend-then-reversal can still realise a
> **small, bounded** loss on the un-peeled remainder — far smaller than an unhedged blowup,
> but not "pure profit". `hedge_tp_distance` tunes how much peels off vs stays frozen.

## Code layout (changes vs base in **bold**)

| File | Responsibility |
|------|----------------|
| `main.py` | Orchestrator + **hedge sweep step, hedge banner, hedge status/notify** |
| `grid_engine.py` | Grid logic + **hedge open/sync/sweep, tp_target_scale** |
| `mt5_connector.py` | MT5 I/O + **`close_ticket`, hedge magics in close-all, desktop-friendly `connect()`** |
| `news_filter.py` / `trend.py` | News blackout / optional H1 trend filter (unchanged) |
| `telegram_notifier.py` | Alert queue, `_TAG="[HEDGE]"` + **`notify_hedge_sweep`** |
| `config.py` | **This bot's config (git-ignored): creds, hedge params, magics, tp_target_scale** |
| `mt5_probe.py` | Read-only probe: account type, symbol specs, profit calc |

## Key config (`config.py`)

| Setting | Value (demo) | Notes |
|---------|--------------|-------|
| `hedge_enabled` | `True` | master switch; `False` = exact TharEX base |
| `hedge_trigger_level` | `5` | start hedging at L5 (the "0.05" level) |
| `hedge_tp_distance` | `0.75` | each hedge's own TP distance (price units) — **tune this** |
| `tp_target_scale` | `0.01` | demo is **standard USD** (probe: 100/lot/$1); cent acct would be `1.0` |
| `SESSION_FILTER_ENABLED` | `False` | 24/7 for the demo test; `True` mirrors the live window |
| magics | `15000000 01/02/03/04` | isolated from the live fleet (`14257467xx`) |

## Running the demo test

Prereqs (one-time, in the MT5 terminal): logged into the demo account and **Algo Trading
enabled** (Ctrl+E / green toolbar button). The bot launches the terminal non-portable and
logs in itself.

```powershell
cd "hedge-grid-bot"
python mt5_probe.py     # read-only: confirm account/symbol/profit scale
python main.py          # run the bot (Ctrl+C to stop)
```

The startup banner prints the hedge settings; the scan log shows `Hedges SELL-side:n
BUY-side:n`, hedge opens, peel-offs (via sync), and `HEDGE SWEEP` on master exits.

> This is a **demo test bot**, intentionally separate from the live VPS fleet. Nothing here
> touches the live accounts.
