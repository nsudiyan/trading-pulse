#!/bin/sh
set -eu

stage=/tmp/pulse-alert-policy-20261004
backup=/opt/pulse-alert-policy-backup-20261004
check() {
    actual=$(sha256sum "$1" | cut -d ' ' -f 1)
    test "$actual" = "$2"
}

# Exact live base recorded during read-only preflight. Abort on drift.
check /opt/news-chart-bot/market.py 22b9b7e28c7540b68080be9c58e5555e4be037ff88b3d5a31aaa16966b56c393
check /opt/news-chart-bot/scenario_contract.py 28a8eea2f5e06635afed81ac49d9fc625127694c36d5a8ae853ad0792b9f674d
check /opt/apex-dashboard/app.js 40d37a248348f32cabd2225dfb4ca71bae7e859dba2121b3b2e845f8795c2c2b
check /opt/apex-dashboard/server.py 796aa7a8ae96a97a3b13acec3d0dcb23b5de5ccdf41e74a6071ad89693dee548
test ! -e /opt/news-chart-bot/alert_policy.py
test ! -e /opt/news-chart-bot/level_age.py
test ! -e "$backup"

# Verify all staged deployment artifacts before changing production.
check "$stage/bot/market.py" 569fc33e92643ac16e78927e9eda1a1ba731a0eb0040f87242a3f94cff86f11b
check "$stage/bot/scenario_contract.py" 29cf8f91d6a4b0178c1ef23749eed5cca9ab4e31f50505966122c7571126de6d
check "$stage/bot/alert_policy.py" a130d6367939084bca9ada88bf651a1860fa3ac17f4154f6cb6a3c48a8ebce04
check "$stage/bot/level_age.py" a1a57c6c5ba5309373af3626ecc9d251238e01bae3b5ea7554e87a4d6a407bed
check "$stage/dashboard/app.js" 7f3ef5636a0967f51b6707de6745588c47185c6e65150b0e80bb3c074a7ed517
check "$stage/dashboard/server.py" f0941e0e876cde6966c4f6eeabb014c2102bb4fdf92826243e94a89bae5f618a

mkdir -p "$backup/bot" "$backup/dashboard"
cp -p /opt/news-chart-bot/market.py /opt/news-chart-bot/scenario_contract.py "$backup/bot/"
cp -p /opt/apex-dashboard/app.js /opt/apex-dashboard/server.py "$backup/dashboard/"
backup_ready=1
release_ok=0
rollback() {
    rc=$?
    if test "$release_ok" -eq 1 || test "$backup_ready" -ne 1; then return; fi
    trap - EXIT
    set +e
    cp -p "$backup/bot/market.py" "$backup/bot/scenario_contract.py" /opt/news-chart-bot/
    cp -p "$backup/dashboard/app.js" "$backup/dashboard/server.py" /opt/apex-dashboard/
    rm -f /opt/news-chart-bot/alert_policy.py /opt/news-chart-bot/level_age.py
    systemctl restart news-chart-bot.service apex-dashboard.service
    echo RELEASE_ROLLED_BACK
    exit "$rc"
}
trap rollback EXIT

for f in market.py scenario_contract.py alert_policy.py level_age.py; do
    install -m 644 "$stage/bot/$f" "/opt/news-chart-bot/$f.new"
    mv "/opt/news-chart-bot/$f.new" "/opt/news-chart-bot/$f"
done
for f in server.py app.js; do
    install -m 644 "$stage/dashboard/$f" "/opt/apex-dashboard/$f.new"
    mv "/opt/apex-dashboard/$f.new" "/opt/apex-dashboard/$f"
done

/opt/news-chart-bot/.venv/bin/python -m py_compile \
    /opt/news-chart-bot/market.py /opt/news-chart-bot/scenario_contract.py \
    /opt/news-chart-bot/alert_policy.py /opt/news-chart-bot/level_age.py \
    /opt/apex-dashboard/server.py
(cd /tmp && PYTHONPATH=/opt/news-chart-bot /opt/news-chart-bot/.venv/bin/python -c \
    'import market, scenario_contract, alert_policy, level_age; assert scenario_contract.VERSION == "direction-context-v6-level-age-moscow-policy"; print("PRODUCTION_IMPORTS_OK")')

if ! systemctl restart news-chart-bot.service apex-dashboard.service; then
    echo RELEASE_RESTART_FAILED
    exit 1
fi
sleep 3
systemctl is-active --quiet news-chart-bot.service
systemctl is-active --quiet apex-dashboard.service
curl --fail --silent --max-time 10 'http://127.0.0.1:5083/api/signals?limit=1' |
    /opt/news-chart-bot/.venv/bin/python -c \
    'import json,sys; r=json.load(sys.stdin); assert isinstance(r.get("signals"), list); print("RELEASE_API_OK")'

release_ok=1
trap - EXIT
echo RELEASE_OK
