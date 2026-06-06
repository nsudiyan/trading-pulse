#!/bin/bash
# P1-8b: ранний cd — фикс "shell-init: getcwd" (битая cwd при рестарте)
cd "$(dirname "$0")" || exit 1
set -u
# Wrapper для LaunchAgent: загружает .env и запускает screener.py

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /opt/homebrew/bin/python3 "$DIR/screener.py" --top-n 50 --min-score 35 --obsidian "$@"
