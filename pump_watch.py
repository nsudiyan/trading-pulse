"""pump_watch — надзор «после вертикали»: сквиз → распределение → слом плато.

Родился из разбора TAIKOUSDT 01–02.07.2026 (+324% за 3.2ч → плато → −75%):
  • вертикаль на неликвиде с ПАДАЮЩИМ OI = шорт-сквиз, не тренд (двигало
    принудительное закрытие шортов — топлива на продолжение нет);
  • плато после вертикали с РАСТУЩИМ OI + funding у лимита + огромным объёмом
    без хода цены = распределение;
  • слом низа плато = точка каскада (у TAIKO после слома было ещё −68%).

Ступень живёт ВНУТРИ WATCH-скана storm_radar (раз в ~10 мин), отдельного
демона нет. Вызов из scan() обёрнут fail-open: падение pump_watch не роняет
боевой скан (конвенция Obsidian fail-open, P0-2).

Дисциплина проекта (аудит 2026-07-01): алерты сообщают ФАКТЫ (рост, ΔOI,
funding, уровень плато) — направление и вход решает человек. Все стадии
пишутся в storm_stages.csv через log_stage: pump_detect / pump_distribution /
pump_break / pump_expire — lead-time потом считает только forward-резолвер.

Запуск:  python pump_watch.py --selfcheck   # чистые ядра на синтетике, без сети
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from file_lock import atomic_json_read, atomic_json_update

OUT = Path(__file__).parent / "outcomes"
STATE_PATH = OUT / "pump_watch_state.json"
BYBIT = "https://api.bybit.com/v5/market"

# ── пороги (калибровка: кейс TAIKO 01-02.07.2026) ──
RISE_GATE_24H = 50.0      # дешёвые ворота из tickers: 24ч-рост, дальше точный замер
RISE_MIN_4H = 80.0        # вертикаль: last к min(low) за 4ч
TURNOVER_MIN = 2_000_000  # $2M/24ч — ниже мусор без книги
OI_SQUEEZE_DROP = -10.0   # ΔOI/4ч на росте ≤ этого = сквиз (шорты закрыло)
OI_TREND_RISE = 10.0      # ΔOI/4ч ≥ этого = трендовый разгон (новые деньги)
FUNDING_EXTREME = 0.01    # |ставка за период| ≥ 1% = «у лимита»
PLATEAU_MIN_AGE_MIN = 120  # первые ~2ч после пика вершина «дышит» (у TAIKO бары
                           # по 58% диапазона): юное «плато» = шум, не уровень.
                           # Фальстарт-слом на 45м пойман бэктестом 02.07 02:20.
PLATEAU_OI_RISE = 10.0    # OI от минимума после пика ≥ +10% = набор позиций на плато
EFFORT_VOL_RATIO = 0.30   # объём часа ≥ 30% пикового часа...
EFFORT_MAX_MOVE = 5.0     # ...при |Δцены за час| < 5% = усилие без результата
BREAK_TOL = 0.005         # слом = last < plateau_low × (1 − 0.5%)
RETRY_FAIL_SEC = 60       # ретрай ignite-отправки после fail не чаще раза в минуту
WATCH_TTL_H = 48.0        # надзор эпизода; дольше — история, снимаем
STATE_CAP = 10            # максимум монет под pump-надзором (лимит внимания)


# ============================== ГЕЙТ ДЛЯ СКРИНЕРА ==============================

def long_mute_reason(symbol: str) -> str | None:
    """Монета под живым pump-эпизодом → причина мьюта ЛОНГОВ, иначе None.

    Решение брата 2026-07-03 (кейс TAIKO: скринер дал «squeeze ЛОНГ» уже после
    слома, на трупе). Лонг после вертикали = вход в распределение; шорты гейт
    НЕ трогает. Fail-open: стейт не читается → None (гейт пропускает, скринер
    не зависит от здоровья pump_watch). TTL эпизода (48ч) чистит мьют сам.
    """
    try:
        st = atomic_json_read(STATE_PATH, default={}) or {}
        d = st.get(symbol)
        if not d:
            return None
        stage = ("break" if d.get("break_sent")
                 else "dist" if d.get("dist_sent") else "detect")
        age_h = (time.time() - d.get("detected_ts", time.time())) / 3600.0
        return f"{stage},kind={d.get('kind', '?')},age={age_h:.1f}h"
    except Exception:
        return None


# ============================== ЧИСТЫЕ ЯДРА ==============================

def rise_4h_pct(bars: list) -> float | None:
    """Рост last к минимуму low за окно (~4ч 15м-баров). bars: (t,o,h,l,c,vol,usd)."""
    if len(bars) < 4:
        return None
    lo = min(b[3] for b in bars)
    last = bars[-1][4]
    return (last - lo) / lo * 100.0 if lo > 0 else None


def classify_kind(oi_chg_4h: float | None) -> str:
    """Сквиз (OI слило на росте) / тренд (OI растёт) / mixed (не читается)."""
    if oi_chg_4h is None:
        return "mixed"
    if oi_chg_4h <= OI_SQUEEZE_DROP:
        return "squeeze"
    if oi_chg_4h >= OI_TREND_RISE:
        return "trend"
    return "mixed"


def update_peak_plateau(st: dict, bars: list) -> None:
    """Трейлинг пика и низа плато. Плато = min(low) БАРОВ ПОСЛЕ бара пика;
    новый пик двигает плато заново (эпизод продолжается)."""
    for t, _o, h, l, _c, _v, _usd in bars:
        if h > st["peak"]:
            st["peak"], st["peak_ts"] = h, t / 1000.0
            st["plateau_low"] = None          # плато строится заново после пика
            st["oi_min_after_peak"] = None
        elif t / 1000.0 > st["peak_ts"]:
            if st.get("plateau_low") is None or l < st["plateau_low"]:
                st["plateau_low"] = l


def hour_usd(bars: list, back: int = 0) -> float:
    """Оборот часа: 4 бара 15м, back=0 — последний час, back=1 — предыдущий."""
    chunk = bars[-(4 * (back + 1)):-(4 * back) or None]
    return sum(b[6] for b in chunk) if chunk else 0.0


def distribution_signs(st: dict, bars: list, oi_now: float | None,
                       funding: float, now: float) -> list[str]:
    """Признаки распределения на плато — список сработавших (факты для алерта)."""
    signs: list[str] = []
    last = bars[-1][4]
    peak = st["peak"]
    off = (peak - last) / peak * 100.0 if peak > 0 else 0.0
    if not (5.0 <= off <= 45.0):          # свободное падение/новый хай — не плато
        return signs
    if (now - st["peak_ts"]) < PLATEAU_MIN_AGE_MIN * 60:
        return signs
    # 1) OI растёт от минимума после пика — на плато набирают позиции
    if oi_now and oi_now > 0:
        lo = st.get("oi_min_after_peak")
        st["oi_min_after_peak"] = min(lo, oi_now) if lo else oi_now
        base = st["oi_min_after_peak"]
        if base and (oi_now - base) / base * 100.0 >= PLATEAU_OI_RISE:
            signs.append(f"OI +{(oi_now - base) / base * 100.0:.0f}% от минимума плато")
    # 2) funding у лимита — платят конские деньги, чтобы стоять в позиции
    if abs(funding) >= FUNDING_EXTREME:
        signs.append(f"funding {funding * 100:+.2f}%/период (у лимита)")
    # 3) усилие без результата: объём почти пиковый, цена стоит
    st["peak_hour_usd"] = max(st.get("peak_hour_usd", 0.0), hour_usd(bars))
    cur, prev_close_idx = hour_usd(bars), -5
    if len(bars) >= 5 and st["peak_hour_usd"] > 0:
        move = abs(bars[-1][4] - bars[prev_close_idx][4]) / bars[prev_close_idx][4] * 100.0
        if cur >= EFFORT_VOL_RATIO * st["peak_hour_usd"] and move < EFFORT_MAX_MOVE:
            signs.append(f"объём часа {cur / 1e6:.1f}M$ (≥{EFFORT_VOL_RATIO:.0%} пикового) при ходе {move:.1f}%")
    return signs


def break_check(st: dict, last: float, now: float) -> bool:
    """Слом низа плато (главный actionable-факт эпизода)."""
    pl = st.get("plateau_low")
    if pl is None or (now - st["peak_ts"]) < PLATEAU_MIN_AGE_MIN * 60:
        return False
    return last < pl * (1.0 - BREAK_TOL)


# ============================== ДАННЫЕ ==============================

def fetch_15m(symbol: str, bars_n: int = 24) -> list:
    """Последние bars_n закрытых 15м баров: (t,o,h,l,c,vol,usd), старые→новые."""
    url = (f"{BYBIT}/kline?category=linear&symbol={symbol}"
           f"&interval=15&limit={bars_n + 1}")
    with urllib.request.urlopen(url, timeout=8) as r:
        lst = json.loads(r.read())["result"]["list"]     # новые → старые
    lst.reverse()
    bars = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
             float(x[5]), float(x[6])) for x in lst]
    # последний элемент Bybit — НЕЗАКРЫТЫЙ бар: для peak/plateau он опасен
    # (high/low ещё дышат) — отрезаем, работаем только по закрытым
    return bars[:-1]


# ============================== СООБЩЕНИЯ ==============================

def _fmt(p: float) -> str:
    from radar import fmt_price
    return fmt_price(p)


def build_detect_msg(sym: str, rise: float, kind: str, oi4: float | None,
                     funding: float, last: float) -> str:
    kind_txt = {
        "squeeze": "OI ПАДАЕТ на росте → похоже на шорт-сквиз: двигало закрытие "
                   "шортов, топливо продолжения под вопросом",
        "trend": "OI растёт вместе с ценой → в разгоне новые деньги",
        "mixed": "динамика OI не читается однозначно",
    }[kind]
    oi_txt = f"{oi4:+.0f}%/4ч" if oi4 is not None else "н/д"
    from radar import mexc_tv_link
    return "\n".join([
        f"🌋 <b>ВЕРТИКАЛЬ · {sym}</b>", "",
        f"🚀 Рост: <b>+{rise:.0f}%</b> за ~4ч · цена <code>{_fmt(last)}</code>",
        f"📈 OI: {oi_txt} — {kind_txt}",
        f"💸 Funding: {funding * 100:+.2f}%/период",
        "",
        "Дальше слежу за плато: распределение (OI↑, funding у лимита, объём без "
        "хода) и слом низа плато придут отдельными алертами.",
        "",
        "📚 <i>Справка по прошлым вертикалям (n=3: TAIKO, TLM, ES): пик лонга "
        "приходил через 24–55 мин после алерта (+8%, +8%, +137%), затем во всех "
        "трёх случаях откат 22–40% от пика. Основная развязка — на плато/сломе.</i>",
        f'📈 <a href="{mexc_tv_link(sym)}">график</a>', "",
        "<i>Это факты движения, не сигнал входа.</i>",
    ])


def build_dist_msg(sym: str, st: dict, signs: list[str], last: float) -> str:
    from radar import mexc_tv_link
    off = (st["peak"] - last) / st["peak"] * 100.0
    lines = [f"🌊 <b>РАСПРЕДЕЛЕНИЕ? · {sym}</b>", "",
             f"Плато после вертикали: пик <code>{_fmt(st['peak'])}</code>, "
             f"сейчас <code>{_fmt(last)}</code> (−{off:.0f}% от пика)"]
    lines += [f"• {s}" for s in signs]
    if st.get("plateau_low"):
        lines += ["", f"Низ плато: <code>{_fmt(st['plateau_low'])}</code> — слом ниже "
                      f"= отдельный алерт 🔻"]
    lines += ["", f'📈 <a href="{mexc_tv_link(sym)}">график</a>', "",
              "<i>Факты, не прогноз. Кейс-первоисточник: TAIKO 02.07.</i>"]
    return "\n".join(lines)


def build_break_msg(sym: str, st: dict, last: float, funding: float) -> str:
    from radar import mexc_tv_link
    off = (st["peak"] - last) / st["peak"] * 100.0
    return "\n".join([
        f"🔻 <b>СЛОМ ПЛАТО · {sym}</b>", "",
        f"Цена <code>{_fmt(last)}</code> ушла под низ плато "
        f"<code>{_fmt(st['plateau_low'])}</code> (−{off:.0f}% от пика "
        f"<code>{_fmt(st['peak'])}</code>)",
        f"💸 Funding: {funding * 100:+.2f}%/период",
        "",
        "📚 <i>Счёт сломов: TAIKO 02.07 → каскад −68%; TLM 04.07 → прокол "
        "(+19% против). Каскад не гарантирован — n пока мал.</i>",
        f'📈 <a href="{mexc_tv_link(sym)}">график</a>', "",
        "<i>Сторона слома — факт, не прогноз. Вход/выход решаешь ты.</i>",
    ])


# ============================== БЫСТРЫЙ СЛОМ (ignite-цикл, ~12с) ==============================

IGNITE_BREAKS_PATH = OUT / "pump_ignite_breaks.json"


def live_break_pass(tickers: dict, dry_run: bool = False,
                    mem_seen: set | None = None) -> None:
    """Вызывается из ignite_loop (12с): слом плато ловим за секунды, не за
    10-минутный WATCH-скан (на TAIKO лаг стоил пары %% хода; просьба брата
    2026-07-06). ВАЖНО про гонку: стейт эпизодов пишет ТОЛЬКО WATCH-скан;
    мы здесь read-only + своя очередь IGNITE_BREAKS_PATH — pump_watch_pass
    подхватит её и выставит break_sent без повторной отправки. Микроокно
    двойного алерта (скан и ignite в одну секунду) — теоретическое, дубль
    сообщения не страшен. Fail-open снаружи (ignite_loop оборачивает)."""
    if not STATE_PATH.exists():
        return
    from storm_radar import log_stage, send_tg
    from radar import radar_buttons
    state = atomic_json_read(STATE_PATH, default={}) or {}
    if not state:
        return
    queue = {} if dry_run else (atomic_json_read(IGNITE_BREAKS_PATH, default={}) or {})
    now = time.time()
    for sym, st in state.items():
        if st.get("break_sent") or not st.get("plateau_low"):
            continue
        if (now - st.get("peak_ts", now)) < PLATEAU_MIN_AGE_MIN * 60:
            continue  # плато не вызрело — те же правила, что у медленного пути
        if dry_run and mem_seen is not None and sym in mem_seen:
            continue
        q = queue.get(sym)
        if q and q.get("ts", 0) > st.get("detected_ts", 0):
            continue  # уже слали для этого эпизода
        t = tickers.get(sym)
        last = (t or {}).get("last") or 0.0
        if last <= 0 or last >= st["plateau_low"] * (1.0 - BREAK_TOL):
            continue
        # ретрай-кулдаун после неудачной отправки: не долбить TG каждые 12с
        # и не плодить строки форензики (ревью 07.07 R1 — регрессия фикса MED-1:
        # log_stage вне `if sent` перелогировал слом каждые 12с при sent=False)
        fail_ts = getattr(live_break_pass, "_fail_ts", None)
        if fail_ts is None:
            fail_ts = live_break_pass._fail_ts = {}
        if now - fail_ts.get(sym, 0) < RETRY_FAIL_SEC:
            continue
        msg = build_break_msg(sym, st, last, (t or {}).get("funding", 0.0))
        if dry_run:
            print(f"[pump][ignite-dry] BREAK {sym}: last {last} < plateau "
                  f"{st['plateau_low']}")
            if mem_seen is not None:
                mem_seen.add(sym)
            continue
        sent = send_tg(msg, radar_buttons(sym))
        if not sent:
            # ни очереди, ни log_stage: инвариант «1 слом = 1 строка» священен.
            # Ретрай через RETRY_FAIL_SEC; если TG лежит дольше — медленный
            # WATCH-скан (≤10 мин) сам отправит и залогирует слом своим путём.
            fail_ts[sym] = now
            print(f"[pump] ignite-BREAK {sym}: send fail, retry ≥{RETRY_FAIL_SEC}с")
            continue
        # очередь ПЕРЕД log_stage (kill в окне не даёт дублей); пишем только
        # при доставке (MED-1) — WATCH подхватит и выставит break_sent
        atomic_json_update(
            IGNITE_BREAKS_PATH,
            lambda d, s=sym: {**{k: v for k, v in (d or {}).items()
                                 if now - v.get("ts", 0) < WATCH_TTL_H * 3600},
                              s: {"ts": now, "price": last,
                                  "plateau": st["plateau_low"]}},
            default={})
        log_stage(sym, "pump_break", last,
                  {"plateau_low": st["plateau_low"], "peak": st.get("peak"),
                   "off_peak_pct": round((st["peak"] - last) / st["peak"] * 100, 1)
                   if st.get("peak") else None,
                   "src": "ignite"}, sent=True)
        print(f"[pump] ignite-BREAK {sym}: {last} < {st['plateau_low']} (sent=True)")


# ============================== ПРОХОД ==============================

def pump_watch_pass(tickers: dict, oi_hist: dict, now: float,
                    dry_run: bool = False) -> None:
    """Один проход pump-надзора внутри WATCH-скана storm_radar.

    tickers: fetch_tickers() (last/oi/funding/turnover/chg24h),
    oi_hist: storm_oi_history (снапшоты OI вселенной, для ΔOI/4ч)."""
    from storm_radar import log_stage, send_tg, oi_change_pct
    from radar import radar_buttons

    state = atomic_json_read(STATE_PATH, default={}) or {}
    changed = False

    # ── подхват очереди ignite-сломов: break уже ДОСТАВЛЕН быстрым циклом —
    #    выставляем флаг без повторной отправки и без повторного log_stage ──
    iq = atomic_json_read(IGNITE_BREAKS_PATH, default={}) or {}
    for sym, q in iq.items():
        st = state.get(sym)
        if st and not st.get("break_sent") and q.get("ts", 0) > st.get("detected_ts", 0):
            st["break_sent"] = True
            changed = True
            print(f"[pump] break {sym} подхвачен из ignite-очереди")

    # ── экспирия эпизодов ──
    for sym in [s for s, st in state.items()
                if now - st.get("detected_ts", now) > WATCH_TTL_H * 3600]:
        st = state.pop(sym)
        changed = True
        if not dry_run:
            log_stage(sym, "pump_expire",
                      tickers.get(sym, {}).get("last", 0.0),
                      {"peak": st.get("peak"), "broke": st.get("break_sent", False)},
                      sent=False)

    # ── детект новых вертикалей (дешёвые ворота → точный замер) ──
    candidates = [s for s, t in tickers.items()
                  if s not in state
                  and t.get("chg24h", 0.0) >= RISE_GATE_24H
                  and t.get("turnover", 0.0) >= TURNOVER_MIN]
    for sym in sorted(candidates,
                      key=lambda s: -tickers[s].get("chg24h", 0.0))[:8]:
        if len(state) >= STATE_CAP:
            print(f"[pump] cap {STATE_CAP} монет, {sym} не взят (24ч "
                  f"+{tickers[sym].get('chg24h', 0):.0f}%)")
            break
        try:
            bars = fetch_15m(sym, bars_n=16)             # 4ч закрытых баров
        except Exception as e:
            print(f"[pump] {sym}: klines fail ({e}), пропуск")
            continue
        time.sleep(0.05)
        rise = rise_4h_pct(bars)
        if rise is None or rise < RISE_MIN_4H:
            continue
        oi4 = oi_change_pct(oi_hist.get(sym, []), now)
        kind = classify_kind(oi4)
        last = bars[-1][4]
        peak_bar = max(bars, key=lambda b: b[2])
        st = {"detected_ts": now, "rise_pct": round(rise, 1), "kind": kind,
              "oi4_at_detect": oi4, "peak": peak_bar[2],
              "peak_ts": peak_bar[0] / 1000.0, "plateau_low": None,
              "oi_min_after_peak": None, "peak_hour_usd": hour_usd(bars),
              "dist_sent": False, "break_sent": False}
        update_peak_plateau(st, bars)
        msg = build_detect_msg(sym, rise, kind, oi4,
                               tickers[sym]["funding"], last)
        if dry_run:
            print(f"[pump][dry] DETECT {sym}: +{rise:.0f}%/4ч kind={kind} oi4={oi4}")
        else:
            sent = send_tg(msg, radar_buttons(sym))
            log_stage(sym, "pump_detect", last,
                      {"rise_4h_pct": round(rise, 1), "kind": kind,
                       "oi_chg_4h": oi4,
                       "funding": tickers[sym]["funding"]}, sent=sent)
        state[sym] = st
        changed = True

    # ── надзор эпизодов: распределение + слом ──
    for sym, st in list(state.items()):
        t = tickers.get(sym)
        if not t:
            continue
        try:
            bars = fetch_15m(sym, bars_n=24)             # 6ч закрытых баров
        except Exception as e:
            print(f"[pump] {sym}: klines fail в надзоре ({e})")
            continue
        time.sleep(0.05)
        if not bars:
            continue
        last = bars[-1][4]
        # ПОРЯДОК ВАЖЕН: слом проверяем по плато ПРОШЛОГО прохода, ДО трейлинга —
        # иначе бар слома сам утаскивает plateau_low вниз и слом невидим
        # (пойман бэктестом на TAIKO 02.07: BREAK не срабатывал вовсе)
        broke_now = not st.get("break_sent") and break_check(st, last, now)
        plateau_at_break = st.get("plateau_low")          # низ, по которому детектили
        update_peak_plateau(st, bars)
        changed = True                                    # peak/plateau трейлятся

        if not st.get("dist_sent"):
            signs = distribution_signs(st, bars, t.get("oi"), t.get("funding", 0.0), now)
            if len(signs) >= 2:
                msg = build_dist_msg(sym, st, signs, last)
                if dry_run:
                    print(f"[pump][dry] DIST {sym}: {signs}")
                    st["dist_sent"] = True
                else:
                    sent = send_tg(msg, radar_buttons(sym))
                    log_stage(sym, "pump_distribution", last,
                              {"signs": signs, "peak": st["peak"],
                               "plateau_low": st.get("plateau_low")}, sent=sent)
                    st["dist_sent"] = True               # один алерт на эпизод

        if broke_now:
            msg = build_break_msg(sym, {**st, "plateau_low": plateau_at_break},
                                  last, t.get("funding", 0.0))
            if dry_run:
                print(f"[pump][dry] BREAK {sym}: last {last} < plateau "
                      f"{plateau_at_break}")
                st["break_sent"] = True
            else:
                sent = send_tg(msg, radar_buttons(sym))
                log_stage(sym, "pump_break", last,
                          {"plateau_low": plateau_at_break, "peak": st["peak"],
                           "off_peak_pct": round((st["peak"] - last) / st["peak"] * 100, 1)},
                          sent=sent)
                st["break_sent"] = True

    if changed and not dry_run:
        atomic_json_update(STATE_PATH, lambda _: state, default={})
    if state:
        print(f"[pump] под надзором: {', '.join(state)}")


# ============================== SELFCHECK ==============================

def selfcheck() -> int:
    """Чистые ядра на синтетике, без сети (конвенция storm_radar --selfcheck)."""
    bar_ms = 15 * 60_000
    t0 = 1_700_000_000_000

    def mk(i, o, h, l, c, usd=1e6):
        return (t0 + i * bar_ms, o, h, l, c, usd / o, usd)

    # 1) вертикаль: 0.10 → 0.30 за 16 баров = +200% к min(low)
    vert = [mk(i, 0.10 + i * 0.0125, 0.105 + i * 0.0125, 0.098 + i * 0.0125,
               0.1125 + i * 0.0125) for i in range(16)]
    r = rise_4h_pct(vert)
    assert r and r > 150, f"vertical rise: {r}"
    # 2) боковик ±1% — НЕ вертикаль
    flat = [mk(i, 0.10, 0.101, 0.099, 0.1005) for i in range(16)]
    rf = rise_4h_pct(flat)
    assert rf is not None and rf < 5, f"flat rise: {rf}"
    # 3) классификация по OI
    assert classify_kind(-40.0) == "squeeze"
    assert classify_kind(+21.0) == "trend"
    assert classify_kind(0.0) == "mixed" and classify_kind(None) == "mixed"
    # 4) пик/плато: пик в баре 3 (h=0.60), затем плато с min low 0.36
    st = {"peak": 0.0, "peak_ts": 0.0, "plateau_low": None, "oi_min_after_peak": None}
    ep = [mk(0, 0.1, 0.2, 0.09, 0.2), mk(1, 0.2, 0.4, 0.19, 0.4),
          mk(2, 0.4, 0.60, 0.38, 0.5), mk(3, 0.5, 0.53, 0.44, 0.45),
          mk(4, 0.45, 0.50, 0.36, 0.48), mk(5, 0.48, 0.49, 0.43, 0.44)]
    update_peak_plateau(st, ep)
    assert st["peak"] == 0.60 and st["plateau_low"] == 0.36, st
    # 5) слом: 0.355 < 0.36×0.995=0.3582 (возраст пика больше 2ч)
    now = (t0 + 14 * bar_ms) / 1000.0
    assert break_check(st, 0.355, now) is True
    assert break_check(st, 0.359, now) is False           # внутри допуска — не слом
    # 6) распределение: OI вырос от минимума плато, funding у лимита
    st2 = {**st, "peak_hour_usd": 20e6, "oi_min_after_peak": 30e6}
    signs = distribution_signs(st2, ep, oi_now=34e6, funding=-0.025, now=now)
    assert any("OI" in s for s in signs) and any("funding" in s for s in signs), signs
    # свободное падение (−60% от пика) — распределением не считается
    ep_fall = ep + [mk(6, 0.44, 0.44, 0.23, 0.24)]
    st3 = {**st2}
    assert distribution_signs(st3, ep_fall, 34e6, -0.025, now) == []
    print("pump_watch selfcheck: OK (6 блоков)")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        raise SystemExit(selfcheck())
    ap.print_help()
