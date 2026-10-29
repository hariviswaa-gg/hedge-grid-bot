#!/bin/bash
ROOT=/root/hedgedemo
PFX=/root/.wine-hedgedemo
touch $ROOT/run/stopped
P=$(cat $ROOT/run/bot.pid 2>/dev/null); [ -n "$P" ] && kill "$P" 2>/dev/null
for p in $(pgrep -f 'HedgeBot/main.py'); do
  pf=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -m1 '^WINEPREFIX=' | cut -d= -f2-)
  [ "$pf" = "$PFX" ] && kill "$p" 2>/dev/null
done
echo "HedgeBot stopped"
