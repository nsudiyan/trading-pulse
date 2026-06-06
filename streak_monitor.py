"""
streak_monitor.py — Losing streak detection and Audit Mode (AVEVA-50).

Monitors resolved trades for losing streaks and drawdown breaches.
When triggered, activates Audit Mode which suspends signal generation
in screener.py until manually reviewed and cleared.

Trigger conditions:
  - 3+ consecutive losing trades (STOP / LOSS outcome)
  - Cumulative R-multiple ≤ -6R across the last 10 resolved trades

Audit Mode:
  - Blocks run_screener() from generating signals
  - Runs RCA common-denominator analysis on the losing streak
  - Logs a critical event to knowledge_base.md
  - Sends a Telegram alert

Exit:
  python3 streak_monitor.py --exit            # manual clearance after review
  python3 streak_monitor.py --status          # show current state
  python3 streak_monitor.py --check           # run check against latest data
  python3 streak_monitor.py --analyze N       # analyze last N resolved trades
"""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── paths ────────────────────────────────────────────────────────────────────

BASE_DIR       = Path(__file__).parent
OUTCOMES_DIR   = BASE_DIR / "outcomes"
RESOLVED_CSV   = OUTCOMES_DIR / "resolved.csv"
RCA_PATH       = OUTCOMES_DIR / "rca_results.json"
STATE_PATH     = OUTCOMES_DIR / "audit_mode.json"
KNOWLEDGE_PATH = BASE_DIR / "knowledge_base.md"

# ─── thresholds ───────────────────────────────────────────────────────────────

CONSECUTIVE_LOSS_THRESHOLD = 3     # consecutive STOP/LOSS to trigger
DRAWDOWN_R_THRESHOLD       = -6.0  # cumulative R in last DRAWDOWN_WINDOW trades
DRAWDOWN_WINDOW            = 10    # trades to look back for drawdown
AUTO_UNLOCK_HOURS          = 4     # hours before Audit Mode auto-expires

LOSS_OUTCOMES = {"STOP", "LOSS"}

# ─── state I/O ────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "active":           False,
        "activated_at":     None,
        "exit_at":          None,
        "trigger_reason":   None,
        "streak_count":     0,
        "drawdown_r":       0.0,
        "analyzed_trades":  [],
        "common_tags":      [],
        "primary_causes":   [],
        "restrictions":     [],
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()


def _save_state(state: dict):
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _is_expired(state: dict) -> bool:
    """True if active Audit Mode has exceeded AUTO_UNLOCK_HOURS since activation."""
    if not state.get("active"):
        return False
    activated_at = state.get("activated_at", "")
    if not activated_at:
        return False
    try:
        activated_dt = datetime.strptime(activated_at[:19], "%Y-%m-%dT%H:%M:%S")
        return (datetime.utcnow() - activated_dt).total_seconds() >= AUTO_UNLOCK_HOURS * 3600
    except Exception:
        return False


# ─── screener gate ────────────────────────────────────────────────────────────

