"""
claude_analyst.py — ИИ-анализ статистики торгового бота через Claude API.

Читает outcomes/resolved.csv, считает WR по сетапам/часам/дням,
отправляет сжатую статистику в Claude и получает конкретные рекомендации:
что убрать, что повысить/понизить, какие пороги изменить.

CLI:
    python3 claude_analyst.py              -- полный анализ (последние 500 сделок)
    python3 claude_analyst.py --n 200      -- последние N сделок
    python3 claude_analyst.py --tg         -- отправить отчёт в Telegram
"""

import csv
import json
import os
import sys
import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# P1-8c: маскировка bot-токена в логируемых ошибках (URL в requests-исключениях)
try:
    from telegram_alerts import redact_token
except Exception:                                  # автономный запуск без telegram_alerts
    import re as _re_rt
    def redact_token(s):
        return _re_rt.sub(r"/bot\d+:[\w-]+", "/bot<REDACTED>", str(s))


import anthropic

from claude_client import get_client  # единый singleton-клиент (без утечки сокетов)

BASE_DIR          = Path(__file__).parent
RESOLVED_CSV      = BASE_DIR / "outcomes" / "resolved.csv"
REPORT_PATH       = BASE_DIR / "outcomes" / "claude_report.md"
CODE_PROMPT_PATH  = BASE_DIR / "outcomes" / "code_prompt.md"

WIN_OUTCOMES  = {"TP1", "WIN"}
LOSS_OUTCOMES = {"STOP", "LOSS"}


# ─── .env loader ──────────────────────────────────────────────────────────────

