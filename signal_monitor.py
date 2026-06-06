"""
signal_monitor.py — real-time signal daemon for Bybit screener.

Два режима работы:
  1. Ценовая проверка (каждые 3 мин): читает тикеры, проверяет пробой стопа активных сигналов.
  2. Полный пересканирование (каждые 10 мин): запускает run_screener(_return_candidates=True),
     находит новые сигналы, проверяет смену направления, отправляет индивидуальные TG-алерты.

Запуск:
    python3 signal_monitor.py
    python3 signal_monitor.py >> monitor.log 2>&1 &   # фоновый режим

active_signals.json — хранит текущие активные сигналы (ключ: symbol).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Config ───────────────────────────────────────────────────────────────────
PRICE_CHECK_SEC    = 180     # 3 мин: только цены → проверка стопов
FULL_SCAN_SEC      = 600     # 10 мин: полный scoring → новые сигналы
SIGNAL_TTL_HOURS   = 12      # сигнал автоматически истекает через 12ч
SCORE_COLLAPSE     = 0.60    # отменить если score упал ниже 60% исходного
ACTIVE_FILE        = Path(__file__).parent / "active_signals.json"
DYNAMIC_BL_PATH    = Path(__file__).parent / "dynamic_blacklist.json"
LOG_PREFIX         = "[Monitor]"

# Авто-черный список: N последовательных LOSS → бан на BAN_DAYS дней
BL_CONSECUTIVE_LOSS = 3
BL_BAN_DAYS         = 7


# ── Active signals store ──────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        return json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(signals: list[dict]):
    ACTIVE_FILE.write_text(
        json.dumps(signals, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{LOG_PREFIX} [{ts}] {msg}", flush=True)


# ── Функция 2: Claude Haiku оценка сигнала ───────────────────────────────────

def _claude_haiku_eval(r: dict) -> str:
    """
    Быстрая оценка качества сигнала через Claude Haiku (1 предложение).
    Возвращает строку для добавления в конец TG сообщения, или '' при ошибке.
    Не блокирует основной цикл — все исключения подавляются.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""

    try:
        import requests as _req

        symbol    = r.get("symbol", "?")
        direction = r.get("direction", r.get("side", "?"))
        setup     = r.get("best_setup", r.get("setup", "?"))
        score     = r.get("score", 0)
        fund      = r.get("fund_%", r.get("funding", 0)) or 0
        oi_chg    = r.get("oi_24h_pct", r.get("oi_chg", 0)) or 0
        cvd_kl    = r.get("cvd_kline", r.get("cvd_%", 0)) or 0
        d_htf     = r.get("d_htf", "—")
        h4_htf    = r.get("h4_htf", "—")
        choch     = r.get("choch_1h", "—")
        atr_pct   = r.get("atr_%", r.get("atr", 0)) or 0
        grade     = r.get("grade", "—")

        user_msg = (
            f"Торговый сигнал: {symbol} {str(direction).upper()} [{setup}]\n"
            f"Score={score} Grade={grade} | Funding={fund:.3f}% | OI Δ={oi_chg:+.1f}% | CVD={cvd_kl:+.1f}%\n"
            f"HTF: D={d_htf} / 4H={h4_htf} | CHoCH={choch} | ATR={atr_pct:.2f}%\n\n"
            "Оцени качество этого сигнала ОДНИМ предложением на русском. "
            "Укажи главный риск или главное преимущество."
        )

        resp = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 120,
                "messages":   [{"role": "user", "content": user_msg}],
            },
            timeout=8,
        )
        text = resp.json().get("content", [{}])[0].get("text", "").strip()
        if text:
            return f"\n🤖 AI: {text}"
    except Exception as e:
        _log(f"[HaikuEval] ошибка: {e}")

    return ""


# ── Функция 4: Авто-черный список ─────────────────────────────────────────────