def is_audit_mode() -> bool:
    """Used by screener.py at startup. Auto-deactivates silently if AUTO_UNLOCK_HOURS elapsed."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not state.get("active"):
            return False
        if _is_expired(state):
            deactivate(reason="auto_unlock", silent=True)
            return False
        return True
    except Exception:
        return False


def get_audit_state() -> dict:
    return load_state()


# ─── data reading ─────────────────────────────────────────────────────────────

def _read_recent_resolved(n: int = 30) -> list:
    """Read the last N rows from resolved.csv, newest first."""
    if not RESOLVED_CSV.exists():
        return []
    rows = []
    with open(RESOLVED_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-n:][::-1]  # last n rows, newest first


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ─── streak detection ─────────────────────────────────────────────────────────

def _detect_streak(trades: list) -> tuple[int, list]:
    """
    Walk from newest to oldest and count consecutive losses.
    Returns (streak_length, [losing_trade_rows]).
    """
    losing = []
    for trade in trades:
        outcome = trade.get("outcome_24h", "") or trade.get("outcome_4h", "")
        if outcome in LOSS_OUTCOMES:
            losing.append(trade)
        else:
            break
    return len(losing), losing


# ─── drawdown detection ───────────────────────────────────────────────────────

def _detect_drawdown(trades: list) -> float:
    """
    Sum R-multiples for the most recent DRAWDOWN_WINDOW trades.
    Uses r_multiple_24h if available, else r_multiple_4h.
    Returns cumulative R (negative = drawdown).
    """
    window = trades[:DRAWDOWN_WINDOW]
    total_r = 0.0
    for t in window:
        r = t.get("r_multiple_24h") or t.get("r_multiple_4h")
        if r not in (None, "", "None"):
            total_r += _f(r)
    return round(total_r, 3)


# ─── common denominator analysis ─────────────────────────────────────────────

def _analyze_common_denominators(losing_trades: list) -> dict:
    """
    Run lightweight RCA tag analysis on the losing streak.
    Falls back to raw field inspection if rca_engine is unavailable.
    """
    try:
        from rca_engine import analyze_trade
        rca_available = True
    except ImportError:
        rca_available = False

    all_tags: list[str] = []
    primary_causes: list[str] = []
    setups: list[str] = []
    utc_hours: list[int] = []
    symbols: list[str] = []

    for t in losing_trades:
        symbols.append(t.get("symbol", "?"))
        setups.append(t.get("setup", "?"))
        try:
            ts = t.get("run_ts", "")
            if ts:
                utc_hours.append(datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").hour)
        except Exception:
            pass

        if rca_available:
            try:
                horizon = "24h" if t.get("outcome_24h") else "4h"
                rca = analyze_trade(t, horizon)
                all_tags.extend(rca.get("tags", []))
                cause = rca.get("primary_cause", "")
                if cause:
                    primary_causes.append(cause)
            except Exception:
                pass

    tag_counts = Counter(all_tags)
    setup_counts = Counter(setups)
    hour_counts = Counter(utc_hours)

    restrictions = _derive_restrictions(tag_counts, setup_counts, hour_counts)

    return {
        "symbols":       symbols,
        "setups":        setup_counts.most_common(),
        "top_tags":      tag_counts.most_common(10),
        "primary_causes": primary_causes,
        "utc_hours":     hour_counts.most_common(),
        "restrictions":  restrictions,
    }


def _derive_restrictions(
    tag_counts: Counter,
    setup_counts: Counter,
    hour_counts: Counter,
) -> list[str]:
    """Generate human-readable restriction proposals from the analysis."""
    restrictions = []

    dominant_tags = [t for t, n in tag_counts.most_common(5) if n >= 2]
    for tag in dominant_tags:
        if tag == "CROWDED_LONG":
            restrictions.append("Avoid longs when funding > 0.05% (crowded long)")
        elif tag == "OVERBOUGHT_ENTRY":
            restrictions.append("Skip entries when RSI 1H > 70")
        elif tag == "MTF_DIVERGENCE":
            restrictions.append("Require MTF alignment: bull AND bear must not both be ≥3")
        elif tag == "CVD_DIVERGENCE":
            restrictions.append("Require CVD kline and trade-flow to agree before entry")
        elif tag == "CHOCH_MISSING":
            restrictions.append("Require CHoCH bull confirmation on 1H for long setups")
        elif tag == "OI_SURGE":
            restrictions.append("Avoid entries when OI 24h > +20% (leverage overhang)")
        elif tag == "EMA_ABSENT":
            restrictions.append("Require at least 1H EMA bull alignment")
        elif tag == "WEAK_RS_BTC":
            restrictions.append("Skip longs where RS vs BTC < -10pp")
        elif tag == "SCORE_ANTICORRELATED_SETUP":
            restrictions.append("Reduce size on breakout/range_sweep with score > 120")

    dominant_setup = setup_counts.most_common(1)
    if dominant_setup and dominant_setup[0][1] >= 2:
        s = dominant_setup[0][0]
        if s == "breakout":
            restrictions.append(f"Temporarily suspend '{s}' setups until streak clears")
        elif s == "range_sweep":
            restrictions.append(f"Temporarily suspend '{s}' setups until streak clears")

    if hour_counts:
        bad_hours = [h for h, n in hour_counts.most_common() if n >= 2]
        if bad_hours:
            restrictions.append(
                f"Avoid entries at UTC {', '.join(str(h) for h in bad_hours[:3])}h"
                f" — repeated losses at these hours in current streak"
            )

    if not restrictions:
        restrictions.append("Reduce position size to 50% until audit review is complete")

    return restrictions


# ─── knowledge base logging ───────────────────────────────────────────────────

_AUDIT_MARKER = "## 🚨 AUDIT MODE"
_AUDIT_END    = "\n---\n"


def _log_to_knowledge_base(state: dict, analysis: dict):
    """Prepend/replace the AUDIT MODE section in knowledge_base.md."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    streak = state.get("streak_count", 0)
    drawdown = state.get("drawdown_r", 0.0)
    trigger = state.get("trigger_reason", "unknown")
    activated = (state.get("activated_at") or now)[:19]

    lines = [
        f"{_AUDIT_MARKER}",
        f"> **КРИТИЧЕСКОЕ СОБЫТИЕ** | Активирован: {activated} UTC | Причина: {trigger}",
        "",
        f"### Триггер",
    ]

    if trigger == "streak":
        lines.append(f"- **{streak} последовательных убыточных сделок** (порог: {CONSECUTIVE_LOSS_THRESHOLD})")
    elif trigger == "drawdown":
        lines.append(f"- **Просадка {drawdown:.1f}R** за последние {DRAWDOWN_WINDOW} сделок (порог: {DRAWDOWN_R_THRESHOLD}R)")
    elif trigger == "both":
        lines.append(f"- **{streak} подряд убытков** + просадка **{drawdown:.1f}R** за {DRAWDOWN_WINDOW} сделок")

    syms = analysis.get("symbols", [])
    if syms:
        lines.append(f"- Убыточные пары: {', '.join(syms)}")

    lines.append("")
    lines.append("### Общие знаменатели (RCA)")

    top_tags = analysis.get("top_tags", [])
    if top_tags:
        for tag, n in top_tags[:8]:
            lines.append(f"- `{tag}` — встречается в {n}/{streak} сделках")
    else:
        lines.append("- RCA данных недостаточно (нужно больше resolved trades)")

    setups = analysis.get("setups", [])
    if setups:
        lines.append(f"\nСетапы в серии: " + ", ".join(f"{s}(×{n})" for s, n in setups))

    causes = analysis.get("primary_causes", [])
    if causes:
        lines.append("\n### Основные причины (primary_cause)")
        seen_causes = []
        for c in causes:
            if c not in seen_causes:
                lines.append(f"- {c}")
                seen_causes.append(c)

    restrictions = analysis.get("restrictions", [])
    if restrictions:
        lines.append("")
        lines.append("### Временные ограничения (до выхода из аудита)")
        for r in restrictions:
            lines.append(f"- {r}")

    lines += [
        "",
        "### Как выйти из Audit Mode",
        "1. Прочитай анализ выше",
        "2. Применяй ограничения к следующим сделкам",
        "3. Выполни: `python3 streak_monitor.py --exit`",
        "",
        f"*Последнее обновление: {now}*",
        "",
    ]

    new_section = "\n".join(lines) + _AUDIT_END

    if KNOWLEDGE_PATH.exists():
        content = KNOWLEDGE_PATH.read_text(encoding="utf-8")
    else:
        content = "# Trading Knowledge Base\n\n"

    if _AUDIT_MARKER in content:
        start = content.index(_AUDIT_MARKER)
        end_marker = content.find(_AUDIT_END, start)
        if end_marker != -1:
            content = content[:start] + new_section + content[end_marker + len(_AUDIT_END):]
        else:
            content = content[:start] + new_section
    else:
        content = new_section + "\n" + content

    KNOWLEDGE_PATH.write_text(content, encoding="utf-8")