def _load_dotenv():
    dotenv_path = BASE_DIR / ".env"
    if not dotenv_path.exists():
        return
    with open(dotenv_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip(); val = val.strip()
            if val and val[0] in ('"', "'") and val[-1] == val[0]:
                val = val[1:-1]
            os.environ.setdefault(key, val)


_load_dotenv()


# ─── Загрузка и агрегация CSV ─────────────────────────────────────────────────

def load_resolved(n: int = 500) -> list[dict]:
    rows = []
    try:
        with open(RESOLVED_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except FileNotFoundError:
        print(f"[ERROR] Файл не найден: {RESOLVED_CSV}")
        sys.exit(1)
    return rows[-n:]


def _wr(wins: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{wins / total * 100:.1f}% (n={total})"


def build_stats(rows: list[dict]) -> dict:
    by_setup   = defaultdict(lambda: {"w": 0, "l": 0})
    by_hour    = defaultdict(lambda: {"w": 0, "l": 0})
    by_day     = defaultdict(lambda: {"w": 0, "l": 0})
    by_score   = defaultdict(lambda: {"w": 0, "l": 0})
    by_dir     = defaultdict(lambda: {"w": 0, "l": 0})
    total_w = total_l = 0

    for r in rows:
        oc = r.get("outcome_24h", "")
        if oc not in WIN_OUTCOMES | LOSS_OUTCOMES:
            continue
        win  = oc in WIN_OUTCOMES
        setup = r.get("setup", "?")
        direction = r.get("direction", "?")

        try:
            dt   = datetime.fromisoformat(r.get("run_ts", ""))
            hour = dt.hour
            day  = dt.strftime("%A")
        except Exception:
            hour, day = -1, "?"

        try:
            score = float(r.get("score", 0) or 0)
            bracket = f"{int(score // 20) * 20}-{int(score // 20) * 20 + 20}"
        except Exception:
            bracket = "?"

        for d in (by_setup[setup], by_hour[hour], by_day[day],
                  by_score[bracket], by_dir[direction]):
            if win:
                d["w"] += 1
            else:
                d["l"] += 1

        if win:
            total_w += 1
        else:
            total_l += 1

    def fmt(d: dict) -> dict:
        t = d["w"] + d["l"]
        return {"wins": d["w"], "losses": d["l"], "total": t,
                "wr": round(d["w"] / t * 100, 1) if t else 0}

    return {
        "overall":   {"wins": total_w, "losses": total_l,
                      "total": total_w + total_l,
                      "wr": round(total_w / (total_w + total_l) * 100, 1)
                            if (total_w + total_l) else 0},
        "by_setup":  {k: fmt(v) for k, v in sorted(by_setup.items())},
        "by_hour":   {str(k): fmt(v) for k, v in sorted(by_hour.items())
                      if k != -1},
        "by_day":    {k: fmt(v) for k, v in by_day.items()},
        "by_score":  {k: fmt(v) for k, v in sorted(by_score.items())},
        "by_direction": {k: fmt(v) for k, v in by_dir.items()},
    }


# ─── Промпт для Claude ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — эксперт по квантовому трейдингу и анализу алготорговых систем.
Анализируешь статистику Bybit-фьючерсного скринера.
Твоя задача — дать КОНКРЕТНЫЕ, ЧИСЛОВЫЕ рекомендации:
- что убрать (сетап/фильтр/символ с плохим WR)
- что повысить/понизить (score пороги, hour фильтры)
- что приоритизировать (лучшие комбинации)
- предупредить о рисках (маленькая выборка, деградация WR)

Отвечай на русском. Используй таблицы и маркированные списки.
Будь прямолинеен — не воды, только факты и цифры."""

USER_TEMPLATE = """Вот статистика последних {n} сделок моего торгового бота (Bybit Linear Perpetuals):

## Общий WR (24H)
{overall}

## WR по сетапам
{by_setup}

## WR по часам UTC
{by_hour}

## WR по дням недели
{by_day}

## WR по score диапазонам
{by_score}

## WR по направлению (LONG/SHORT)
{by_direction}

---
Текущие настройки системы:
- HARD BLOCK часы: 7, 8, 11, 12, 14, 18, 19 UTC
- BAD часы (min score 165): 1, 13, 20, 23, 0 UTC
- GOOD часы: 5, 10, 17, 21 UTC
- Суббота min score: 195, Пятница: 169, Вторник: 150
- bos_fvg: ОТКЛЮЧЁН, squeeze min=100, short_dist min=85 max=150, swing min=75
- MAX_SCORE_GLOBAL: 180 (сигналы выше блокируются)
- Чёрный список: WETUSDT, LABUSDT, TONUSDT, ASTERUSDT, ARIAUSDT
- Cooldown: 8h между сигналами по одной паре

Вопросы:
1. Какие сетапы стоит отключить или повысить пороги?
2. Какие часы добавить в HARD BLOCK или BAD список?
3. Нужно ли менять пороги score для конкретных дней/сетапов?
4. Что работает хорошо и не стоит трогать?
5. Топ-3 изменения с наибольшим ожидаемым эффектом на WR.
"""


def format_stats_for_prompt(stats: dict, n: int) -> str:
    def tbl(d: dict) -> str:
        lines = []
        for k, v in sorted(d.items(), key=lambda x: -x[1]["wr"]):
            lines.append(f"  {k}: WR={v['wr']}% (n={v['total']}, "
                         f"wins={v['wins']}, losses={v['losses']})")
        return "\n".join(lines) if lines else "  нет данных"

    return USER_TEMPLATE.format(
        n=n,
        overall=(f"WR={stats['overall']['wr']}% "
                 f"(n={stats['overall']['total']}, "
                 f"wins={stats['overall']['wins']}, "
                 f"losses={stats['overall']['losses']})"),
        by_setup=tbl(stats["by_setup"]),
        by_hour=tbl(stats["by_hour"]),
        by_day=tbl(stats["by_day"]),
        by_score=tbl(stats["by_score"]),
        by_direction=tbl(stats["by_direction"]),
    )


# ─── Claude запрос ────────────────────────────────────────────────────────────

def ask_claude(prompt_text: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY не найден в .env или окружении")
        sys.exit(1)

    client = get_client(api_key=api_key)
    print("[Claude] Отправляю запрос... (~10-20 секунд)")

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt_text}],
    )
    return message.content[0].text


# ─── Telegram отправка ────────────────────────────────────────────────────────

def send_to_telegram(text: str):
    try:
        import telegram_alerts as _tg
        cfg = _tg.load_config()
        token   = cfg.get("bot_token")
        chat_id = cfg.get("chat_id") or cfg.get("owner_chat_id")
        if not token or not chat_id:
            print("[TG] Не настроен telegram_config.json")
            return

        import requests
        # Telegram лимит 4096 символов — режем на части
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for i, chunk in enumerate(chunks, 1):
            # Claude использует CommonMark **bold**, Telegram legacy Markdown — *bold*
            chunk = chunk.replace("**", "*")
            prefix = f"📊 *Claude Analyst* (часть {i}/{len(chunks)})\n\n" if len(chunks) > 1 else "📊 *Claude Analyst*\n\n"
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": prefix + chunk,
                      "parse_mode": "Markdown"},
                timeout=10,
            )
        print(f"[TG] Отправлено {len(chunks)} сообщений")
    except Exception as e:
        print(f"[TG] Ошибка: {redact_token(e)}")


# ─── Функция 1: generate_code_prompt ─────────────────────────────────────────

_CODE_PROMPT_SYSTEM = """Ты — Claude Code assistant, специализирующийся на Python-коде торговых систем.
Получаешь аналитический отчёт о торговом боте на Bybit и генерируешь точный промпт для Claude Code.

Промпт должен содержать:
1. Конкретные имена Python-констант и новые значения
2. Точные функции которые нужно изменить (с указанием что именно менять)
3. Обоснование каждого изменения — цифры из отчёта

Формат вывода:
```
Прочитай screener.py и примени следующие изменения:

1. [Константа/функция] — [что изменить] — [почему: WR X%→Y%]
2. ...
```

Пиши только текст промпта, без вводных слов. На русском языке."""


def generate_code_prompt(report: str) -> str:
    """
    Берёт текст аналитического отчёта → генерирует готовый Claude Code промпт.
    Сохраняет в outcomes/code_prompt.md.
    Возвращает текст промпта или '' при ошибке.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[CodePrompt] ANTHROPIC_API_KEY не найден — пропуск")
        return ""

    try:
        client = get_client(api_key=api_key)
        print("[CodePrompt] Генерирую Claude Code промпт...")

        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=_CODE_PROMPT_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    "На основе этого аналитического отчёта сгенерируй точный промпт "
                    "для Claude Code, который изменит screener.py:\n\n"
                    f"{report}"
                ),
            }],
        )
        prompt_text = msg.content[0].text

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        full = f"# Claude Code Prompt\n*Сгенерирован: {ts}*\n\n{prompt_text}"
        CODE_PROMPT_PATH.write_text(full, encoding="utf-8")
        print(f"[CodePrompt] Сохранён: {CODE_PROMPT_PATH}")
        return prompt_text

    except Exception as e:
        print(f"[CodePrompt] Ошибка API: {e}")
        return ""


