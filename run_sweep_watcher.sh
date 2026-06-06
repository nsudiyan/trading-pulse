#!/bin/bash
# P1-8b: ранний cd — фикс "shell-init: getcwd" (битая cwd при рестарте)
cd "$(dirname "$0")" || exit 1
set -u
# Wrapper for LaunchAgent: loads .env and starts sweep_watcher.py

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /opt/homebrew/bin/python3 "$DIR/sweep_watcher.py" "$@"
