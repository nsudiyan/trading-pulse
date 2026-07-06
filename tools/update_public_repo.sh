#!/bin/bash
# Обновление ПУБЛИЧНОГО снапшота github.com/nsudiyan/crypto-futures-screener
# из рабочего репо ~/trading. Пайплайн из памяти 2026-06-10 + грабли.
# Запускать РУКАМИ (публикация = решение человека): bash ~/trading/tools/update_public_repo.sh
set -euo pipefail

SRC=~/trading
DST=~/crypto-futures-screener

echo "── 1/5 архив отслеживаемых файлов (untracked-секреты физически не попадают)"
cd "$DST"
find . -maxdepth 1 -not -name '.git' -not -name '.' -not -name 'README.md' -exec rm -rf {} +
cd "$SRC" && git archive HEAD | tar -x -C "$DST"

echo "── 2/5 scrub: username из путей/ссылок"
cd "$DST"
grep -rl "nikitasudian" --include="*.py" --include="*.js" --include="*.md" \
     --include="*.html" --include="*.sh" . 2>/dev/null | while read -r f; do
  sed -i '' 's/nikitasudian/YOUR_USERNAME/g; s/nsudiyan/YOUR_USERNAME/g' "$f"
done

echo "── 3/5 секрет-свип (обязателен; ведущий дефис chat_id — грабля: grep -e)"
fail=0
for pat in "api_hash.{0,5}['\"]?[0-9a-f]{16,}" "session_string.{0,5}['\"]1Ap" \
           "bot_token['\"]?[[:space:]]*[:=][[:space:]]*['\"][0-9]{8,}:" \
           "sk-[A-Za-z0-9]{20,}" "gho_[A-Za-z0-9]{20,}" "AKIA[A-Z0-9]{16}"; do
  if grep -rqE -e "$pat" --include="*.py" --include="*.js" --include="*.json" \
        --include="*.md" --include="*.csv" . 2>/dev/null; then
    echo "❌ НАЙДЕН СЕКРЕТ по паттерну: $pat — ПУШ ОТМЕНЁН"; fail=1
  fi
done
[ "$fail" -eq 1 ] && exit 1
echo "   чисто ✓"

echo "── 4/5 коммит"
git add -A
git -c user.name="crypto-futures-screener" -c user.email="noreply@github.com" \
    commit -m "Update: Pulse dashboard, pump_watch, bias v3, screener gates, review fixes (2026-07-06)" \
    || { echo "нет изменений"; exit 0; }

echo "── 5/5 push (грабля: если 'Device not configured' → gh auth setup-git)"
git push origin HEAD
echo "✅ https://github.com/nsudiyan/crypto-futures-screener"