# ─── Функция 3: analyze_missed_moves ─────────────────────────────────────────

_MISSED_SYSTEM = """Ты — эксперт по алготрейдингу на крипто-фьючерсах.
Анализируешь случаи, когда сигнал торгового бота получил исход FLAT,
но цена потом прошла более 3% в нужную сторону.

Твоя задача:
1. Найти общие паттерны в этих "пропущенных" движениях
2. Объяснить почему TP1 не был достигнут (TP слишком далеко, вход неточный, стоп широкий)
3. Предложить конкретные изменения в расчёте TP1/SL

Пиши на русском, кратко, с конкретными числами."""

_MISSED_THRESHOLD = 3.0  # % движение чтобы считать "пропущенным"


def analyze_missed_moves(rows: list[dict]) -> str:
    """
    Находит FLAT сигналы где цена прошла >3% в нужную сторону.
    Спрашивает Claude почему TP1 не был достигнут.
    Возвращает текст анализа или '' при ошибке/нет данных.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[MissedMoves] ANTHROPIC_API_KEY не найден — пропуск")
        return ""

    # Собираем FLAT сигналы с движением >3% в нужную сторону
    missed: list[dict] = []
    for r in rows:
        if r.get("outcome_24h", "") != "FLAT":
            continue
        direction = r.get("direction", "").upper()
        try:
            chg  = float(r.get("change_24h_pct") or 0)
            mfe  = float(r.get("mfe_24h_pct")    or 0)
        except (ValueError, TypeError):
            continue

        is_missed = (
            (direction == "LONG"  and (chg >  _MISSED_THRESHOLD or mfe >  _MISSED_THRESHOLD)) or
            (direction == "SHORT" and (chg < -_MISSED_THRESHOLD or mfe >  _MISSED_THRESHOLD))
        )
        if is_missed:
            missed.append(r)

    if not missed:
        print("[MissedMoves] Нет FLAT сигналов с движением >3% — пропуск")
        return ""

    print(f"[MissedMoves] Найдено {len(missed)} пропущенных движений, спрашиваю Claude...")

    # Компактное представление для промпта (макс. 25 случаев)
    cases = []
    for r in missed[-25:]:
        try:
            entry = float(r.get("price_entry") or 0)
            tp1   = float(r.get("tp1")          or 0)
            slv   = float(r.get("stop")         or 0)
            tp1_pct = (tp1 - entry) / entry * 100 if entry else 0
            sl_pct  = abs(slv - entry) / entry * 100 if entry else 0
        except (ValueError, TypeError):
            tp1_pct = sl_pct = 0

        cases.append(
            f"{r.get('symbol','?')} {r.get('direction','?')} [{r.get('setup','?')}] "
            f"score={r.get('score','?')} "
            f"chg_24h={r.get('change_24h_pct','?')}% mfe={r.get('mfe_24h_pct','?')}% "
            f"TP1_dist={tp1_pct:.1f}% SL_dist={sl_pct:.1f}% "
            f"funding={r.get('funding','?')}% OI={r.get('oi_24h_pct','?')}%"
        )

    user_prompt = (
        f"Вот {len(missed)} сигналов с исходом FLAT, но ценой >3% в нужную сторону за 24h:\n\n"
        + "\n".join(cases)
        + "\n\nПочему TP1 не был достигнут? Какие паттерны ты видишь? "
          "Что конкретно изменить в расчёте TP1/SL чтобы захватывать эти движения?"
    )

    try:
        client = get_client(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=_MISSED_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        analysis = msg.content[0].text
        print(f"[MissedMoves] Анализ получен ({len(missed)} случаев)")
        return f"## Анализ пропущенных движений ({len(missed)} FLAT → >3%)\n\n{analysis}"
    except Exception as e:
        print(f"[MissedMoves] Ошибка API: {e}")
        return ""


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Claude AI анализ статистики бота")
    parser.add_argument("--n",   type=int, default=500,
                        help="Последние N сделок для анализа (default: 500)")
    parser.add_argument("--tg",  action="store_true",
                        help="Отправить отчёт в Telegram")
    parser.add_argument("--save", action="store_true", default=True,
                        help="Сохранить отчёт в outcomes/claude_report.md")
    parser.add_argument("--no-code-prompt", action="store_true",
                        help="Не генерировать Claude Code промпт")
    parser.add_argument("--no-missed",      action="store_true",
                        help="Не анализировать пропущенные движения")
    args = parser.parse_args()

    print(f"[1/6] Загружаю последние {args.n} сделок из resolved.csv...")
    rows  = load_resolved(args.n)
    print(f"      Загружено {len(rows)} строк")

    print("[2/6] Считаю статистику...")
    stats = build_stats(rows)
    print(f"      Общий WR: {stats['overall']['wr']}% "
          f"(n={stats['overall']['total']})")

    print("[3/6] Формирую промпт и спрашиваю Claude (Sonnet)...")
    prompt = format_stats_for_prompt(stats, len(rows))
    report = ask_claude(prompt)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    full_report = (f"# Claude Analyst Report\n"
                   f"*Дата: {ts} | Сделок: {len(rows)}*\n\n"
                   f"{report}")

    print("\n" + "=" * 60)
    print(full_report)
    print("=" * 60)

    if args.save:
        REPORT_PATH.write_text(full_report, encoding="utf-8")
        print(f"\n[4/6] Отчёт сохранён: {REPORT_PATH}")

    if args.tg:
        print("[4/6] Отправляю основной отчёт в Telegram...")
        send_to_telegram(full_report)

    # ── Функция 1: Claude Code промпт ─────────────────────────────────────────
    if not args.no_code_prompt:
        print("[5/6] Генерирую Claude Code промпт...")
        code_prompt = generate_code_prompt(report)
        if code_prompt and args.tg:
            send_to_telegram(
                f"🛠 *Claude Code Prompt*\n\nСохранён в `outcomes/code_prompt.md`\n\n"
                f"```\n{code_prompt[:3500]}\n```"
            )
    else:
        print("[5/6] --no-code-prompt: пропуск")

    # ── Функция 3: анализ пропущенных движений ────────────────────────────────
    if not args.no_missed:
        print("[6/6] Анализирую пропущенные движения (FLAT >3%)...")
        missed_report = analyze_missed_moves(rows)
        if missed_report:
            print("\n" + missed_report)
            if args.save:
                missed_path = BASE_DIR / "outcomes" / "missed_moves_report.md"
                existing = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
                REPORT_PATH.write_text(existing + "\n\n---\n\n" + missed_report, encoding="utf-8")
                print(f"[6/6] Пропущенные движения дописаны в: {REPORT_PATH}")
            if args.tg:
                send_to_telegram(f"🔍 *Пропущенные движения*\n\n{missed_report}")
    else:
        print("[6/6] --no-missed: пропуск")


if __name__ == "__main__":
    main()
