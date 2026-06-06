#!/usr/bin/env python3
"""
check_score_gate.py — одноразовая проверка гипотезы про score-гейт (TimeGate ≥169).

Гипотеза (2026-05-29): высокий score НЕ предсказывает лучший исход у шортов —
ведро score 120-150 обгоняло 150-200 по среднему R. Если на свежих данных это
держится, значит пятничный TimeGate (≥169) режет НЕ туда и порог надо понижать.

Логика: считает по outcomes/resolved.csv статистику ШОРТОВ по score-бакетам
(0-120 / 120-150 / 150-200 / 200+) на сигналах ПОСЛЕ внедрения DistGate-0,
формирует вердикт и шлёт его В ЛИЧКУ (owner_chat_id), не в канал.

Запускается из cron ежедневно, но самостоятельно срабатывает ОДИН раз
на/после FIRE_ON_OR_AFTER и ставит маркер, чтобы не повторяться.
"""
import csv
import json
import datetime
import urllib.request
import urllib.parse
from pathlib import Path

BASE     = Path(__file__).parent
RESOLVED = BASE / "outcomes" / "resolved.csv"
MARKER   = BASE / ".score_gate_check_done"
CONFIG   = BASE / "telegram_config.json"

FIRE_ON_OR_AFTER = datetime.date(2026, 6, 1)   # когда напомнить
WINDOW_DAYS      = 10                            # катящееся окно: последние N дней (не фикс. дата)
CHANGE_DATE      = "2026-05-29"                  # дата внедрения DistGate-0 (счёт чистых post-change)
MIN_N            = 8                             # минимум сделок в ведре для уверенного вывода

BUCKETS = [(0, 120), (120, 150), (150, 200), (200, 9999)]


def _rmul(r):
    v = r.get("r_multiple_24h") or r.get("r_multiple_4h")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_short(r):
    return str(r.get("direction", "")).strip().lower() in ("шорт", "short")


def _is_win(r):
    return (r.get("outcome_24h") or r.get("outcome_4h") or "") in ("TP1", "TP2", "WIN")


def _score(r):
    try:
        return int(float(r.get("score", "")))
    except (TypeError, ValueError):
        return None


def _bucket_stats(rows):
    out = {}
    for lo, hi in BUCKETS:
        sub = [r for r in rows if (s := _score(r)) is not None and lo <= s < hi]
        rs  = [_rmul(r) for r in sub if _rmul(r) is not None]
        n   = len(rs)
        if not n:
            out[(lo, hi)] = (0, 0.0, 0.0, 0.0)
            continue
        wins = sum(1 for r in sub if _is_win(r))
        wr   = wins / n * 100
        tot  = sum(rs)
        out[(lo, hi)] = (n, wr, tot / n, tot)
    return out


def _load_shorts(since):
    if not RESOLVED.exists():
        return []
    with open(RESOLVED, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if _is_short(r) and (r.get("run_ts", "")[:10] >= since)]


def _verdict(stats):
    mid  = stats[(120, 150)]   # (n, wr, avgR, totR)
    high = stats[(150, 200)]
    n_mid, _, avg_mid, _   = mid
    n_high, _, avg_high, _ = high
    if n_mid < MIN_N or n_high < MIN_N:
        return ("МАЛО ДАННЫХ",
                f"В ведре 120-150 n={n_mid}, в 150-200 n={n_high} (надо ≥{MIN_N}). "
                "Продлить наблюдение — перезапусти проверку позже.")
    if avg_mid > avg_high:
        return ("ПОДТВЕРЖДАЕТСЯ ✅",
                f"120-150 avgR={avg_mid:+.3f} > 150-200 avgR={avg_high:+.3f}. "
                "Высокий score НЕ улучшает исход → пятничный TimeGate (≥169) режет не туда. "
                "Рекомендация: понизить/пересобрать порог.")
    return ("НЕ подтверждается ❌",
            f"120-150 avgR={avg_mid:+.3f} ≤ 150-200 avgR={avg_high:+.3f}. "
            "Высокий score себя оправдывает — порог TimeGate оставить как есть.")


def _send_personal(text):
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        token = cfg.get("bot_token")
        chat  = str(cfg.get("owner_chat_id") or cfg.get("chat_id"))  # ЛИЧКА, не канал
        if not token or not chat:
            print("[check_score_gate] TG не настроен")
            return False
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print(f"[check_score_gate] ошибка отправки: {e}")
        return False


def build_report():
    cutoff = (datetime.date.today() - datetime.timedelta(days=WINDOW_DAYS)).isoformat()
    shorts = _load_shorts(cutoff)
    post   = sum(1 for r in shorts if r.get("run_ts", "")[:10] >= CHANGE_DATE)
    stats  = _bucket_stats(shorts)
    label, note = _verdict(stats)

    lines = [
        "🔔 Напоминание-проверка: score-гейт (TimeGate ≥169)",
        f"Шорты за последние {WINDOW_DAYS} дней (с {cutoff}): n={len(shorts)}",
        f"  из них чистых post-change (≥{CHANGE_DATE}): {post}",
        "",
        "Ведро score | n | WR | avgR | totR",
    ]
    for lo, hi in BUCKETS:
        n, wr, avg, tot = stats[(lo, hi)]
        band = f"{lo}+" if hi == 9999 else f"{lo}-{hi}"
        lines.append(f"  {band}: n={n}  WR={wr:.0f}%  avgR={avg:+.3f}  totR={tot:+.1f}")
    lines += ["", f"ВЕРДИКТ: {label}", note]
    return "\n".join(lines)


def main():
    today = datetime.date.today()
    if today < FIRE_ON_OR_AFTER:
        return
    if MARKER.exists():
        return
    report = build_report()
    print(report)
    ok = _send_personal(report)
    if ok:
        MARKER.write_text(today.isoformat(), encoding="utf-8")
        print("[check_score_gate] вердикт отправлен в личку, маркер поставлен")
    else:
        print("[check_score_gate] отправка не удалась — маркер НЕ ставлю, повтор завтра")


if __name__ == "__main__":
    import sys
    if "--now" in sys.argv:        # ручной прогон для проверки (игнорит date-guard и маркер)
        print(build_report())
    else:
        main()
