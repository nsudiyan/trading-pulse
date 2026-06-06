#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/.env" ]; then
    set -a; source "$DIR/.env"; set +a
fi
exec /opt/homebrew/bin/python3 "$DIR/web_dashboard.py"