def _remove_audit_from_knowledge_base():
    """Remove the AUDIT MODE section from knowledge_base.md on exit."""
    if not KNOWLEDGE_PATH.exists():
        return
    content = KNOWLEDGE_PATH.read_text(encoding="utf-8")
    if _AUDIT_MARKER not in content:
        return
    start = content.index(_AUDIT_MARKER)
    end_marker = content.find(_AUDIT_END, start)
    if end_marker != -1:
        content = content[:start] + content[end_marker + len(_AUDIT_END):]
    else:
        content = content[:start]
    KNOWLEDGE_PATH.write_text(content.lstrip("\n"), encoding="utf-8")


# ─── telegram alert ───────────────────────────────────────────────────────────

def _send_telegram_alert(state: dict, analysis: dict):
    """Send an Audit Mode activation/exit alert via telegram_alerts if available."""
    try:
        import telegram_alerts as _tg
        import json as _json
        from pathlib import Path as _Path

        cfg_path = _Path(__file__).parent / "telegram_config.json"
        if not cfg_path.exists():
            return
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        if not cfg.get("enabled") or not cfg.get("bot_token"):
            return

        token   = cfg["bot_token"]
        chat_id = str(cfg["chat_id"])

        streak  = state.get("streak_count", 0)
        drawdown = state.get("drawdown_r", 0.0)
        trigger = state.get("trigger_reason", "unknown")
        syms    = analysis.get("symbols", [])
        restr   = analysis.get("restrictions", [])

        if state.get("active"):
            lines = [
                "🚨 <b>AUDIT MODE АКТИВИРОВАН</b> 🚨",
                "",
                f"Причина: <b>{trigger}</b>",
            ]
            if trigger == "streak":
                lines.append(f"  {streak} подряд убыточных сделок")
            elif trigger == "drawdown":
                lines.append(f"  Просадка {drawdown:.1f}R за {DRAWDOWN_WINDOW} сделок")
            elif trigger == "both":
                lines.append(f"  {streak} подряд убытков + просадка {drawdown:.1f}R")
            if syms:
                lines.append(f"\nПары в серии: {', '.join(syms[:6])}")
            if restr:
                lines.append("\n<b>Временные ограничения:</b>")
                for r in restr[:4]:
                    lines.append(f"• {r}")
            lines += [
                "",
                "⛔ Screener заблокирован до выхода из аудита.",
                "Выполни: <code>python3 streak_monitor.py --exit</code>",
            ]
        else:
            lines = [
                "✅ <b>Audit Mode снят</b>",
                f"  Аудит завершён: {state.get('exit_at', '')[:19]} UTC",
                "  Screener разблокирован.",
            ]

        msg = "\n".join(lines)
        import requests as _req
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     msg,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception:
        pass


