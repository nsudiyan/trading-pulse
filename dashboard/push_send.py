#!/usr/bin/env python3
"""Web Push с мака на подписанные устройства (зоны входа дашборда).
Подписки: dashboard/push_subs.json (СЕКРЕТ, в .gitignore) — список sub-JSON
из кнопки 🔔 на сайте. VAPID: dashboard/push_vapid.json (тоже секрет).
Отправка: npx web-push (уже в npx-кэше). Fail-open по каждому устройству;
подписка, ответившая 404/410 (протухла), выкидывается из файла.
Проверка: python3 push_send.py --test
"""
import json
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
SUBS_PATH = DIR / "push_subs.json"
VAPID_PATH = DIR / "push_vapid.json"


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
            out = r.stdout + r.stderr
            if "410" in out or "404" in out:
                print(f"[push] подписка протухла, удаляю: …{sub['endpoint'][-24:]}")
                continue
            alive.append(sub)
            if r.returncode == 0:
                ok += 1
            else:
                print(f"[push] fail: {out[:160]}")
        except Exception as e:
            alive.append(sub)
            print(f"[push] error: {e}")
    if len(alive) != len(subs):
        SUBS_PATH.write_text(json.dumps(alive, ensure_ascii=False, indent=1))
    return ok


if __name__ == "__main__":
    if "--test" in sys.argv:
        n = send_push("⚡ Пульс — проверка", "Пуши с мака работают. Зоны входа под надзором.")
        print(f"доставлено: {n}")
    else:
        print("использование: push_send.py --test")
