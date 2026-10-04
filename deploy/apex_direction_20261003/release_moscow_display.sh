#!/bin/sh
set -eu

stage=/tmp/pulse-moscow-ui-20261004
backup=/opt/pulse-moscow-ui-backup-20261004
check() {
    actual=$(sha256sum "$1" | cut -d ' ' -f 1)
    test "$actual" = "$2"
}

# Exact production base after the guarded level-age/session release.
check /opt/news-chart-bot/market.py 569fc33e92643ac16e78927e9eda1a1ba731a0eb0040f87242a3f94cff86f11b
check /opt/apex-dashboard/app.js 7f3ef5636a0967f51b6707de6745588c47185c6e65150b0e80bb3c074a7ed517
check /opt/apex-dashboard/index.html 517f362b7ac6a7d7dbbb14b26f54d676d27a9ad2c106b03cf896ace6498ec8d4
test ! -e "$backup"

# Verify only the three reviewed, staged display-time changes.
check "$stage/bot/market.py" 4c59e541fadd261e6818b5caa173a7daa534e3e0af475e6fd1b2d55f94fc92a8
check "$stage/dashboard/app.js" 593fe66116f9927d4d5d1846ce1285f7f28f94f859b0a65233ecae5ca8d744dd
check "$stage/dashboard/index.html" 0d486e08bcbb366d73188fece5501d1b6dc719d579d8ab775873d4d804d57243

mkdir -p "$backup/bot" "$backup/dashboard"
cp -p /opt/news-chart-bot/market.py "$backup/bot/"
cp -p /opt/apex-dashboard/app.js /opt/apex-dashboard/index.html "$backup/dashboard/"
backup_ready=1
release_ok=0
rollback() {
    rc=$?
    if test "$release_ok" -eq 1 || test "$backup_ready" -ne 1; then return; fi
    trap - EXIT
    set +e
    cp -p "$backup/bot/market.py" /opt/news-chart-bot/
    cp -p "$backup/dashboard/app.js" "$backup/dashboard/index.html" /opt/apex-dashboard/
    systemctl restart news-chart-bot.service apex-dashboard.service
    echo RELEASE_ROLLED_BACK
    exit "$rc"
}
trap rollback EXIT

install -m 644 "$stage/bot/market.py" /opt/news-chart-bot/market.py.new
mv /opt/news-chart-bot/market.py.new /opt/news-chart-bot/market.py
for f in app.js index.html; do
    install -m 644 "$stage/dashboard/$f" "/opt/apex-dashboard/$f.new"
    mv "/opt/apex-dashboard/$f.new" "/opt/apex-dashboard/$f"
done

/opt/news-chart-bot/.venv/bin/python -m py_compile /opt/news-chart-bot/market.py
node --check /opt/apex-dashboard/app.js
(cd /tmp && PYTHONPATH=/opt/news-chart-bot /opt/news-chart-bot/.venv/bin/python -c \
    'import market; print("PRODUCTION_IMPORT_OK")')

if ! systemctl restart news-chart-bot.service apex-dashboard.service; then
    echo RELEASE_RESTART_FAILED
    exit 1
fi
sleep 3
systemctl is-active --quiet news-chart-bot.service
systemctl is-active --quiet apex-dashboard.service
curl --fail --silent --max-time 10 http://127.0.0.1:5083/ |
    grep -Fq 'Времена интерфейса — МСК'
curl --fail --silent --max-time 10 http://127.0.0.1:5083/app.js |
    grep -Fq 'function mskFull(value)'
curl --fail --silent --max-time 10 'http://127.0.0.1:5083/api/signals?limit=1' |
    /opt/news-chart-bot/.venv/bin/python -c \
    'import json,sys; r=json.load(sys.stdin); assert isinstance(r.get("signals"), list); print("RELEASE_API_OK")'

release_ok=1
trap - EXIT
echo RELEASE_OK
