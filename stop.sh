#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f logs/run.pid ] && kill "$(cat logs/run.pid)" 2>/dev/null
[ -f logs/bot.pid ] && kill "$(cat logs/bot.pid)" 2>/dev/null
echo stopped
