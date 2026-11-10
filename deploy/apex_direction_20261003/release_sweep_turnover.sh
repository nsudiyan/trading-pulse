#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/news-chart-bot
STAGE=/tmp/pulse-sweep-turnover-20261004
BACKUP=/opt/pulse-sweep-turnover-backup-20261004
BOT="$STAGE/bot"
PYTHON="$APP/.venv/bin/python"

OLD_MARKET=6fd006759295963f876fd22a5d73d3d2857499c48f3a7cf2637993a15a1ac2c2
OLD_SWEEP=e0fa93a05e7366205385184a045e52a8dca71fe7ef47e71a0669ef1089f2c541
OLD_STRONG=2fdba4d6edbf7672a86a0c3694b66d65e7d332780aa77aa57f8c7f86e9100841
NEW_MARKET=1bc96d33bbef0fa8df31f260f8489891686ee7c87e7a61f7ac820e94deed9428
NEW_SWEEP=9bc1da199bc69d8ad9eda83fca25fafa0d7cdeec08db9b708e08bec01d562bc9
NEW_STRONG=2248643b6c97c9dd345d6062a14c63f3e8e33abd2d5d42b1f253d78fb980e710

sha() { sha256sum "$1" | awk '{print $1}'; }
fail() { echo "RELEASE_ABORTED: $*" >&2; exit 1; }

test -d "$APP" || fail "application directory missing: $APP"
test -f "$BOT/market.py" -a -f "$BOT/sweep_gate.py" \
     -a -f "$BOT/strong_sweep.py" || fail "staged source bundle incomplete"
test "$(sha "$APP/market.py")" = "$OLD_MARKET" || fail "market.py drifted from reviewed baseline"
test "$(sha "$APP/sweep_gate.py")" = "$OLD_SWEEP" || fail "sweep_gate.py drifted from reviewed baseline"
test "$(sha "$APP/strong_sweep.py")" = "$OLD_STRONG" || fail "strong_sweep.py drifted from reviewed baseline"
test "$(sha "$BOT/market.py")" = "$NEW_MARKET" || fail "staged market.py hash mismatch"
test "$(sha "$BOT/sweep_gate.py")" = "$NEW_SWEEP" || fail "staged sweep_gate.py hash mismatch"
test "$(sha "$BOT/strong_sweep.py")" = "$NEW_STRONG" || fail "staged strong_sweep.py hash mismatch"
test ! -e "$BACKUP" || fail "backup path already exists: $BACKUP"
systemctl is-active --quiet news-chart-bot.service || fail "Bybit bot was not active before release"

PYTHONPYCACHEPREFIX="$STAGE/pycache" "$PYTHON" -m py_compile \
  "$BOT/market.py" "$BOT/sweep_gate.py" "$BOT/strong_sweep.py"
PYTHONPATH="$BOT:$APP" "$PYTHON" -c \
  'import market, sweep_gate, strong_sweep; assert hasattr(market, "Engine")'

install -d -o root -g root -m 0755 "$BACKUP"
cp -a "$APP/market.py" "$APP/sweep_gate.py" "$APP/strong_sweep.py" "$BACKUP/"
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP/release-started-utc.txt"

modified=0
rollback() {
  rc=$?
  if (( rc != 0 && modified )); then
    echo "RELEASE_ROLLBACK: restoring prior sweep files" >&2
    for file in market.py sweep_gate.py strong_sweep.py; do
      cp -a "$BACKUP/$file" "$APP/$file" || true
    done
    systemctl restart news-chart-bot.service || true
  fi
  exit "$rc"
}
trap rollback EXIT
modified=1

for file in market.py sweep_gate.py strong_sweep.py; do
  install -o root -g root -m 0644 "$BOT/$file" "$APP/.${file}.release-new"
  mv -f "$APP/.${file}.release-new" "$APP/$file"
done

systemctl restart news-chart-bot.service
systemctl is-active --quiet news-chart-bot.service || fail "bot failed health check after restart"
PYTHONPATH="$APP" "$PYTHON" - <<'PY'
import market, sweep_gate, strong_sweep
assert hasattr(market, "Engine")
step = 900_000
bars = [{"start_ms": i * step, "end_ms": (i + 1) * step - 1,
         "volume": 1.0, "turnover": 10.0 if i < 14 else 25.0}
        for i in range(15)]
assert sweep_gate.sweep_activity_ratio(bars, "linear") == 2.5
assert sweep_gate.sweep_activity_ratio(bars, "inverse") == 1.0
PY
systemctl is-active --quiet news-chart-bot.service || fail "bot stopped after smoke checks"
echo "DEPLOY_OK"
sha256sum "$APP/market.py" "$APP/sweep_gate.py" "$APP/strong_sweep.py"
echo "BACKUP=$BACKUP"
modified=0
trap - EXIT
