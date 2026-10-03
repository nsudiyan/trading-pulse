#!/bin/sh
set -eu
stage=/tmp/pulse-direction-20261003
backup=/opt/pulse-direction-backup-20261003
test ! -e "$backup"
check() { actual=$(sha256sum "$1" | cut -d ' ' -f 1); test "$actual" = "$2"; }
check /opt/news-chart-bot/market.py 339eea9f1bbb4dca691b2803ab1beff696935bd8acb89edafe3f5c04790ac58b
check /opt/news-chart-bot/structure.py 7592c2fe7c5b100dd8c41e43c56a07c0f24d1f339f6a0f7b7a8c7fdf2ee3f521
check /opt/apex-dashboard/server.py d737ff18b2af69ed8805e9e8dce26a745cd8afd2d617b2c4c301f4eb1a39052d
check /opt/apex-dashboard/app.js 1dc09205055580ea745bf172ba89d6389038e4e2ee099cc63e91352dcf63dee5
mkdir -p "$backup/bot" "$backup/dashboard"
cp -p /opt/news-chart-bot/market.py /opt/news-chart-bot/structure.py "$backup/bot/"
cp -p /opt/apex-dashboard/server.py /opt/apex-dashboard/app.js "$backup/dashboard/"
rollback() {
  cp -p "$backup/bot/market.py" "$backup/bot/structure.py" /opt/news-chart-bot/
  cp -p "$backup/dashboard/server.py" "$backup/dashboard/app.js" /opt/apex-dashboard/
  systemctl restart news-chart-bot.service apex-dashboard.service
  echo RELEASE_ROLLED_BACK
}
trap 'rollback' HUP INT TERM
for f in market.py structure.py scenario_contract.py; do
  install -m 644 "$stage/bot/$f" "/opt/news-chart-bot/$f.new"
  mv "/opt/news-chart-bot/$f.new" "/opt/news-chart-bot/$f"
done
for f in server.py app.js movement.py; do
  install -m 644 "$stage/dashboard/$f" "/opt/apex-dashboard/$f.new"
  mv "/opt/apex-dashboard/$f.new" "/opt/apex-dashboard/$f"
done
if ! systemctl restart news-chart-bot.service apex-dashboard.service; then rollback; exit 1; fi
sleep 3
if ! systemctl is-active --quiet news-chart-bot.service || ! systemctl is-active --quiet apex-dashboard.service; then rollback; exit 1; fi
if ! curl --fail --silent --max-time 10 'http://127.0.0.1:5083/api/signals?limit=1' | /opt/news-chart-bot/.venv/bin/python -c 'import json,sys; r=json.load(sys.stdin); assert "movement" in r["signals"][0]; print("RELEASE_API_OK")'; then rollback; exit 1; fi
echo RELEASE_OK
