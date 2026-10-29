"""
Read-only probe for the hedge-grid-bot demo account.
Connects via config creds, prints account + XAUUSD symbol specs and the
order_calc_profit magnitude (to decide tp_target_scale), then disconnects.
NO orders are placed.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5
from mt5_connector import connect, disconnect, resolve_symbol, profit_per_dollar_usc
from config import SYMBOLS_CONFIG


def main():
    if not connect():
        print("CONNECT FAILED:", mt5.last_error())
        return

    info = mt5.account_info()
    print("=== ACCOUNT ===")
    print(f"login={info.login} server={info.server}")
    print(f"currency={info.currency} balance={info.balance} equity={info.equity} leverage={info.leverage}")
    print(f"margin_mode={info.margin_mode} trade_allowed={info.trade_allowed}")

    sc = SYMBOLS_CONFIG[0]
    try:
        sym = resolve_symbol(sc['symbol'], sc['label'])
    except Exception as e:
        print("SYMBOL RESOLVE FAILED:", e)
        disconnect()
        return

    si = mt5.symbol_info(sym)
    tick = mt5.symbol_info_tick(sym)
    print("=== SYMBOL ===")
    print(f"resolved={sym}")
    print(f"digits={si.digits} point={si.point} contract_size={si.trade_contract_size}")
    print(f"volume_min={si.volume_min} volume_step={si.volume_step} volume_max={si.volume_max}")
    print(f"trade_mode={si.trade_mode} (0=disabled 4=full)")
    if tick:
        print(f"bid={tick.bid} ask={tick.ask} spread_pts={round((tick.ask - tick.bid) / si.point)}")

    print("=== PROFIT CALC (per +$1.00 move) ===")
    print(f"0.01 lot -> {profit_per_dollar_usc(sym, 0.01)}")
    print(f"0.21 lot -> {profit_per_dollar_usc(sym, 0.21)}")
    print(f"1.00 lot -> {profit_per_dollar_usc(sym, 1.0)}")
    print("Interpretation: standard USD ~100 per 1.0 lot -> set tp_target_scale=0.01;")
    print("                cent (USC)   ~10000 per 1.0 lot -> set tp_target_scale=1.0")

    disconnect()
    print("DONE (read-only, no orders placed)")


if __name__ == "__main__":
    main()
