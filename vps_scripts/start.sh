#!/bin/bash
# HedgeBot (demo, ISOLATED) — prefix /root/.wine-hedgedemo, display :20
ROOT=/root/hedgedemo
PFX=/root/.wine-hedgedemo
PY=$PFX/drive_c/Python312/python.exe
mkdir -p $ROOT/logs $ROOT/run
for p in $(pgrep -f 'HedgeBot/main.py'); do
  pf=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -m1 '^WINEPREFIX=' | cut -d= -f2-)
  [ "$pf" = "$PFX" ] && kill "$p" 2>/dev/null
done
sleep 1
DISPLAY=:21 WINEPREFIX=$PFX WINEDEBUG=-all nohup wine "$PY" C:/HedgeBot/main.py >> $ROOT/logs/bot.log 2>&1 &
echo $! > $ROOT/run/bot.pid
echo "HedgeBot started PID=$(cat $ROOT/run/bot.pid)"
