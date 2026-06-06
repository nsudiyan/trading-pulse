#!/bin/bash
# Wrapper для LaunchAgent: загружает .env и запускает screener.py

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /opt/homebrew/bin/python3 "$DIR/screener.py" --top-n 50 --min-score 35 --obsidian "$@"
