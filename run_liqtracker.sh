#!/bin/bash
# Wrapper для LaunchAgent: загружает .env и запускает liquidation_tracker.py

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /opt/homebrew/bin/python3 "$DIR/liquidation_tracker.py" \
    --top 80 --alert 100000 --report 60 --no-hl "$@"
