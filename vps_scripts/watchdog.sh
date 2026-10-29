#!/bin/bash
ROOT=/root/hedgedemo
PFX=/root/.wine-hedgedemo
LOG=$ROOT/logs/watchdog.log
rm -f $ROOT/run/stopped
while true; do
  if [ -f "$ROOT/run/stopped" ]; then sleep 15; continue; fi
  alive=0
  for p in $(pgrep -f 'HedgeBot/main.py'); do
    pf=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -m1 '^WINEPREFIX=' | cut -d= -f2-)
    [ "$pf" = "$PFX" ] && alive=1
  done
  if [ "$alive" = "0" ]; then
    echo "$(date) HedgeBot dead - restarting" >> $LOG
    bash $ROOT/start.sh >> $LOG 2>&1
    sleep 20
  fi
  sleep 30
done
