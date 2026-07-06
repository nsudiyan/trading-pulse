"""bias — «наклон структуры» для live-сигналов дашборда.

═══ v2 (2026-07-05, вечер) — ДЕЙСТВУЮЩАЯ ВЕРСИЯ ═══
Ретро-исследование side_study (140 треков с ходом ≥5%, состояние на момент
сигнала БЕЗ look-ahead) показало:
  • v1-правила (OI/funding из кейсов VANRY/TAIKO) — 39% (7/18) на ретро,
    ХУЖЕ монетки → ВЫКЛЮЧЕНЫ из голосования (обе крайности ΔOI перед сигналом
    оказались медвежьими: OI≥+10% → 27% up, OI≤−3% → 28% up).
  • Сильные и значимые закономерности:
      A: radar-всплеск → ходуны шли ВВЕРХ 24/27 = 89% (p≈5e-5)
      B: Rose-лонг → ходуны шли ВНИЗ 56/79 = 71% контрарно (p≈3e-4);
         Rose-шорт → вверх 6/9 (n мало, применяем с пометкой «слабый»)
  • v2 in-sample: 75% по доминанте, 77% по ret24 (покрытие 115/140).
ОГОВОРКА: правила выведены из этой же выборки (in-sample) — доверие даёт
только форвард-экзамен (bias_at_birth → grade_bias), разрез по версиям.
Слабые фичи (momentum ≥+10% → down 61%, p≈0.18; OI-крайности p≈0.012 после
поправки на перебор) НЕ включены — против переобучения.

═══ v1 (2026-07-05, утро) — в архиве ниже, в проде было ~3 часа ═══

НЕ прогноз с доказанным эджем: попытки направления в проекте закрыты аудитами
(C1 2026-06-08, council 2026-06-01). Это детерминированная разметка структуры
по правилам из двух разобранных кейсов (VANRY/TAIKO, chekлист брата), с
ОБЯЗАТЕЛЬНЫМ форвард-экзаменом: наклон фиксируется в ledger-треке при рождении
сигнала (предрегистрация, не переписывается) и через сутки сверяется с ret24.
Точность показывается на дашборде — по ней решаем, чего наклон стоит.

ПРАВИЛА v1 (предрегистрированы 2026-07-05, менять только новой версией v2+):
  R1 pump-эпизод: kind=squeeze и стадия dist/break → ВНИЗ (2 голоса, TAIKO);
     kind=trend, стадия detect → ВВЕРХ (1 голос).
  R2 знак ΔOI/4ч на движении (|chg24h| ≥ 3%):
     OI ≥ +10% → наклон ПО направлению цены (1 голос) — деньги входят (VANRY);
     OI ≤ −10% → наклон ПРОТИВ направления цены (1 голос) — движение на
     закрытии позиций, топливо кончается (TAIKO).
  R3 funding-экстрим (|rate| ≥ 0.5%/период) при движении (|chg24h| ≥ 3%):
     funding ПРОТИВ движения (растём при f<0 / падаем при f>0) → усиление
     движения (1 голос) — сопротивляющаяся сторона горит и топит (VANRY).
  Сумма голосов: >0 → up, <0 → down, 0 → flat. |сумма| ≥ 2 → «заметный».

Ограничения v1 (честно): направление цены берётся грубо из chg24h тикера
(не 4ч-клины — 0 лишних запросов); ΔOI из storm_oi_history (топ-150 оборота,
у мелочи истории нет → правило R2 молчит); n кейсов-первоисточников = 2.
"""
from __future__ import annotations

BIAS_VERSION = "v3"

ROSE_CHANNELS = {"rose", "RoseSignalsPremium"}

# Снапшот vol_radar.MAJOR_SYMBOLS (2026-07-06) — не импортируем боевой модуль
# ради константы; список меняется только по отчёту резолвера, синкать руками.
RADAR_MAJORS = frozenset({
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LTCUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "ATOMUSDT", "NEARUSDT", "SUIUSDT",
    "TRXUSDT", "BCHUSDT", "ETCUSDT", "XLMUSDT", "HBARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SHIB1000USDT", "CRVUSDT", "LDOUSDT", "SEIUSDT", "AXSUSDT", "HYPEUSDT",
})


def compute_bias_v2(source: str | None, direction: str | None,
                    channel: str | None = None,
                    symbol: str | None = None) -> dict | None:
    """v3 (2026-07-06): radar-⬆ ТОЛЬКО альтам — мажоры дают ход ≥5% лишь в
    2/19 случаев (не сливают — просто стоят с рынком), наклон на них пустой;
    сам vol_radar мажоров одиночками не доставляет с 02.07 (3/96 хороших).
    Альты: ходуны вверх 30/35 = 86% (ledger, 178 финальных).
    v2-правила Rose без изменений. → {"side","strong","why","v"} | None."""
    if source == "radar":
        if symbol and symbol in RADAR_MAJORS:
            return None  # мажор: «если пойдёт — вверх» при шансе пойти ~10% — шум
        return {"side": "up", "strong": True, "v": BIAS_VERSION,
                "why": ["радар-всплеск на альте: 86% ходунов шли вверх (n=35)"]}
    is_rose = source == "rose" or (source == "tg_channel"
                                   and (channel or "") in ROSE_CHANNELS)
    if is_rose and direction == "long":
        return {"side": "down", "strong": True, "v": BIAS_VERSION,
                "why": ["Rose-лонг контрарно: 71% ходунов шли вниз (ретро n=79)"]}
    if is_rose and direction == "short":
        return {"side": "up", "strong": False, "v": BIAS_VERSION,
                "why": ["Rose-шорт контрарно: 6/9 вверх (ретро, n мало)"]}
    return None

