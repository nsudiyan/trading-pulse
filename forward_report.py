#!/usr/bin/env python3
"""
FORWARD REPORT (2026-05-31) — еженедельный статус форвард-тестов → в ЛС владельцу.
Запускается launchd (com.trading.forwardreport). Показывает, сколько накопили
и бьёт ли эдж критерий. Сигналы добавляем ТОЛЬКО когда форвард пройдёт все пункты.
READ-ONLY (кроме TG-сообщения в личку).
"""
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "/opt/homebrew/bin/python3"

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _run(script, *args):
    try:
        r = subprocess.run([PY, str(HERE / script), *args],
                           capture_output=True, text=True, timeout=150, cwd=str(HERE))
        return (r.stdout.strip() or r.stderr.strip()[:400]) or "(нет вывода)"
    except Exception as e:
        return f"[{script}] ошибка: {e}"

def main():
    rug = _run("rug_short_forward.py", "--stop", "10")
    bos = _run("bos_fvg_short_forward.py", "--lookback-days", "90")
    parts = [
        "<b>📈 ФОРВАРД-ОТЧЁТ</b> (еженедельно)", "",
        "<b>RUG-SHORT:</b>",
        "<pre>" + _esc(rug[-1400:]) + "</pre>",
        "<b>BOS_FVG-SHORT:</b>",
        "<pre>" + _esc(bos[-1200:]) + "</pre>",
        "",
        "<i>Сигналы добавим ТОЛЬКО когда форвард пройдёт ВСЁ: net-of-costs плюс + "
        "робастность в обеих половинах + стабильность к параметрам + не бета. "
        "Пока хоть один пункт не выполнен — сигналов нет.</i>",
    ]
    msg = "\n".join(parts)
    try:
        import telegram_alerts as _ta
        cfg = _ta.load_config()
        tok = cfg.get("bot_token"); chat = str(cfg.get("owner_chat_id") or cfg.get("chat_id"))
        if tok and chat:
            _ta._send(tok, chat, msg)
            print("[ForwardReport] ✅ отправлено в ЛС")
        else:
            print("[ForwardReport] нет токена/чата")
    except Exception as e:
        print(f"[ForwardReport] не отправлено: {e}")

if __name__ == "__main__":
    main()
