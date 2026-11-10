#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/news-chart-bot
STAGE=/tmp/pulse-market-filters-20261004
BACKUP=/opt/pulse-market-filters-backup-20261004
BOT="$STAGE/bot"
PYTHON="$APP/.venv/bin/python"

OLD_MARKET=4c59e541fadd261e6818b5caa173a7daa534e3e0af475e6fd1b2d55f94fc92a8
OLD_POLICY=a130d6367939084bca9ada88bf651a1860fa3ac17f4154f6cb6a3c48a8ebce04
NEW_MARKET=6fd006759295963f876fd22a5d73d3d2857499c48f3a7cf2637993a15a1ac2c2
NEW_POLICY=e0db07d886e72023e0b5a7375bdd09ba47b602770498601ceabccd0b608f43f5
NEW_BTC=ec4ffc863b2c9ca0d9ea3dd92d36d9f035af237dd8927ed6dc8d4b485b0be4df
NEW_SWEEP=e0fa93a05e7366205385184a045e52a8dca71fe7ef47e71a0669ef1089f2c541

sha() { sha256sum "$1" | awk '{print $1}'; }
fail() { echo "RELEASE_ABORTED: $*" >&2; exit 1; }

test -d "$APP" || fail "application directory missing: $APP"
test -f "$BOT/market.py" -a -f "$BOT/alert_policy.py" \
     -a -f "$BOT/btc_gate.py" -a -f "$BOT/sweep_gate.py" \
  || fail "staged source bundle incomplete"
test "$(sha "$APP/market.py")" = "$OLD_MARKET" || fail "market.py drifted from reviewed baseline"
test "$(sha "$APP/alert_policy.py")" = "$OLD_POLICY" || fail "alert_policy.py drifted from reviewed baseline"
test "$(sha "$BOT/market.py")" = "$NEW_MARKET" || fail "staged market.py hash mismatch"
test "$(sha "$BOT/alert_policy.py")" = "$NEW_POLICY" || fail "staged alert_policy.py hash mismatch"
test "$(sha "$BOT/btc_gate.py")" = "$NEW_BTC" || fail "staged btc_gate.py hash mismatch"
test "$(sha "$BOT/sweep_gate.py")" = "$NEW_SWEEP" || fail "staged sweep_gate.py hash mismatch"
test ! -e "$APP/btc_gate.py" || fail "btc_gate.py already exists; refusing overwrite"
test ! -e "$APP/sweep_gate.py" || fail "sweep_gate.py already exists; refusing overwrite"
test ! -e "$BACKUP" || fail "backup path already exists: $BACKUP"
systemctl is-active --quiet news-chart-bot.service || fail "Bybit bot was not active before release"

PYTHONPYCACHEPREFIX="$STAGE/pycache" "$PYTHON" -m py_compile \
  "$BOT/market.py" "$BOT/alert_policy.py" "$BOT/btc_gate.py" "$BOT/sweep_gate.py"
PYTHONPATH="$BOT:$APP" "$PYTHON" -c \
  'import market, alert_policy, btc_gate, sweep_gate; assert hasattr(market, "Engine")'

install -d -o root -g root -m 0755 "$BACKUP"
cp -a "$APP/market.py" "$BACKUP/market.py"
cp -a "$APP/alert_policy.py" "$BACKUP/alert_policy.py"
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP/release-started-utc.txt"

modified=0
rollback() {
  rc=$?
  if (( rc != 0 && modified )); then
    echo "RELEASE_ROLLBACK: restoring prior bot files" >&2
    cp -a "$BACKUP/market.py" "$APP/market.py" || true
    cp -a "$BACKUP/alert_policy.py" "$APP/alert_policy.py" || true
    rm -f "$APP/btc_gate.py" "$APP/sweep_gate.py"
    systemctl restart news-chart-bot.service || true
  fi
  exit "$rc"
}
trap rollback EXIT
modified=1

for file in btc_gate.py sweep_gate.py alert_policy.py market.py; do
  install -o root -g root -m 0644 "$BOT/$file" "$APP/.${file}.release-new"
  mv -f "$APP/.${file}.release-new" "$APP/$file"
done

systemctl restart news-chart-bot.service
systemctl is-active --quiet news-chart-bot.service || fail "bot failed health check after restart"
PYTHONPATH="$APP" "$PYTHON" -c \
  'import market, alert_policy, btc_gate, sweep_gate; assert hasattr(market, "Engine")'

PYTHONPATH="$APP" "$PYTHON" - <<'PY'
from datetime import datetime, timezone
from alert_policy import timeframe_alert_policy
ms = lambda value: int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)
assert timeframe_alert_policy("15", ms("2026-10-02T08:00:00")) == (True, None)
assert timeframe_alert_policy("15", ms("2026-10-02T07:59:59"))[0] is False
assert timeframe_alert_policy("240", ms("2026-10-02T00:00:00"))[0] is False
PY

systemctl is-active --quiet news-chart-bot.service || fail "bot stopped after smoke checks"
echo "DEPLOY_OK"
sha256sum "$APP/market.py" "$APP/alert_policy.py" "$APP/btc_gate.py" "$APP/sweep_gate.py"
echo "BACKUP=$BACKUP"
modified=0
trap - EXIT
