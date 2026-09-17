#!/bin/bash
# ДЕПЛОЙ БОТА «одной фразой» (мак = редактор, сервер = завод).
# Переносит ТОЛЬКО код (*.py/*.sh), данные/стейт сервера НЕ трогает, ничего не удаляет.
# Разделение (правило брата 11.07):
#   деплой БОТА     = этот скрипт (код → сервер + рестарт резидентов trading-*)
#   деплой ДАШБОРДА = как раньше: cd ~/trading/dashboard/site && vercel deploy --prod --yes
#   деплой РУБКОФФ  = его собственный ~/rubkoff_deploy.sh (/opt/rubkoff, не пересекается)
KEY=~/.ssh/rubkoff_server; SRV=root@147.45.175.24
rsync -a --prune-empty-dirs \
  --exclude='flow_collector/' --exclude='venv/' --exclude='__pycache__/' \
  --include='*/' --include='*.py' --include='*.sh' --exclude='*' \
  -e "ssh -i $KEY" ~/trading/ "$SRV:/root/trading/"
ssh -i "$KEY" "$SRV" 'systemctl restart trading-bot trading-rosewatch trading-stormignite trading-tradewatcher trading-liqtracker && echo "✓ код задеплоен, резиденты перезапущены (периодика подхватит код на следующем тике сама)"'