OI_ENTER = 10.0     # |ΔOI|% порог
MOVE_MIN = 3.0      # |chg24h|% — «движение есть»
FUND_EXTREME = 0.005  # 0.5%/период


def compute_bias(chg24h: float | None, oi_chg_4h: float | None,
                 funding: float | None, pump: dict | None) -> dict | None:
    """→ {"side": "up|down|flat", "strong": bool, "why": [..], "v": BIAS_VERSION}
    None — когда сказать нечего (нет ни одного голоса)."""
    votes = 0
    why: list[str] = []

    # R1: pump-эпизод
    if pump:
        kind = pump.get("kind")
        if kind == "squeeze" and (pump.get("dist") or pump.get("broke")):
            votes -= 2
            why.append("сквиз-вертикаль в раздаче (TAIKO-паттерн)")
        elif kind == "trend" and not (pump.get("dist") or pump.get("broke")):
            votes += 1
            why.append("вертикаль на новых деньгах (OI рос)")

    moving = chg24h is not None and abs(chg24h) >= MOVE_MIN
    price_dir = 0 if not moving else (1 if chg24h > 0 else -1)

    # R2: знак ΔOI на движении
    if moving and oi_chg_4h is not None:
        if oi_chg_4h >= OI_ENTER:
            votes += price_dir
            why.append(f"OI {oi_chg_4h:+.0f}%/4ч при {chg24h:+.0f}%/24ч — деньги входят")
        elif oi_chg_4h <= -OI_ENTER:
            votes -= price_dir
            why.append(f"OI {oi_chg_4h:+.0f}%/4ч при {chg24h:+.0f}%/24ч — ход на закрытии позиций")

    # R3: funding-экстрим против движения
    if moving and funding is not None and abs(funding) >= FUND_EXTREME:
        against = (price_dir > 0 and funding < 0) or (price_dir < 0 and funding > 0)
        if against:
            votes += price_dir
            why.append(f"funding {funding * 100:+.2f}% против хода — сопротивление горит")

    if not why:
        return None
    side = "up" if votes > 0 else "down" if votes < 0 else "flat"
    return {"side": side, "strong": abs(votes) >= 2, "why": why, "v": BIAS_VERSION}


def grade_bias(side: str, ret24_pct: float | None, dead_zone: float = 2.0) -> str | None:
    """Форвард-экзамен: up прав при ret24 > +2%, down — при < −2%;
    |ret| ≤ 2% = флэт-зона (не считается); flat не экзаменуется."""
    if ret24_pct is None or side not in ("up", "down"):
        return None
    if abs(ret24_pct) <= dead_zone:
        return "flat_zone"
    hit = (side == "up") == (ret24_pct > 0)
    return "hit" if hit else "miss"


def selfcheck() -> int:
    # v3
    b = compute_bias_v2("radar", None, symbol="GIGGLEUSDT")
    assert b["side"] == "up" and b["strong"] and b["v"] == "v3", b
    assert compute_bias_v2("radar", None, symbol="BTCUSDT") is None  # мажор — шум
    b = compute_bias_v2("radar", None)  # без символа — как альт (обратная совместимость)
    assert b and b["side"] == "up", b
    b = compute_bias_v2("tg_channel", "long", "RoseSignalsPremium")
    assert b["side"] == "down" and b["strong"], b
    b = compute_bias_v2("rose", "short")
    assert b["side"] == "up" and not b["strong"], b
    assert compute_bias_v2("storm", "long") is None      # storm 59% — не правило
    assert compute_bias_v2("tg_channel", "long", "cryptoattack24") is None
    # v1 (архив — функция жива для истории)
    # TAIKO-плато: сквиз в раздаче + OI против − funding не считается (флэт цены не важен тут)
    b = compute_bias(chg24h=+40, oi_chg_4h=-30, funding=-0.025,
                     pump={"kind": "squeeze", "dist": True, "broke": False})
    assert b["side"] == "down" and b["strong"], b   # −2 (R1) −1 (R2) +1 (R3-против? f<0 при росте = за ход вверх) = −2
    # VANRY-разгон: OI входит + funding против хода вверх
    b = compute_bias(chg24h=+35, oi_chg_4h=+90, funding=-0.025, pump=None)
    assert b["side"] == "up" and b["strong"], b
    # тихая монета: сказать нечего
    assert compute_bias(chg24h=+1.0, oi_chg_4h=+2, funding=0.0001, pump=None) is None
    # падение с растущим OI: деньги входят в шорт → down
    b = compute_bias(chg24h=-12, oi_chg_4h=+15, funding=0.0001, pump=None)
    assert b["side"] == "down", b
    # экзамен
    assert grade_bias("up", +5.0) == "hit" and grade_bias("up", -5.0) == "miss"
    assert grade_bias("down", -3.1) == "hit" and grade_bias("up", 1.0) == "flat_zone"
    assert grade_bias("flat", 9.9) is None
    print("bias selfcheck: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(selfcheck())