# ─── activation / deactivation ────────────────────────────────────────────────

def check_and_activate(silent: bool = False) -> bool:
    """
    Read latest resolved trades and activate Audit Mode if thresholds are breached.

    Returns True if Audit Mode was newly activated, False otherwise.
    Already-active state is left unchanged.
    """
    state = load_state()
    if state.get("active"):
        if _is_expired(state):
            deactivate(reason="auto_unlock")
        return False  # already in audit mode (or just auto-unlocked), caller handles

    trades = _read_recent_resolved(max(DRAWDOWN_WINDOW + 5, 20))
    if not trades:
        return False

    streak_count, losing_trades = _detect_streak(trades)
    drawdown_r = _detect_drawdown(trades)

    streak_triggered   = streak_count >= CONSECUTIVE_LOSS_THRESHOLD
    drawdown_triggered = drawdown_r   <= DRAWDOWN_R_THRESHOLD

    if not streak_triggered and not drawdown_triggered:
        return False

    if streak_triggered and drawdown_triggered:
        trigger_reason = "both"
    elif streak_triggered:
        trigger_reason = "streak"
    else:
        trigger_reason = "drawdown"

    analysis = _analyze_common_denominators(losing_trades if losing_trades else trades[:DRAWDOWN_WINDOW])

    state = {
        "active":          True,
        "activated_at":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exit_at":         None,
        "trigger_reason":  trigger_reason,
        "streak_count":    streak_count,
        "drawdown_r":      drawdown_r,
        "analyzed_trades": analysis.get("symbols", []),
        "common_tags":     [t for t, _ in analysis.get("top_tags", [])[:8]],
        "primary_causes":  analysis.get("primary_causes", []),
        "restrictions":    analysis.get("restrictions", []),
    }
    _save_state(state)
    _log_to_knowledge_base(state, analysis)
    _send_telegram_alert(state, analysis)

    if not silent:
        _print_activation(state, analysis)

    return True


