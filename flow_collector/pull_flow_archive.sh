#!/bin/bash
# Ежедневный пулл gz-архивов обоих сборщиков (data=H-FLOW, data_x=lead-lag) с сервера
# на мак + sha256-сверка. Маркер .pulled_ok ставится НА СЕРВЕРЕ только после совпадения
# сумм — лишь такие дни имеет право прунить дисковый предохранитель (Codex 11.07).
KEY=~/.ssh/rubkoff_server; SRV=root@147.45.175.24
LOCAL=~/trading/flow_collector/server_archive
TODAY=$(date -u +%Y-%m-%d)
for SUB in data data_x; do
  for DAY in $(ssh -i $KEY -o BatchMode=yes $SRV "ls /root/trading/flow_collector/$SUB 2>/dev/null" | grep -v "$TODAY"); do
    [ -f "$LOCAL/$SUB/$DAY/.pulled_ok" ] && continue
    mkdir -p "$LOCAL/$SUB/$DAY"
    rsync -a -e "ssh -i $KEY" "$SRV:/root/trading/flow_collector/$SUB/$DAY/" "$LOCAL/$SUB/$DAY/" || { echo "$(date -u +%FT%T) RSYNC_FAIL $SUB/$DAY"; continue; }
    R=$(ssh -i $KEY $SRV "cd /root/trading/flow_collector/$SUB/$DAY && sha256sum *.gz 2>/dev/null | sort")
    L=$(cd "$LOCAL/$SUB/$DAY" && shasum -a 256 *.gz 2>/dev/null | sort)
    if [ -n "$R" ] && [ "$R" = "$L" ]; then
      ssh -i $KEY $SRV "touch /root/trading/flow_collector/$SUB/$DAY/.pulled_ok"
      touch "$LOCAL/$SUB/$DAY/.pulled_ok"
      echo "$(date -u +%FT%T) OK $SUB/$DAY ($(echo "$R" | wc -l | tr -d ' ') файлов)"
    else
      echo "$(date -u +%FT%T) CHECKSUM_MISMATCH $SUB/$DAY — маркер НЕ ставлю"
    fi
  done
done

# ── недельные бэкапы outcomes с сервера (отдельная копия вне сервера) ──
rsync -a -e "ssh -i $KEY" "$SRV:/root/trading/archive/outcomes_backups/" "$LOCAL/outcomes_backups/" 2>/dev/null && echo "$(date -u +%FT%T) OK outcomes_backups"
