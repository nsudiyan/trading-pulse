#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/news-chart-bot
STAGE=/tmp/pulse-sweep-detector-20261004
BACKUP=/opt/pulse-sweep-detector-backup-20261004
BOT="$STAGE/bot"
PYTHON="$APP/.venv/bin/python"

OLD_SWEEPS=ad524fe3fa697c8266d0f2d54b761f14577f5676c7e9b355a51475f9bbf7deab
NEW_SWEEPS=85fb4301d4237f2c3095ee7b0dc465b751aa08e2ef748559cbab2979cc7eebeb

sha() { sha256sum "$1" | awk '{print $1}'; }
fail() { echo "RELEASE_ABORTED: $*" >&2; exit 1; }

test -f "$APP/sweeps.py" || fail "production sweeps.py missing"
test -f "$BOT/sweeps.py" || fail "staged sweeps.py missing"
test "$(sha "$APP/sweeps.py")" = "$OLD_SWEEPS" || fail "sweeps.py drifted from reviewed baseline"
test "$(sha "$BOT/sweeps.py")" = "$NEW_SWEEPS" || fail "staged sweeps.py hash mismatch"
test ! -e "$BACKUP" || fail "backup path already exists: $BACKUP"
systemctl is-active --quiet news-chart-bot.service || fail "Bybit bot was not active before release"

PYTHONPYCACHEPREFIX="$STAGE/pycache" "$PYTHON" -m py_compile "$BOT/sweeps.py"
PYTHONPATH="$BOT:$APP" "$PYTHON" -c \
  'import market, strong_sweep, sweeps; assert market.recent_sweep is sweeps.recent_sweep'

install -d -o root -g root -m 0755 "$BACKUP"
cp -a "$APP/sweeps.py" "$BACKUP/sweeps.py"
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP/release-started-utc.txt"

modified=0
rollback() {
  rc=$?
  if (( rc != 0 && modified )); then
    echo "RELEASE_ROLLBACK: restoring previous sweep detector" >&2
    cp -a "$BACKUP/sweeps.py" "$APP/sweeps.py" || true
    systemctl restart news-chart-bot.service || true
  fi
  exit "$rc"
}
trap rollback EXIT
modified=1

install -o root -g root -m 0644 "$BOT/sweeps.py" "$APP/.sweeps.py.release-new"
mv -f "$APP/.sweeps.py.release-new" "$APP/sweeps.py"
systemctl restart news-chart-bot.service
systemctl is-active --quiet news-chart-bot.service || fail "bot failed health check after restart"
PYTHONPATH="$APP" "$PYTHON" - <<'PY'
import market, strong_sweep, sweeps
assert market.recent_sweep is sweeps.recent_sweep
bars = [{"start_ms": i * 900_000, "end_ms": (i + 1) * 900_000 - 1,
         "high": 105.0, "low": 99.0, "close": 100.0,
         "volume": 0.0} for i in range(21)]
bars[5]["low"] = 95.0
bars[12]["low"] = 95.1
bars[-1]["low"] = 94.0
pattern = sweeps.equal_level_sweep(bars)
assert pattern and pattern["direction"] == "BUY" and pattern["level"] == 95.0
assert "volume_ratio" not in pattern
PY
systemctl is-active --quiet news-chart-bot.service || fail "bot stopped after smoke checks"
echo "DEPLOY_OK"
sha256sum "$APP/sweeps.py"
echo "BACKUP=$BACKUP"
modified=0
trap - EXIT