def deactivate(reason: str = "manual_exit", silent: bool = False) -> bool:
    """
    Exit Audit Mode. Removes the KB section and sends a clearance alert.
    Returns False if not currently active.
    silent=True suppresses stdout (used by is_audit_mode() called from screener).
    """
    state = load_state()
    if not state.get("active"):
        if not silent:
            print("Audit Mode is not currently active.")
        return False

    state["active"]      = False
    state["exit_at"]     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    state["exit_reason"] = reason
    _save_state(state)
    _remove_audit_from_knowledge_base()
    _send_telegram_alert(state, {})

    if not silent:
        label = f" (авто, через {AUTO_UNLOCK_HOURS}ч)" if reason == "auto_unlock" else ""
        print(f"✅ Audit Mode деактивирован{label}. Screener разблокирован.")
    return True


# ─── display ──────────────────────────────────────────────────────────────────

def _print_activation(state: dict, analysis: dict):
    print("\n" + "=" * 72)
    print("  🚨 AUDIT MODE АКТИВИРОВАН")
    print("=" * 72)
    print(f"  Причина      : {state['trigger_reason']}")
    print(f"  Сделок подряд: {state['streak_count']}  |  Просадка R: {state['drawdown_r']:.2f}")
    print(f"  Пары в серии : {', '.join(state['analyzed_trades'])}")
    if state["common_tags"]:
        print(f"  Топ теги     : {', '.join(state['common_tags'][:5])}")
    print("\n  Временные ограничения:")
    for r in state.get("restrictions", []):
        print(f"    • {r}")
    print("\n  Screener заблокирован. Для выхода:")
    print("    python3 streak_monitor.py --exit")
    print("=" * 72 + "\n")


