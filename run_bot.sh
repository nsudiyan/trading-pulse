#!/bin/bash
# Wrapper для LaunchAgent: загружает .env и запускает telegram_bot.py daemon
# LaunchAgent не наследует shell-окружение → secrets грузим явно из .env

DIR="$(cd "$(dirname "$0")" && pwd)"

# Загружаем .env если он есть
if [ -f "$DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$DIR/.env"
    set +a
fi

# boost_watcher в фоне с авто-перезапуском
(
    while true; do
        /opt/homebrew/bin/python3 "$DIR/boost_watcher.py" >> "$DIR/boost_watcher.log" 2>&1
        echo "[run_bot] boost_watcher exited — restart in 10s" >> "$DIR/boost_watcher.log"
        sleep 10
    done
) &

exec /opt/homebrew/bin/python3 "$DIR/telegram_bot.py" daemon
