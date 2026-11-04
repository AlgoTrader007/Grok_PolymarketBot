#!/usr/bin/env bash
# Restart-on-crash wrapper (use systemd on a VPS; this is for nohup use).
cd "$(dirname "$0")"
PY=${PY:-.venv/bin/python}
echo $$ > logs/run.pid
trap 'kill $CHILD 2>/dev/null; rm -f logs/run.pid; exit 0' TERM INT
while true; do
  $PY bot.py >> logs/bot.out 2>&1 &
  CHILD=$!; echo $CHILD > logs/bot.pid
  wait $CHILD; rc=$?
  echo "{\"ts_la\":\"$(TZ=America/Los_Angeles date -Iseconds)\",\"type\":\"anomaly\",\"kind\":\"restart\",\"rc\":$rc}" >> logs/events.jsonl
  sleep 3
done