def _print_status():
    state = load_state()
    print("\n" + "=" * 60)
    print("  STREAK MONITOR — STATUS")
    print("=" * 60)
    if state.get("active"):
        print(f"  ⛔ AUDIT MODE АКТИВЕН")
        print(f"  Активирован : {state.get('activated_at', '')[:19]} UTC")
        try:
            activated_dt = datetime.strptime(state.get("activated_at", "")[:19], "%Y-%m-%dT%H:%M:%S")
            elapsed_s = (datetime.utcnow() - activated_dt).total_seconds()
            remaining_s = AUTO_UNLOCK_HOURS * 3600 - elapsed_s
            if remaining_s > 0:
                rh, rm = divmod(int(remaining_s), 3600)
                rm //= 60
                print(f"  Авто-снятие : через {rh}ч {rm}м  (лимит: {AUTO_UNLOCK_HOURS}ч)")
            else:
                print(f"  Авто-снятие : ⏰ срок истёк — снимется при следующем запуске screener или --check")
        except Exception:
            pass
        print(f"  Причина     : {state.get('trigger_reason')}")
        print(f"  Серия убытков: {state.get('streak_count')}")
        print(f"  Просадка R  : {state.get('drawdown_r', 0):.2f}")
        print(f"  Символы     : {', '.join(state.get('analyzed_trades', []))}")
        print(f"\n  Топ теги    : {', '.join(state.get('common_tags', [])[:5])}")
        print("\n  Ограничения:")
        for r in state.get("restrictions", []):
            print(f"    • {r}")
        print("\n  Для выхода: python3 streak_monitor.py --exit")
    else:
        prev_exit = state.get("exit_at")
        if prev_exit:
            print(f"  ✅ Audit Mode не активен  (последний выход: {prev_exit[:19]} UTC)")
        else:
            print("  ✅ Audit Mode не активен")

        # Show live streak/drawdown stats
        trades = _read_recent_resolved(20)
        if trades:
            streak_count, _ = _detect_streak(trades)
            drawdown_r = _detect_drawdown(trades)
            print(f"\n  Текущая серия убытков: {streak_count} (порог: {CONSECUTIVE_LOSS_THRESHOLD})")
            print(f"  Просадка за {DRAWDOWN_WINDOW} сделок: {drawdown_r:.2f}R (порог: {DRAWDOWN_R_THRESHOLD}R)")
            margin_streak   = CONSECUTIVE_LOSS_THRESHOLD - streak_count
            margin_drawdown = abs(drawdown_r - DRAWDOWN_R_THRESHOLD)
            if margin_streak <= 1 and streak_count > 0:
                print(f"  ⚠️  Ещё 1 убыток → триггер по серии!")
            if drawdown_r < -3:
                print(f"  ⚠️  Просадка нарастает: {drawdown_r:.2f}R (до триггера: {margin_drawdown:.2f}R)")
    print("=" * 60 + "\n")


def _print_analyze(n: int = 20):
    """Print streak/drawdown analysis for the last N resolved trades."""
    trades = _read_recent_resolved(n)
    if not trades:
        print("Нет resolved trades для анализа.")
        return

    streak_count, losing = _detect_streak(trades)
    drawdown_r = _detect_drawdown(trades)

    print(f"\n  Последние {len(trades)} сделок (newest first)")
    print(f"  Текущая серия убытков : {streak_count}")
    print(f"  Просадка ({DRAWDOWN_WINDOW} сделок)  : {drawdown_r:.2f}R\n")

    print(f"  {'Символ':<14} {'Сетап':<12} {'Исход 24h':<10} {'R':<8} {'Время'}")
    print("  " + "─" * 60)
    for t in trades[:15]:
        outcome = t.get("outcome_24h", "") or t.get("outcome_4h", "—")
        r_val = t.get("r_multiple_24h") or t.get("r_multiple_4h") or "—"
        ts = (t.get("run_ts") or "")[:16]
        print(f"  {t.get('symbol','?'):<14} {t.get('setup','?'):<12} {outcome:<10} {str(r_val):<8} {ts}")

    if losing:
        print(f"\n  Анализ серии ({len(losing)} убытков):")
        analysis = _analyze_common_denominators(losing)
        top_tags = analysis.get("top_tags", [])
        if top_tags:
            print("  Топ теги:")
            for tag, n_count in top_tags[:8]:
                print(f"    {tag} (×{n_count})")
        for r in analysis.get("restrictions", []):
            print(f"  • {r}")
    print()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Streak monitor and Audit Mode manager (AVEVA-50)"
    )
    ap.add_argument("--status",  action="store_true", help="Show current audit mode status")
    ap.add_argument("--check",   action="store_true", help="Run check against latest resolved trades")
    ap.add_argument("--exit",    action="store_true", help="Manually exit Audit Mode after review")
    ap.add_argument("--analyze", type=int, metavar="N", nargs="?", const=20,
                    help="Analyze last N resolved trades (default: 20)")
    args = ap.parse_args()

    if args.exit:
        deactivate(reason="manual_exit")
        return

    if args.check:
        print("Проверяю последние сделки…")
        activated = check_and_activate(silent=False)
        if not activated:
            _print_status()
        return

    if args.analyze is not None:
        _print_analyze(args.analyze)
        return

    # Default: status
    _print_status()


if __name__ == "__main__":
    main()
