#!/usr/bin/env python3
"""
SCANNER BOT RUNNER (2026-05-31) — аудиторный бот "Вариант 1"
============================================================
Standalone мульти-юзерный Telegram-бот: любой пользователь пишет /scan /chart /macro
/learn /help и получает честный ответ от scanner_assistant.handle().

ВАЖНО — изоляция от живого:
- Использует ОТДЕЛЬНЫЙ токен (scanner_bot_config.json или env SCANNER_BOT_TOKEN), НЕ
  токен личного telegram_bot.py. Создай нового бота у @BotFather, вставь токен.
- Ничего не пишет в данные проекта, не трогает live-демоны. Только читает кэш/макро
  через scanner_assistant (read-only) и отвечает пользователям.
- Если токен не задан — печатает инструкцию и выходит (ничего не ломает).

Запуск:   python3 scanner_bot_run.py
Деплой:   позже — отдельный launchd job (НЕ создаю автоматически; решение владельца).
"""
import json, os, sys, time, urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = HERE / "scanner_bot_config.json"
TG = "https://api.telegram.org"
RATE_SEC = 2.0          # не чаще 1 команды / 2с на пользователя
_last_user = {}         # user_id -> ts

try:
    import scanner_assistant as SA
except Exception as e:
    print(f"ОШИБКА: не импортируется scanner_assistant: {e}"); sys.exit(1)

def _log(m): print(f"[ScannerBot] {time.strftime('%H:%M:%S')} {m}", flush=True)

def _token():
    if CFG.exists():
        try:
            t = json.loads(CFG.read_text(encoding="utf-8")).get("bot_token")
            if t: return t
        except Exception: pass
    return os.environ.get("SCANNER_BOT_TOKEN")

def _api(token, method, params=None, timeout=35):
    url = f"{TG}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _send(token, chat_id, html):
    # Telegram лимит 4096 символов — режем на части по строкам при необходимости
    chunks, cur = [], ""
    for line in html.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip(): chunks.append(cur)
    for ch in chunks:
        _api(token, "sendMessage", {
            "chat_id": chat_id, "text": ch, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })

def main():
    token = _token()
    if not token:
        print(
            "\n⚠️  Токен не задан.\n"
            "1) Создай бота: открой @BotFather в Telegram → /newbot → получи токен.\n"
            "2) Вставь его одним из способов:\n"
            f"   • в файл {CFG}:  {{\"bot_token\": \"123:ABC...\"}}\n"
            "   • или env:  export SCANNER_BOT_TOKEN=123:ABC...\n"
            "3) Запусти снова: python3 scanner_bot_run.py\n"
            "\nЭто ОТДЕЛЬНЫЙ бот для аудитории (не трогает личный telegram_bot).\n"
        )
        sys.exit(0)

    me = _api(token, "getMe")
    if not me.get("ok"):
        _log(f"Неверный токен: {me.get('description') or me.get('error')}"); sys.exit(1)
    _log(f"Запущен: @{me['result']['username']} (мульти-юзер, scanner+TA, не сигналы)")

    offset, fails = 0, 0
    while True:
        try:
            upd = _api(token, "getUpdates", {"offset": offset, "timeout": 30})
            if not upd.get("ok"):
                fails += 1; time.sleep(min(5 * fails, 60)); continue
            fails = 0
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not chat_id or not text: continue
                # rate-limit на пользователя
                now = time.time()
                if now - _last_user.get(chat_id, 0) < RATE_SEC:
                    continue
                _last_user[chat_id] = now
                try:
                    if text.startswith("/"):
                        reply = SA.handle(text)
                    else:
                        # любое не-командное сообщение от нового юзера → онбординг
                        reply = SA.onboarding()
                    _send(token, chat_id, reply)
                    _log(f"{chat_id}: {text[:40]}")
                except Exception as e:
                    _log(f"handle error: {e}")
                    _send(token, chat_id, "⚠️ Временная ошибка, попробуй ещё раз.\n\n" + SA.DISCLAIMER)
        except KeyboardInterrupt:
            _log("Остановлен."); break
        except Exception as e:
            _log(f"polling error: {e}"); fails += 1; time.sleep(min(5 * fails, 60))

if __name__ == "__main__":
    main()
