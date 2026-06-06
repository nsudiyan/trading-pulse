"""
claude_prompt_ab_test.py — OFFLINE replay: v1 vs v2 system prompt.

НЕ live. Прогоняет оба промпта на сохранённых shadow-контекстах с ИДЕНТИЧНЫМ
входом (контекст морозим один раз, зовём Claude дважды). temperature=0 →
детерминированно. Цель — СКРИНИНГ, не доказательство:

  1. Размазывает ли формула confidence v2 лучше мушки v1?
  2. Различает ли confidence победителей от проигравших (главный вопрос)?
  3. % согласия v1/v2 (sanity) + распределение verdict'ов.
  4. GO precision на ~10 GO с известным исходом (слабый сигнал, n мал).

Usage:  python3 claude_prompt_ab_test.py --limit 60
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import claude_realtime_filter as crf

BASE = Path(__file__).parent
SHADOW = BASE / "outcomes" / "shadow_verdicts.jsonl"
CACHE = BASE / "outcomes" / "shadow_outcomes_cache.json"
V2_DRAFT = BASE / "claude_realtime_filter_v2_prompt.draft.txt"


def _load_v2_prompt() -> str:
    txt = V2_DRAFT.read_text(encoding="utf-8")
    marker = 'SYSTEM_PROMPT_V2 = """'
    after = txt.split(marker, 1)[1]
    return after.split('"""', 1)[0]


def _call_with_prompt(context: str, system_prompt: str) -> dict | None:
    """Копия парсинга _call_claude, но с произвольным system-промптом."""
    import os
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    if crf._ANTHROPIC_CLIENT is None:
        crf._ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=api_key, timeout=crf.TIMEOUT_SEC)
    client = crf._ANTHROPIC_CLIENT
    try:
        msg = client.messages.create(
            model=crf.MODEL,
            max_tokens=crf.MAX_TOKENS,
            temperature=crf.TEMPERATURE,
            system=system_prompt,
            messages=[{"role": "user", "content": context}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        result = json.loads(text)
        if result.get("verdict") not in ("GO", "SKIP", "WAIT"):
            return None
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0.5)))
        return result
    except Exception as e:
        print(f"    [err] {type(e).__name__}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60, help="макс. кандидатов (контроль расхода)")
    args = ap.parse_args()

    crf._load_dotenv()
    v1_prompt = crf.SYSTEM_PROMPT
    v2_prompt = _load_v2_prompt()
    print(f"v1 prompt: {len(v1_prompt)} chars | v2 prompt: {len(v2_prompt)} chars\n")

    verdicts = [json.loads(l) for l in open(SHADOW, encoding="utf-8")]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    verdicts = verdicts[-args.limit:]
    print(f"Прогон {len(verdicts)} кандидатов × 2 промпта = {len(verdicts)*2} вызовов\n")

    rows = []
    for i, v in enumerate(verdicts):
        sym = v["symbol"]
        cand = v.get("candidate") or {}
        cand.setdefault("direction", v.get("direction", "LONG"))
        cand.setdefault("setup", v.get("setup", "?"))
        ctx = crf.build_context(sym, cand, v.get("source", "screener"))  # морозим
        r1 = _call_with_prompt(ctx, v1_prompt)
        r2 = _call_with_prompt(ctx, v2_prompt)
        if not r1 or not r2:
            print(f"  [{i+1}/{len(verdicts)}] {sym}: пропуск (r1={bool(r1)} r2={bool(r2)})")
            continue
        oc = cache.get(f"{sym}_{v['ts']}", {})
        rows.append({
            "sym": sym, "o1": oc.get("o1", ""), "o4": oc.get("o4", ""),
            "v1": r1["verdict"], "c1": r1["confidence"],
            "v2": r2["verdict"], "c2": r2["confidence"],
        })
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(verdicts)}] обработано...")
        time.sleep(0.2)

    if not rows:
        print("Нет данных.")
        return

    n = len(rows)
    print(f"\n{'='*60}\nРЕЗУЛЬТАТ ({n} кандидатов)\n{'='*60}")

    # 1. Распределение verdict'ов
    def dist(arm):
        return {x: sum(1 for r in rows if r[arm] == x) for x in ("GO", "SKIP", "WAIT")}
    print(f"\nVerdict distribution:")
    print(f"  v1: {dist('v1')}")
    print(f"  v2: {dist('v2')}")

    # 2. Согласие
    agree = sum(1 for r in rows if r["v1"] == r["v2"])
    print(f"\nСогласие v1==v2: {agree}/{n} = {agree/n*100:.0f}%")

    # 3. Разброс confidence (главное: размазана ли формула v2 лучше мушки v1)
    c1 = [r["c1"] for r in rows]
    c2 = [r["c2"] for r in rows]
    print(f"\nConfidence spread:")
    print(f"  v1: min={min(c1):.2f} max={max(c1):.2f} std={statistics.pstdev(c1):.3f} distinct={len(set(c1))}")
    print(f"  v2: min={min(c2):.2f} max={max(c2):.2f} std={statistics.pstdev(c2):.3f} distinct={len(set(c2))}")

    # 4. ГЛАВНОЕ: различает ли confidence WIN от LOSS среди GO (1h)?
    print(f"\nРазличает ли confidence WIN/LOSS среди GO (1h, n мал — осторожно):")
    for arm, cf in [("v1", "c1"), ("v2", "c2")]:
        go_win = [r[cf] for r in rows if r[arm] == "GO" and r["o1"] == "WIN"]
        go_loss = [r[cf] for r in rows if r[arm] == "GO" and r["o1"] == "LOSS"]
        mw = statistics.mean(go_win) if go_win else float("nan")
        ml = statistics.mean(go_loss) if go_loss else float("nan")
        sep = mw - ml if (go_win and go_loss) else float("nan")
        print(f"  {arm}: conf(WIN)={mw:.2f} (n={len(go_win)})  conf(LOSS)={ml:.2f} (n={len(go_loss)})  "
              f"separation={sep:+.2f}")

    # 5. GO precision на известных исходах (слабый сигнал)
    print(f"\nGO precision (1h decisive, n мал):")
    for arm in ("v1", "v2"):
        go_dec = [r for r in rows if r[arm] == "GO" and r["o1"] in ("WIN", "LOSS")]
        win = sum(1 for r in go_dec if r["o1"] == "WIN")
        prec = win / len(go_dec) * 100 if go_dec else 0
        print(f"  {arm}: {win}/{len(go_dec)} = {prec:.0f}%")

    # 6. Дамп disagreement-кейсов (где интереснее всего)
    print(f"\nDisagreement cases (v1 != v2):")
    for r in rows:
        if r["v1"] != r["v2"]:
            print(f"  {r['sym']:12s} o1={r['o1']:5s} | v1={r['v1']}({r['c1']:.2f}) v2={r['v2']}({r['c2']:.2f})")


if __name__ == "__main__":
    main()
