#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLE="$ROOT/deploy/apex_direction_20261003"
HOST="root@147.45.175.24"
KEY="$HOME/.ssh/rubkoff_server"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="/tmp/apex-sweep-choch-$STAMP"
BACKUP="/opt/backups/apex-sweep-choch-$STAMP"
FILES=(
  bot/alert_policy.py bot/level_age.py bot/market.py bot/scenario_contract.py
  bot/sweeps.py bot/setup_chain.py
  dashboard/app.js dashboard/index.html dashboard/server.py dashboard/portfolio.py
)

[[ -r "$KEY" ]] || { echo "SSH key not available: $KEY" >&2; exit 1; }
for rel in "${FILES[@]}"; do
  [[ -f "$BUNDLE/$rel" ]] || { echo "Release source missing: $rel" >&2; exit 1; }
done

SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
SCP=(scp -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
"${SSH[@]}" "$HOST" "install -d -o root -g root -m 0755 '$STAGE'"
tar -cf - -C "$BUNDLE" "${FILES[@]}" | "${SSH[@]}" "$HOST" "tar -xf - -C '$STAGE'"

ARGS=("$STAGE" "$BACKUP")
for rel in "${FILES[@]}"; do
  ARGS+=("$rel" "$(shasum -a 256 "$BUNDLE/$rel" | awk '{print $1}')")
done

"${SSH[@]}" "$HOST" bash -s -- "${ARGS[@]}" <<'REMOTE'
set -Eeuo pipefail
STAGE="$1"; BACKUP="$2"; shift 2
APP=/opt/news-chart-bot
DASH=/opt/apex-dashboard
DB=/var/lib/news-chart-bot/state.sqlite3
PY="$APP/.venv/bin/python"
fail() { echo "RELEASE_ABORTED: $*" >&2; exit 1; }
declare -A HASHES TARGETS
while (($#)); do HASHES["$1"]="$2"; shift 2; done
FILES=(
  bot/alert_policy.py bot/level_age.py bot/market.py bot/scenario_contract.py
  bot/sweeps.py bot/setup_chain.py
  dashboard/app.js dashboard/index.html dashboard/server.py dashboard/portfolio.py
)
target_for() {
  case "$1" in
    bot/*) echo "$APP/${1#bot/}" ;;
    dashboard/*) echo "$DASH/${1#dashboard/}" ;;
    *) fail "unexpected release path $1" ;;
  esac
}
sha() { sha256sum "$1" | awk '{print $1}'; }

[[ -x "$PY" ]] || fail "bot Python environment missing"
[[ -f "$DB" ]] || fail "production DB missing"
[[ ! -e "$BACKUP" ]] || fail "backup path already exists: $BACKUP"
systemctl is-active --quiet news-chart-bot.service || fail "Bybit bot was not active before release"
systemctl is-active --quiet apex-dashboard.service || fail "APEX dashboard was not active before release"

NEW_FILES=(bot/setup_chain.py dashboard/portfolio.py)
for rel in "${FILES[@]}"; do
  src="$STAGE/$rel"; dst="$(target_for "$rel")"
  [[ -f "$src" ]] || fail "staged source missing: $rel"
  [[ "$(sha "$src")" == "${HASHES[$rel]}" ]] || fail "staged hash mismatch: $rel"
  if [[ " ${NEW_FILES[*]} " != *" $rel "* ]]; then
    [[ -f "$dst" ]] || fail "production file missing: $dst"
  fi
  TARGETS["$rel"]="$dst"
done

# Validate staged bot/dashboard modules against the live dependencies before mutation.
PYTHONPYCACHEPREFIX="$STAGE/pycache" "$PY" -m py_compile \
  "$STAGE/bot/market.py" "$STAGE/bot/scenario_contract.py" \
  "$STAGE/bot/sweeps.py" "$STAGE/bot/setup_chain.py" \
  "$STAGE/bot/alert_policy.py" "$STAGE/bot/level_age.py" \
  "$STAGE/dashboard/server.py" "$STAGE/dashboard/portfolio.py"
PYTHONPATH="$STAGE/bot:$APP" "$PY" - <<'PY'
import market, setup_chain, sweeps, scenario_contract
assert hasattr(market, "Engine")
assert hasattr(setup_chain, "process_closed_bar")
assert hasattr(sweeps, "sweep_candidates")
assert scenario_contract.VERSION == "sweep-choch-v1-level-age-moscow-policy"
PY
PYTHONPATH="$STAGE/dashboard:$DASH:$APP" "$PY" - <<'PY'
import server, portfolio
assert hasattr(server, "read_signals")
assert hasattr(portfolio, "read_portfolio")
PY

install -d -o root -g root -m 0755 "$BACKUP"
for rel in "${FILES[@]}"; do
  dst="${TARGETS[$rel]}"; backup="$BACKUP/$rel"
  install -d -o root -g root -m 0755 "$(dirname "$backup")"
  if [[ -e "$dst" ]]; then cp -a "$dst" "$backup"; else touch "$backup.absent"; fi
done
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP/release-started-utc.txt"

modified=0
rollback() {
  rc=$?
  if (( rc != 0 && modified )); then
    echo "RELEASE_ROLLBACK: restoring prior bot/dashboard files" >&2
    for rel in "${FILES[@]}"; do
      dst="${TARGETS[$rel]}"; backup="$BACKUP/$rel"
      if [[ -f "$backup.absent" ]]; then rm -f "$dst"; elif [[ -f "$backup" ]]; then cp -a "$backup" "$dst"; fi
    done
    systemctl restart news-chart-bot.service apex-dashboard.service || true
  fi
  exit "$rc"
}
trap rollback EXIT
modified=1

for rel in "${FILES[@]}"; do
  dst="${TARGETS[$rel]}"
  install -o root -g root -m 0644 "$STAGE/$rel" "$dst.release-new"
  mv -f "$dst.release-new" "$dst"
done

PYTHONPATH="$APP" "$PY" -m py_compile \
  "$APP/market.py" "$APP/scenario_contract.py" "$APP/sweeps.py" \
  "$APP/setup_chain.py" "$APP/alert_policy.py" "$APP/level_age.py" \
  "$DASH/server.py" "$DASH/portfolio.py"
PYTHONPATH="$APP" "$PY" - <<'PY'
import market, setup_chain, sweeps, scenario_contract
assert hasattr(market, "Engine") and hasattr(setup_chain, "process_closed_bar")
assert hasattr(sweeps, "sweep_candidates")
assert scenario_contract.VERSION == "sweep-choch-v1-level-age-moscow-policy"
PY
PYTHONPATH="$DASH:$APP" "$PY" - <<'PY'
import server, portfolio
assert hasattr(server, "read_signals") and hasattr(portfolio, "read_portfolio")
PY

systemctl restart news-chart-bot.service
systemctl restart apex-dashboard.service
systemctl is-active --quiet news-chart-bot.service || fail "Bybit bot failed post-restart health"
systemctl is-active --quiet apex-dashboard.service || fail "APEX dashboard failed post-restart health"
curl -fsS --max-time 10 http://127.0.0.1:5083/ -o /dev/null || fail "dashboard page check failed"
curl -fsS --max-time 10 http://127.0.0.1:5083/app.js -o /tmp/apex-sweep-choch-app.js || fail "dashboard asset check failed"
curl -fsS --max-time 10 http://127.0.0.1:5083/api/signals?limit=2 -o /tmp/apex-sweep-choch-api.json || fail "dashboard API check failed"
PYTHONPATH="$APP" "$PY" - <<'PY'
import json, sqlite3, urllib.request
with urllib.request.urlopen("http://127.0.0.1:5083/api/signals?limit=2", timeout=10) as response:
    data=json.load(response)
assert "strategy_stats" in data and "portfolio" in data and isinstance(data["signals"], list)
assert data["portfolio"]["model_version"] == "sweep-choch-v1-level-age-moscow-policy"
db=sqlite3.connect("file:/var/lib/news-chart-bot/state.sqlite3?mode=ro", uri=True)
tables={row[0] for row in db.execute("select name from sqlite_master where type='table'")}
assert {"active_sweep_setups", "sweep_setup_events"} <= tables
print("API strategy_stats:", data["strategy_stats"])
print("APEX model:", data["portfolio"]["model_version"])
print("Signal cards returned:", len(data["signals"]))
print("Telegram queue:", db.execute("select status,count(*) from signal_alerts group by status").fetchall())
print("Setup-state rows:", db.execute("select count(*) from active_sweep_setups").fetchone()[0])
PY
for rel in "${FILES[@]}"; do
  [[ "$(sha "${TARGETS[$rel]}")" == "${HASHES[$rel]}" ]] || fail "installed file hash mismatch: $rel"
done
systemctl is-active --quiet news-chart-bot.service || fail "bot stopped during smoke checks"
systemctl is-active --quiet apex-dashboard.service || fail "dashboard stopped during smoke checks"

echo "DEPLOY_OK"
echo "BACKUP=$BACKUP"
for rel in "${FILES[@]}"; do echo "$(sha "${TARGETS[$rel]}")  ${TARGETS[$rel]}"; done
modified=0
trap - EXIT
REMOTE

echo "Remote release completed: $BACKUP"
