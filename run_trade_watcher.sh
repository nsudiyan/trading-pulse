#!/bin/bash
# P1-8b: ранний cd — фикс "shell-init: getcwd" (битая cwd при рестарте)
cd "$(dirname "$0")" || exit 1
set -u
# Wrapper для LaunchAgent: загружает .env и запускает trade_watcher.py
# (вотчер открытых РЕАЛЬНЫХ сделок из outcomes/trades.json — пинг при касании SL/TP1)

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /opt/homebrew/bin/python3 "$DIR/trade_watcher.py" "$@"