def _load_dynamic_blacklist() -> dict:
    """Загружает {symbol: expiry_unix} и удаляет истёкшие записи."""
    try:
        raw = json.loads(DYNAMIC_BL_PATH.read_text(encoding="utf-8"))
        now = time.time()
        active = {sym: exp for sym, exp in raw.items() if exp > now}
        if len(active) != len(raw):
            DYNAMIC_BL_PATH.write_text(
                json.dumps(active, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return active
    except Exception:
        return {}


def _check_consecutive_losses(tg_cfg: dict):
    """
    Сканирует resolved.csv. Если символ имеет BL_CONSECUTIVE_LOSS подряд
    LOSS/STOP → добавляет в dynamic_blacklist.json на BL_BAN_DAYS дней
    и патчит screener.SYMBOL_BLACKLIST.
    """
    import csv as _csv

    resolved_path = Path(__file__).parent / "outcomes" / "resolved.csv"
    if not resolved_path.exists():
        return

    # Читаем все строки, группируем по символу
    by_sym: dict[str, list[dict]] = {}
    try:
        with open(resolved_path, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                sym = row.get("symbol", "")
                if sym:
                    by_sym.setdefault(sym, []).append(row)
    except Exception as e:
        _log(f"[AutoBL] Ошибка чтения CSV: {e}")
        return

    loss_outcomes = {"STOP", "LOSS"}
    current_bl    = _load_dynamic_blacklist()
    now           = time.time()
    newly_banned: list[str] = []

    for sym, rows in by_sym.items():
        if sym in current_bl:
            continue  # уже в списке

        # Сортируем по run_ts, берём последние BL_CONSECUTIVE_LOSS
        try:
            rows.sort(key=lambda x: x.get("run_ts", ""))
        except Exception:
            pass

        recent = rows[-BL_CONSECUTIVE_LOSS:]
        if len(recent) < BL_CONSECUTIVE_LOSS:
            continue

        if all(r.get("outcome_24h", "").upper() in loss_outcomes for r in recent):
            expiry = now + BL_BAN_DAYS * 86400
            current_bl[sym] = expiry
            newly_banned.append(sym)
            _log(f"[AutoBL] {sym} → БANLISTED на {BL_BAN_DAYS} дней "
                 f"({BL_CONSECUTIVE_LOSS} LOSS подряд)")

    if not newly_banned:
        return

    # Сохраняем обновлённый blacklist
    try:
        DYNAMIC_BL_PATH.write_text(
            json.dumps(current_bl, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        _log(f"[AutoBL] Ошибка сохранения: {e}")
        return

    # Патчим screener.SYMBOL_BLACKLIST прямо сейчас
    _apply_dynamic_blacklist()

    # TG уведомление
    try:
        import requests as _req
        token   = tg_cfg.get("bot_token", "")
        chat_id = str(tg_cfg.get("chat_id") or tg_cfg.get("owner_chat_id", ""))
        if token and chat_id:
            syms_str = ", ".join(newly_banned)
            msg = (
                f"🚫 *Авто-черный список*\n\n"
                f"Добавлены на {BL_BAN_DAYS} дней ({BL_CONSECUTIVE_LOSS} LOSS подряд):\n"
                f"`{syms_str}`\n\n"
                f"Всего в dynamic blacklist: {len(current_bl)} символов"
            )
            _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
    except Exception as e:
        _log(f"[AutoBL] TG ошибка: {e}")


def _apply_dynamic_blacklist() -> int:
    """
    Инжектирует активные записи dynamic_blacklist.json в screener.SYMBOL_BLACKLIST.
    Возвращает число добавленных символов.
    """
    try:
        import screener
        active = _load_dynamic_blacklist()
        if not active:
            return 0
        before = len(screener.SYMBOL_BLACKLIST)
        screener.SYMBOL_BLACKLIST.update(active.keys())
        added = len(screener.SYMBOL_BLACKLIST) - before
        if added:
            _log(f"[AutoBL] Применено {added} dynamic blacklist символов "
                 f"(всего в BL: {len(screener.SYMBOL_BLACKLIST)})")
        return added
    except Exception as e:
        _log(f"[AutoBL] _apply ошибка: {e}")
        return 0


# ── Invalidation check: price-only (cheap) ────────────────────────────────────

def _check_stops(tg_cfg: dict):
    """Fetches live prices; cancels any active signal whose stop is breached."""
    import screener

    active = _load()
    if not active:
        return

    try:
        tickers = screener.fetch_all_tickers()
    except Exception as e:
        _log(f"fetch_all_tickers error: {e}")
        return

    still = []
    for sig in active:
        sym   = sig["symbol"]
        dr    = sig.get("direction", "long")
        stop  = sig.get("stop")

        if stop is None or sym not in tickers:
            still.append(sig); continue

        px = float(tickers[sym].get("lastPrice", 0) or 0)
        if px == 0:
            still.append(sig); continue

        breached = (dr == "long" and px <= stop) or (dr == "short" and px >= stop)
        if breached:
            rel = "≤" if dr == "long" else "≥"
            reason = f"стоп пробит: {px:.5g} {rel} {stop:.5g}"
            _log(f"ОТМЕНА {sym} {dr.upper()} — {reason}")
            try:
                import telegram_alerts as _tg
                _tg.send_cancel_alert(sig, reason, px, cfg=tg_cfg)
            except Exception as e:
                _log(f"TG cancel error: {e}")
        else:
            still.append(sig)

    _save(still)


# ── Full rescan: scoring + gate logic ────────────────────────────────────────

def _run_full_scan(tg_cfg: dict):
    """
    Calls run_screener(_return_candidates=True) to get fully-filtered candidates.
    Then:
      - For active signals: check score collapse or direction flip → cancel/reverse
      - For new signals: send immediate alert + add to active_signals
    """
    import screener
    import telegram_alerts as _tg

    _log("Полный скан…")
    try:
        res = screener.run_screener(top_n=50, min_score=35, _return_candidates=True)
        if not res or not isinstance(res, tuple) or len(res) < 2:
            candidates, all_results = [], []
        else:
            candidates, all_results = res
    except Exception as e:
        _log(f"run_screener error: {e}")
        return

    results_by_sym = {r["symbol"]: r for r in (all_results or []) if r}

    # ── Step 1: check existing active signals against new scan ────────────────
    active    = _load()
    still     = []
    cancelled = set()

    for sig in active:
        sym      = sig["symbol"]
        orig_sc  = sig.get("score", 0)
        old_dir  = sig.get("direction", "long")

        new_r = results_by_sym.get(sym)
        if new_r is None:
            still.append(sig); continue

        new_score = new_r.get("score", 0)

        # Build new plan to determine current direction
        new_dir = ""
        new_plan = {}
        try:
            new_plan = screener.build_trade_plan(new_r)
            new_dir  = new_plan.get("side", "")
        except Exception:
            pass

        # Priority 1: direction flip with strong opposite signal
        from screener import SETUP_TG_MIN_SCORE
        new_setup = new_r.get("best_setup", new_r.get("setup", ""))
        if (new_dir and new_dir != old_dir
                and new_score >= SETUP_TG_MIN_SCORE.get(new_setup, 80)):
            _log(f"СМЕНА {sym}: {old_dir} → {new_dir} (score {orig_sc}→{new_score})")
            try:
                _tg.send_reversal_alert(sig, new_r, new_plan, cfg=tg_cfg)
            except Exception as e:
                _log(f"TG reversal error: {e}")
            cancelled.add(sym)
            # New signal will be processed in Step 2 as a fresh candidate
            continue

        # Priority 2: score collapse (structure broke)
        if orig_sc > 0 and new_score < orig_sc * SCORE_COLLAPSE:
            reason = f"сигнал ослаб: score {orig_sc}→{new_score} (< {SCORE_COLLAPSE*100:.0f}% исходного)"
            _log(f"ОТМЕНА {sym} — {reason}")
            try:
                px = new_r.get("price", 0)
                _tg.send_cancel_alert(sig, reason, px, cfg=tg_cfg)
            except Exception as e:
                _log(f"TG cancel error: {e}")
            cancelled.add(sym)
            continue

        still.append(sig)

    _save(still)

    # ── Step 2: process new candidates ───────────────────────────────────────
    active     = _load()
    active_sym = {s["symbol"] for s in active}

    for r in (candidates or []):
        sym = r.get("symbol", "")
        if not sym or sym in active_sym:
            continue  # already active (same direction) — skip

        try:
            plan = screener.build_trade_plan(r)
        except Exception:
            continue

        side = plan.get("side", "long")
        setup = r.get("best_setup", r.get("setup", "?"))
        score = r.get("score", 0)

        # ── Rug check before signal ───────────────────────────────────────────
        rug_result = None
        try:
            import rug_detector
            rug_result = rug_detector.analyze(sym)
            time.sleep(1.5)  # CoinGecko rate limit
        except Exception as e:
            _log(f"rug_detector error for {sym}: {e}")

        if rug_result and rug_result["verdict"] == "RUG_RISK":
            _log(f"🚨 RUG_RISK блокирует сигнал {sym} (score={rug_result['risk_score']})")
            try:
                rug_detector.send_rug_alert(rug_result, cfg=tg_cfg)
            except Exception as e:
                _log(f"TG rug alert error: {e}")
            continue  # don't send signal, don't add to active

        _log(f"НОВЫЙ СИГНАЛ: {sym} {side.upper()} [{setup}] score={score}"
             + (f"  rug={rug_result['verdict']}({rug_result['risk_score']})" if rug_result else ""))

        # Send TG alert first — сигнал приходит мгновенно
        try:
            _tg.send_signal_alert(r, plan, cfg=tg_cfg)
        except Exception as e:
            _log(f"TG signal error: {e}")

        # Vision chart analysis — график 1H + AI разбор сразу после сигнала
        _tok = tg_cfg.get("bot_token", "")
        _cid = str(tg_cfg.get("chat_id", ""))
        _vision_sent = False
        try:
            import chart_analyzer as _ca
            if _ca._CHART_OK and _tok and _cid:
                _log(f"[ChartAI] генерирую график {sym}…")
                chart_path = _ca.generate_chart(sym, r)
                if chart_path:
                    analysis = _ca.analyze_with_claude(sym, chart_path, r)
                    if not analysis:
                        analysis = _ca.rule_based_analysis(sym, r)
                    caption = f"<b>{sym}</b> 1H | AI Chart  score={score}  {r.get('grade', '')}"
                    full_cap = caption + "\n\n" + analysis
                    if len(full_cap) <= 1024:
                        _ca.tg_send_photo(_tok, _cid, chart_path, caption=full_cap)
                    else:
                        _ca.tg_send_photo(_tok, _cid, chart_path, caption=caption)
                        _ca.tg_send_text(_tok, _cid, analysis)
                    _vision_sent = True
                    _log(f"[ChartAI] {sym} → отправлен")
        except Exception as e:
            _log(f"[ChartAI] ошибка: {e}")

        # Fallback: 1 предложение Haiku если Vision недоступен
        if not _vision_sent:
            haiku_note = _claude_haiku_eval(r)
            if haiku_note and _tok and _cid:
                try:
                    import requests as _req
                    _req.post(
                        f"https://api.telegram.org/bot{_tok}/sendMessage",
                        json={"chat_id": _cid, "text": haiku_note.strip()},
                        timeout=10,
                    )
                except Exception as e:
                    _log(f"[HaikuEval] TG follow-up error: {e}")

        # Send rug warning alongside signal if SUSPICIOUS/WATCH
        if rug_result and rug_result["verdict"] in ("SUSPICIOUS", "WATCH"):
            try:
                rug_detector.send_rug_alert(rug_result, cfg=tg_cfg)
            except Exception as e:
                _log(f"TG rug warn error: {e}")

        # Swing chart
        if setup == "swing":
            try:
                from swing_chart import generate_swing_chart
                from telegram_alerts import _send_photo
                tok = tg_cfg.get("bot_token", "")
                cid = str(tg_cfg.get("chat_id", ""))
                if tok and cid:
                    png = generate_swing_chart(sym, r)
                    if png:
                        _send_photo(tok, cid, png,
                                    f"<b>{sym}</b> H4 — СЕТАП 6 Hadiukov Swing")
            except Exception as e:
                _log(f"SwingChart error: {e}")

        # Register in screener cooldown so batch screener won't re-send
        try:
            screener._record_cooldown([sym])
        except Exception:
            pass

        # Add to active signals
        active.append({
            "symbol":    sym,
            "setup":     setup,
            "direction": side,
            "entry":     plan.get("entry") or r.get("price"),
            "stop":      plan.get("stop"),
            "tp1":       plan.get("tp1"),
            "score":     score,
            "grade":     r.get("grade", "—"),
            "rug_verdict": rug_result["verdict"] if rug_result else "UNKNOWN",
            "sent_at":   datetime.now().isoformat(),
            "expires_at":(datetime.now() + timedelta(hours=SIGNAL_TTL_HOURS)).isoformat(),
        })
        _save(active)

        time.sleep(0.5)  # TG rate limit between multiple signals


# ── TTL expiry ────────────────────────────────────────────────────────────────

def _expire_old(tg_cfg: dict):
    """Remove signals older than SIGNAL_TTL_HOURS, send expiry notice."""
    import telegram_alerts as _tg

    active = _load()
    cutoff = timedelta(hours=SIGNAL_TTL_HOURS)
    now    = datetime.now()
    still  = []

    for sig in active:
        try:
            age = now - datetime.fromisoformat(sig["sent_at"])
        except Exception:
            still.append(sig); continue

        if age > cutoff:
            sym = sig["symbol"]
            _log(f"TTL истёк: {sym} ({SIGNAL_TTL_HOURS}ч без TP/SL) — удаляем")
            reason = f"истёк TTL — {SIGNAL_TTL_HOURS}ч без срабатывания TP/SL"
            try:
                _tg.send_cancel_alert(sig, reason, 0.0, cfg=tg_cfg)
            except Exception:
                pass
        else:
            still.append(sig)

    _save(still)


# ── Daily AI report ──────────────────────────────────────────────────────────

def _send_daily_ai_report(tg_cfg: dict):
    """Ежедневный AI-анализ сигналов за 7 дней → Telegram (09:00 UTC)."""
    import csv
    import requests as _req
    import telegram_alerts as _tg

    # ── Читаем CSV ──────────────────────────────────────────────────────────
    resolved_path = Path(__file__).parent / "outcomes" / "pump_resolved.csv"
    if not resolved_path.exists():
        _log("[DailyAI] pump_resolved.csv не найден")
        return

    cutoff_ts = time.time() - 7 * 86400
    records: list[dict] = []
    try:
        with open(resolved_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    if float(row.get("ts", 0)) >= cutoff_ts:
                        records.append(row)
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        _log(f"[DailyAI] Ошибка чтения CSV: {e}")
        return

    if len(records) < 3:
        _log(f"[DailyAI] Только {len(records)} записей за 7 дней — пропуск")
        return

    # ── Статистика ──────────────────────────────────────────────────────────
    def _out(r: dict, col: str = "outcome_4h") -> str:
        return r.get(col, "FLAT").strip().upper()

    total  = len(records)
    wins   = [r for r in records if _out(r) == "WIN"]
    losses = [r for r in records if _out(r) == "LOSS"]
    flats  = [r for r in records if _out(r) == "FLAT"]
    pumps  = [r for r in records if r.get("signal_type") == "pump"]
    rugs   = [r for r in records if r.get("signal_type") == "rug_prep"]

    def _cnt(lst: list, outcome: str) -> int:
        return sum(1 for r in lst if _out(r) == outcome)

    avg_score = sum(float(r.get("score", 0)) for r in records) / total

    # Пропущенные движения: FLAT но цена реально пошла
    missed = []
    for r in flats:
        try:
            ch4 = float(r.get("change_4h", 0))
            if r.get("signal_type") == "pump"     and ch4 >  3.0:
                missed.append(r)
            elif r.get("signal_type") == "rug_prep" and ch4 < -3.0:
                missed.append(r)
        except (ValueError, TypeError):
            pass

    # Ложные WIN: mae_1h_pct > 2% (цена сначала шла против)
    risky_wins = []
    for r in wins:
        try:
            if float(r.get("mae_1h_pct", 0)) > 2.0:
                risky_wins.append(r)
        except (ValueError, TypeError):
            pass

    # ── Формируем тексты для prompt ─────────────────────────────────────────
    stats_text = (
        f"Всего: {total} сигналов за 7 дней\n"
        f"WIN: {len(wins)} ({len(wins)/total*100:.1f}%)  "
        f"FLAT: {len(flats)} ({len(flats)/total*100:.1f}%)  "
        f"LOSS: {len(losses)} ({len(losses)/total*100:.1f}%)\n"
        f"pump ({len(pumps)}): WIN={_cnt(pumps,'WIN')} FLAT={_cnt(pumps,'FLAT')} LOSS={_cnt(pumps,'LOSS')}\n"
        f"rug_prep ({len(rugs)}): WIN={_cnt(rugs,'WIN')} FLAT={_cnt(rugs,'FLAT')} LOSS={_cnt(rugs,'LOSS')}\n"
        f"Средний score: {avg_score:.1f}\n"
        f"Рискованных WIN (mae>2%): {len(risky_wins)}"
    )

    sig_lines = []
    for r in records[-20:]:   # последние 20 чтобы не переполнить prompt
        sig_lines.append(
            f"{r.get('symbol','?')} {r.get('signal_type','?')} score={r.get('score','?')} "
            f"OI={r.get('oi_chg_4h','?')}% CVD={r.get('cvd_pct','?')}% "
            f"fund={r.get('funding','?')}% BTC={r.get('btc_4h','?')}% "
            f"→ 1h:{r.get('change_1h','?')}%({r.get('outcome_1h','?')}) "
            f"4h:{r.get('change_4h','?')}%({r.get('outcome_4h','?')})"
        )
    signals_text = "\n".join(sig_lines)

    if missed:
        missed_text = "\n".join(
            f"{r.get('symbol')} {r.get('signal_type')} score={r.get('score')} "
            f"change_4h={r.get('change_4h')}% (FLAT но цена пошла)"
            for r in missed
        )
    else:
        missed_text = "Нет пропущенных движений"

    # ── Отправка в Telegram ──────────────────────────────────────────────────
    token   = tg_cfg.get("bot_token", "")
    chat_id = str(tg_cfg.get("chat_id", ""))
    if not token or not chat_id:
        _log("[DailyAI] Telegram не настроен")
        return

    # Если есть API ключ — просим AI проанализировать, иначе шлём сырую статистику
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        system_prompt = (
            "Ты аналитик торгового скринера криптовалют.\n"
            "Анализируешь результаты сигналов памп/раг детектора на Bybit фьючерсах.\n"
            "Пишешь на русском языке, кратко и по делу.\n"
            "Формат: только текст, никакого markdown, никаких звёздочек."
        )
        user_prompt = (
            "Вот результаты скринера за последние 7 дней:\n\n"
            f"СТАТИСТИКА:\n{stats_text}\n\n"
            f"ДЕТАЛИ КАЖДОГО СИГНАЛА:\n{signals_text}\n\n"
            f"ПРОПУЩЕННЫЕ ПАМПЫ (FLAT но цена пошла):\n{missed_text}\n\n"
            "Напиши анализ в таком формате:\n\n"
            "СВОДКА\n[2-3 предложения об общей точности]\n\n"
            "ЧТО НЕ РАБОТАЕТ\n"
            "[конкретные паттерны или пороги которые дают FLAT/LOSS — с примерами символов]\n\n"
            "ПРОПУЩЕННЫЕ ДВИЖЕНИЯ\n"
            "[почему скринер не поймал движение — что нужно изменить]\n\n"
            "ЧТО УБРАТЬ\n"
            "[сигналы или правила которые шумят и не дают результата]\n\n"
            "ЧТО ДОБАВИТЬ\n"
            "[1-2 конкретные идеи для улучшения на основе увиденных паттернов]\n\n"
            "ИТОГ\n"
            "[одно конкретное изменение которое даст максимальный эффект на следующей неделе]"
        )
        try:
            resp = _req.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 1500,
                    "system":     system_prompt,
                    "messages":   [{"role": "user", "content": user_prompt}],
                },
                timeout=60,
            )
            body = f"📊 AI-анализ скринера | {datetime.now().strftime('%d.%m.%Y')}\n\n" \
                   + resp.json()["content"][0]["text"]
        except Exception as e:
            _log(f"[DailyAI] Ошибка Anthropic API: {e}")
            return
    else:
        # Без API ключа — отправляем сырую статистику
        body = (
            f"📊 Статистика скринера | {datetime.now().strftime('%d.%m.%Y')}\n\n"
            f"{stats_text}\n\n"
            f"СИГНАЛЫ (последние 20):\n{signals_text}\n\n"
            f"ПРОПУЩЕННЫЕ ДВИЖЕНИЯ:\n{missed_text}"
        )

    full_msg = body

    MAX = 4000
    chunks: list[str] = []
    while len(full_msg) > MAX:
        split_at = full_msg.rfind("\n", 0, MAX)
        split_at = split_at if split_at > 0 else MAX
        chunks.append(full_msg[:split_at])
        full_msg = full_msg[split_at:].lstrip("\n")
    if full_msg:
        chunks.append(full_msg)

    try:
        for i, chunk in enumerate(chunks):
            _tg._send(token, chat_id, chunk)
            if i < len(chunks) - 1:
                time.sleep(0.5)
        _log(f"[DailyAI] Отчёт отправлен ({total} сигналов, {len(chunks)} частей)")
    except Exception as e:
        _log(f"[DailyAI] Ошибка отправки TG: {e}")


# ── Status print ──────────────────────────────────────────────────────────────

def _print_status():
    active = _load()
    if not active:
        _log("Активных сигналов нет")
        return
    _log(f"Активных сигналов: {len(active)}")
    for s in active:
        sent = s.get("sent_at", "?")[:19]
        _log(f"  {s['symbol']:12} {s['direction']:5} [{s['setup']}]"
             f"  score={s['score']}  entry={s.get('entry','?')}  stop={s.get('stop','?')}"
             f"  выслан {sent}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    _log(f"Запуск сигнального монитора — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    _log(f"Ценовая проверка: каждые {PRICE_CHECK_SEC}с  |  "
         f"Полный скан: каждые {FULL_SCAN_SEC}с  |  "
         f"TTL сигнала: {SIGNAL_TTL_HOURS}ч")

    import telegram_alerts as _tg
    tg_cfg = _tg.load_config()
    if not tg_cfg.get("enabled") or not tg_cfg.get("bot_token"):
        _log("ВНИМАНИЕ: Telegram не настроен — алерты не будут отправляться")

    # Startup status
    _print_status()

    last_full_scan   = 0.0
    last_expiry_chk  = 0.0
    last_daily_report: float = 0.0

    while True:
        try:
            now = time.time()

            # 1. Fast price check → stop-breach detection
            _check_stops(tg_cfg)

            # 2. Full rescan every FULL_SCAN_SEC
            if now - last_full_scan >= FULL_SCAN_SEC:
                _apply_dynamic_blacklist()   # inject dynamic BL before scan
                _run_full_scan(tg_cfg)
                last_full_scan = time.time()

            # 3. TTL expiry + consecutive-loss BL check once per hour
            if now - last_expiry_chk >= 3600:
                _expire_old(tg_cfg)
                _check_consecutive_losses(tg_cfg)   # update dynamic blacklist
                last_expiry_chk = time.time()

            # 4. Daily AI report at 09:00 UTC
            from datetime import timezone as _tz
            _utc_h = datetime.now(_tz.utc).hour
            if _utc_h == 9 and now - last_daily_report > 23 * 3600:
                try:
                    _send_daily_ai_report(tg_cfg)
                except Exception as _e:
                    _log(f"[DailyAI] Ошибка: {_e}")
                last_daily_report = time.time()

        except KeyboardInterrupt:
            _log("Остановлен (KeyboardInterrupt)")
            break
        except Exception as e:
            _log(f"Ошибка цикла: {e}")

        time.sleep(PRICE_CHECK_SEC)


if __name__ == "__main__":
    # CLI: python3 signal_monitor.py [status]
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        _print_status()
    else:
        main()
