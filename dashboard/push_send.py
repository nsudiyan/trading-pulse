#!/usr/bin/env python3
"""Web Push с мака на подписанные устройства (зоны входа дашборда).
Подписки: dashboard/push_subs.json (СЕКРЕТ, в .gitignore) — список sub-JSON
из кнопки 🔔 на сайте. VAPID: dashboard/push_vapid.json (тоже секрет).
Отправка: npx web-push (уже в npx-кэше). Fail-open по каждому устройству;
подписка, ответившая 404/410 (протухла), выкидывается из файла.
Проверка: python3 push_send.py --test
"""
import json
import re
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
SUBS_PATH = DIR / "push_subs.json"
VAPID_PATH = DIR / "push_vapid.json"


def _subscription_expired(returncode: int, out: str) -> bool:
    """Протухла ли подписка (410 Gone / 404) — по выводу web-push CLI.
    Консервативно: True ТОЛЬКО при явном коде ОТ push-сервиса и НЕ при поломке
    самого npx/сети (иначе рискуем удалить ЖИВУЮ подписку). Асимметрия намеренная:
    ложно сохранить мёртвую подписку (лишний спавн раз в тик) безопаснее, чем
    молча потерять живую (код-ревью 2026-07-06 HIGH-2)."""
    low = (out or "").lower()
    npx_broke = any(m in low for m in (
        "npm error", "npm err", "command not found",
        "cannot find", "etarget", "enotfound", "network"))
    if returncode == 0 or npx_broke:
        return False
    return re.search(r"(statuscode|status code|response code)\D{0,12}(410|404)", low) is not None


def send_push(title: str, body: str, tag: str = "pulse-entry") -> int:
    """Шлёт пуш всем подпискам. Возвращает число успешных доставок."""
    try:
        subs = json.loads(SUBS_PATH.read_text()) if SUBS_PATH.exists() else []
        vapid = json.loads(VAPID_PATH.read_text())
    except Exception as e:
        print(f"[push] нет конфига: {e}")
        return 0
    if not subs:
        return 0
    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": "/"},
                         ensure_ascii=False)
    ok, alive = 0, []
    for sub in subs:
        try:
            r = subprocess.run(
                ["npx", "--yes", "web-push", "send-notification",
                 f"--endpoint={sub['endpoint']}",
                 f"--key={sub['keys']['p256dh']}",
                 f"--auth={sub['keys']['auth']}",
                 f"--vapid-subject=mailto:sudiyan.nikita@gmail.com",
                 f"--vapid-pubkey={vapid['publicKey']}",
                 f"--vapid-pvtkey={vapid['privateKey']}",
                 f"--payload={payload}"],
                capture_output=True, text=True, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            if _subscription_expired(r.returncode, out):
                print(f"[push] подписка протухла (410/404), удаляю: …{sub['endpoint'][-24:]}")
                continue
            alive.append(sub)
            if r.returncode == 0:
                ok += 1
            else:
                print(f"[push] fail (подписка сохранена): {out[:160]}")
        except Exception as e:
            alive.append(sub)
            print(f"[push] error: {e}")
    if len(alive) != len(subs):
        SUBS_PATH.write_text(json.dumps(alive, ensure_ascii=False, indent=1))
    return ok


def _selfcheck() -> None:
    """Кейсы протухания: живую подписку НЕ теряем при поломке npx/сети."""
    exp = _subscription_expired
    # мёртвая подписка — реальный ответ push-сервиса
    assert exp(1, "Error sending: statusCode: 410")
    assert exp(1, "Received unexpected response code: 404")
    # НЕ трогаем при поломке npx / сети (главный риск HIGH-2)
    assert not exp(1, "npm error code E404\nnpm error 404 Not Found web-push")
    assert not exp(1, "getaddrinfo ENOTFOUND registry.npmjs.org")
    assert not exp(1, "network timeout at: https://registry...")
    # успех — не трогаем, даже если "410" случайно в endpoint-токене
    assert not exp(0, "Push message sent. endpoint …abc410def404xyz")
    # неоднозначный ненулевой без явного statusCode — сохраняем (асимметрия)
    assert not exp(1, "some transient 410 mention without status prefix")
    print("push_send selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    elif "--test" in sys.argv:
        n = send_push("⚡ Пульс — проверка", "Пуши с мака работают. Зоны входа под надзором.")
        print(f"доставлено: {n}")
    else:
        print("использование: push_send.py --test")
