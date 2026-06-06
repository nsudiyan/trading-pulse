#!/bin/bash
# Wrapper для LaunchAgent: загружает .env и запускает channel_reader.py scan

DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$DIR/.env" ]; then
    set -a
    source "$DIR/.env"
    set +a
fi

exec /opt/homebrew/bin/python3 "$DIR/channel_reader.py" scan
