"""
vol_radar — самостоятельный радар волатильности для ручной торговли (план A).

Сканит USDT-перпы, ловит vol_spike (объём закрытого бара ≥ порога, но цена
ещё НЕ двинулась) на 30-мин барах, шлёт радар-алерт (монета, сила спайка,
VP-уровни, MEXC TV-ссылка). Направление — за человеком (доказано: машина сторону не угадывает).
⚠ "Опережающий"/lift/фора-цифры НЕ валидированы реальным бэктестом (аудит 2026-07-01) —
не заявлять точность, пока нет резолвера outcomes/radar_hits.csv.

Запуск:  python vol_radar.py --dry-run        # печать, без отправки
         python vol_radar.py                  # боевой скан + Telegram
         python vol_radar.py --top 100        # ограничить вселенную топ-N по обороту
"""
from __future__ import annotations
import argparse, json, time, urllib.request
from pathlib import Path
from statistics import mean

from radar import build_radar_message, radar_buttons
from volume_profile import fetch_klines

BYBIT = "https://api.bybit.com/v5/market"
COOLDOWN_PATH = Path(__file__).parent / "outcomes" / "radar_cooldown.json"
HITS_PATH = Path(__file__).parent / "outcomes" / "radar_hits.csv"   # история алертов для просмотра графиков
# Append-only facts for the auditable UI.  This intentionally does not alter
# the legacy radar_hits.csv contract consumed by old tools and parity scripts.
# Only rows created after this writer is installed have these facts.
EVENT_META_PATH = Path(__file__).parent / "outcomes" / "radar_event_metadata.jsonl"
BUDGET_PATH = Path(__file__).parent / "outcomes" / "radar_budget.json"
COOLDOWN_H = 4.0          # ДОСТАВЛЕННЫЙ символ — тишина 4ч
UNSENT_COOLDOWN_MIN = 30  # НЕдоставленный детект — только CSV-дедуп эпизода (30м):
                          # раньше недоставленный глушился на все 4ч и гига-спайк
                          # VANRY-класса через полчаса пропадал (ревью 2026-07-06 #5).
                          # cooldown-стейт хранит EXPIRY (не ts постановки);
                          # старый формат мигрируется на лету (ts → ts+4ч).
VOL_MULT = 5.0            # объём последнего бара >= 5.0× среднего (2026-07-02: 4→5, отбор лучших)
PRICE_STILL_MAX = 1.0     # |изменение цены| <= 1% — цена ещё НЕ отреагировала (опережение)
METHOD_VERSION = "vol_radar.detect_spike.closed_bar/v1"
PROVIDER = "bybit.v5.market.kline"
VENUE = "BYBIT"

# Анти-шум доставки (2026-07-02, по ретро radar_hits.csv: 131/76/23 сообщений в день):
# 50% хитов — рыночные каскады (>=3 монет разом при движении BTC), не пер-монетный сигнал.
CASCADE_N = 3             # >=3 спайков за один скан = каскад -> ОДИН дайджест вместо N карточек
CASCADE_COOLDOWN_H = 2.0  # каскад-дайджест не чаще раза в 2ч (шухер рынка — одно событие)
SCAN_TOP = 1              # одиночных карточек за скан: только сильнейший спайк
SINGLES_PER_DAY = 5       # дневной кап одиночных карточек (UTC-день); всё прочее -> CSV, sent=0

# «Пробуждение» (2026-07-05, решение брата по разбору VANRY ×23.6 sent=0):
# гига-спайк на альте — редкий класс (6 шт ≥15× за неделю), живёт ВНЕ капа
# SINGLES_PER_DAY и вне топ-1: доставляется всегда, со своей пометкой.
# Ретро: 6ч-критерий такие НЕ ловит (0/6 good — ход приходит через 12-35ч,
# VANRY +52%/35ч) — это маяк «на карандаш», не скальп-сигнал.
AWAKENING_MULT = 15.0     # порог гига-спайка
AWAKENING_PER_DAY = 3     # защитный мини-кап (аномальный день); лог при срезе

# Мажоры НЕ идут одиночными карточками (2026-07-02, резолв 246 хитов 29.06-02.07 по
# 6ч-критерию брата MFE>=5% при MAE<=1.5%: мажоры 3/96 хороших vs альты-одиночки 19/63).
# Они спайкуют объёмом на каждом рыночном чихе, но чистый ход >=5%/6ч почти не дают.
# В каскад-дайджест мажоры ВХОДЯТ (каскад = рыночное событие), в CSV пишутся всегда.
# Список = снапшот ретро-разреза; менять только по следующему отчёту резолвера.
MAJOR_SYMBOLS = frozenset({
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LTCUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "ATOMUSDT", "NEARUSDT", "SUIUSDT",
    "TRXUSDT", "BCHUSDT", "ETCUSDT", "XLMUSDT", "HBARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SHIB1000USDT", "CRVUSDT", "LDOUSDT", "SEIUSDT", "AXSUSDT", "HYPEUSDT",
})


def detect_spike(klines: list, vol_mult: float = VOL_MULT,
                 price_still_max: float = PRICE_STILL_MAX) -> dict | None:
    """Чистое ядро (тестируемо). klines: [[ts,o,h,l,c,v],...] возр. времени, нужно >= 12 баров.
    klines[-1] — текущий НЕЗАКРЫТЫЙ бар (Bybit отдаёт его последним). Его ОБЪЁМ исключается
    из расчёта — partial-bar объём несравним с full-bar средним, ratio плавает между сканами
    (аудит 2026-07-01: LA3/LA4-класс дефект); "текущий" для объёма — последний ЗАКРЫТЫЙ (pos).
    А вот ЦЕНА незакрытого бара (close = текущая цена) УЧАСТВУЕТ в guard «цена ещё стоит»:
    без неё монета, уже улетевшая на live-баре, шла как «не отреагировавшая» и алерт
    опаздывал ещё до отправки (ревью 2026-07-06 #8).
    Возвращает {vol_ratio, price_chg_pct, live_chg_pct} при спайке, иначе None."""
    if len(klines) < 12:
        return None
    vols = [b[5] for b in klines]
    pos = len(klines) - 2                  # последний ЗАКРЫТЫЙ бар (-1 — незакрытый, исключаем)
    avg = mean(vols[pos - 10:pos])         # среднее за 10 закрытых баров ДО pos (непересекающееся)
    if avg <= 0:
        return None
    vol_ratio = vols[pos] / avg
    price_chg = (klines[pos][4] - klines[pos - 1][4]) / klines[pos - 1][4] * 100.0
    # «цена ещё НЕ отреагировала» — проверяем и НА МОМЕНТ СКАНА: объём live-бара
    # в ratio не участвует (нестабилен, LA3/LA4), но ЦЕНА live-бара обязана —
    # иначе монета, уже улетевшая +5% на текущем баре, идёт как «стоячая»
    # и алерт опоздал ещё до отправки (ревью 2026-07-06 #8)
    live_chg = (klines[-1][4] - klines[pos][4]) / klines[pos][4] * 100.0
    if (vol_ratio >= vol_mult and abs(price_chg) <= price_still_max
            and abs(live_chg) <= price_still_max):
        # The following facts are observational only.  Selection, cooldown,
        # budget and notification branches never read them.
        return {"vol_ratio": round(vol_ratio, 2), "price_chg_pct": round(price_chg, 2),
                "live_chg_pct": round(live_chg, 2),
                "closed_bar_open_ms": int(klines[pos][0]),
                "closed_bar_volume": float(klines[pos][5])}
    return None


# Стейблкоины — НЕ сканируем (по построению не пампят, дают ложные/мусорные спайки).
# Исключаем по базовому активу (часть до USDT).
STABLE_BASES = {
    "USDC", "USDE", "FDUSD", "TUSD", "DAI", "USDD", "USTC", "PYUSD", "GUSD",
    "EUR", "EURC", "EURT", "EURI", "AEUR", "USD1", "USDR", "USDX", "USDY",
    "BUSD", "LUSD", "FRAX", "USDP", "SUSD", "CRVUSD", "GHO", "USDB", "USDF",
}


def is_stablecoin(symbol: str) -> bool:
    """True если базовый актив перпа — стейблкоин (SYMBOL = BASE + 'USDT')."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return base in STABLE_BASES


# Металлы/товарные перпы: спайкуют на макро-новостях (золото/серебро), для
# крипто-радара — шум того же класса, что токенизированные акции (2026-07-02).
COMMODITY_BASES = {"XAU", "XAG", "XAUT", "PAXG"}


def is_commodity(symbol: str) -> bool:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return base in COMMODITY_BASES


def _stock_symbols() -> set:
    """Тикеры токенизированных АКЦИЙ Bybit (symbolType=='stock') — MSTR/KLAC/NVDA и т.п.
    Это не крипта, исключаем. Признак из instruments-info, не хардкод-список."""
    try:
        with urllib.request.urlopen(f"{BYBIT}/instruments-info?category=linear&limit=1000", timeout=10) as r:
            lst = json.loads(r.read())["result"]["list"]
        return {x["symbol"] for x in lst if x.get("symbolType") == "stock"}
    except Exception as e:
        print(f"[radar] instruments-info fetch failed (акции не отфильтрованы): {e}")
        return set()


def fetch_perp_symbols(top: int | None = None) -> list[str]:
    """USDT-перпы Bybit БЕЗ стейблкоинов и БЕЗ токенизированных акций, опц. топ-N по обороту."""
    try:
        stocks = _stock_symbols()
        with urllib.request.urlopen(f"{BYBIT}/tickers?category=linear", timeout=10) as r:
            lst = json.loads(r.read())["result"]["list"]
        usdt = [t for t in lst if t["symbol"].endswith("USDT")
                and not is_stablecoin(t["symbol"]) and not is_commodity(t["symbol"])
                and t["symbol"] not in stocks]
        usdt.sort(key=lambda t: float(t.get("turnover24h", 0) or 0), reverse=True)
        syms = [t["symbol"] for t in usdt]
        return syms[:top] if top else syms
    except Exception as e:
        print(f"[radar] tickers fetch failed: {e}")
        return []


def _iso_from_ms(value: int) -> str:
    """UTC ISO for an exchange kline timestamp in milliseconds."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _log_event_metadata(symbol: str, sp: dict, price: float, scan_interval: str,
                        server_received_at) -> None:
    """Write source facts for a *new* hit without modifying detector behavior.

    `source_timestamp_utc` is the close time of Bybit's exact closed kline,
    not a guessed scan time.  No order-book/liquidity assertion is made here.
    """
    try:
        from datetime import timezone
        interval_minutes = int(scan_interval)
        open_ms = int(sp["closed_bar_open_ms"])
        source_close_ms = open_ms + interval_minutes * 60_000
        received = server_received_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        row = {
            "event_id": f"radar:{symbol}:{received}",
            "event_type": "radar",
            "symbol": symbol,
            "venue": VENUE,
            "provider": PROVIDER,
            "detected_at_utc": received,
            "source_timestamp_utc": _iso_from_ms(source_close_ms),
            "source_bar_open_utc": _iso_from_ms(open_ms),
            "server_received_at_utc": received,
            "timeframe": f"{interval_minutes}m",
            "method_version": METHOD_VERSION,
            "price": float(price),
            "volume": float(sp["closed_bar_volume"]),
            "vol_ratio": float(sp["vol_ratio"]),
            "data_quality": "verified",
            "liquidity_verified": False,
        }
        EVENT_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_META_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        # Observation metadata must never block an existing radar scan/log.
        print(f"[radar] event metadata skipped: {e}")


def _log_hit(symbol: str, sp: dict, price: float, sent: bool, *, server_received_at=None):
    """Append-only история алертов: ts, монета, сила спайка, цена, движение, доставлен ли."""
    import csv
    from datetime import datetime, timezone
    HITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not HITS_PATH.exists() or HITS_PATH.stat().st_size == 0
    with HITS_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts_utc", "symbol", "vol_ratio", "price", "price_chg_30m", "sent"])
        at = server_received_at or datetime.now(timezone.utc)
        w.writerow([at.strftime("%Y-%m-%dT%H:%M:%S"),
                    symbol, sp["vol_ratio"], price, sp["price_chg_pct"], int(sent)])


def _load_cooldown() -> dict:
    try:
        return json.loads(COOLDOWN_PATH.read_text())
    except Exception:
        return {}


def _save_cooldown(cd: dict):
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(cd))


def _load_budget() -> dict:
    try:
        return json.loads(BUDGET_PATH.read_text())
    except Exception:
        return {}


def _save_budget(b: dict):
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_PATH.write_text(json.dumps(b))


def select_hits(hits: list[dict], cascade_n: int = CASCADE_N,
                top_n: int = SCAN_TOP,
                majors: frozenset = MAJOR_SYMBOLS) -> tuple[str, list[dict]]:
    """Чистое решение по скану (тестируемо): >=cascade_n спайков — рыночный
    каскад, один дайджест; иначе одиночные карточки — top_n сильнейших АЛЬТОВ
    (мажоры одиночными не доставляются: 3/96 хороших в ретро, см. MAJOR_SYMBOLS)."""
    ranked = sorted(hits, key=lambda h: -h["vol_ratio"])
    if len(hits) >= cascade_n:
        return "cascade", ranked
    alts = [h for h in ranked if h["symbol"] not in majors]
    return "singles", alts[:top_n]


def select_awakenings(hits: list[dict], majors: frozenset = MAJOR_SYMBOLS,
                      mult: float = AWAKENING_MULT) -> list[dict]:
    """Чистый отбор «пробуждений»: гига-спайк ≥mult на АЛЬТЕ (мажоры — рыночный
    чих, не пробуждение неликвида). Сортировка по силе."""
    return sorted((h for h in hits
                   if h["vol_ratio"] >= mult and h["symbol"] not in majors),
                  key=lambda h: -h["vol_ratio"])


def build_awakening_message(h: dict) -> str:
    return "\n".join([
        f"🌅 <b>ПРОБУЖДЕНИЕ · {h['symbol']}</b>",
        "",
        f"Гига-объём <b>{h['vol_ratio']:.1f}×</b> нормы при цене "
        f"{h['price_chg_pct']:+.1f}%/30м — кто-то зашевелил мёртвую монету.",
        "",
        "📚 <i>Справка: класс VANRY (×23.6 → +52% через 35ч). Быстрый 6ч-ход "
        "такие дают редко (0/6 в ретро) — это маяк НА КАРАНДАШ на 1-2 суток, "
        "не сигнал входа. Дальше монету поведут storm/pump-надзор.</i>",
    ])


def build_cascade_message(hits: list[dict]) -> str:
    top, rest = hits[:5], max(0, len(hits) - 5)
    lines = [f"🌊 <b>РЫНОЧНЫЙ КАСКАД ОБЪЁМОВ</b> — {len(hits)} монет ≥{VOL_MULT:g}× разом",
             "<i>Движение всего рынка, не отдельной монеты.</i>"]
    lines += [f"• <b>{h['symbol']}</b> {h['vol_ratio']:.1f}× ({h['price_chg_pct']:+.1f}%/30м)"
              for h in top]
    if rest:
        lines.append(f"…и ещё {rest} (все в radar_hits.csv)")
    return "\n".join(lines)


def run(dry_run: bool = False, top: int | None = 150, scan_interval: str = "30"):
    syms = fetch_perp_symbols(top)
    if not syms:
        print("[radar] нет символов"); return
    cd = _load_cooldown()
    now = time.time()
    # миграция старого формата (значение = ts постановки, всегда в прошлом) →
    # expiry по старому правилу 4ч; новый формат (expiry) всегда в будущем
    cd = {k: (v if v > now else v + COOLDOWN_H * 3600) for k, v in cd.items()}
    cd = {k: v for k, v in cd.items() if v > now}  # чистим истёкшие

    hits = []                                  # проход 1: собрать ВСЕ спайки скана
    # Pure observer inputs only.  These are populated from the original scan
    # and are never read by hit selection, cooldown, budget or Telegram code.
    scan_klines: dict[str, list] = {}
    raw_hit_symbols: set[str] = set()
    for sym in syms:
        if sym in cd:
            continue
        kl = fetch_klines(sym, interval=scan_interval, limit=12)
        scan_klines[sym] = kl
        sp = detect_spike(kl)
        if sp:
            raw_hit_symbols.add(sym)
        if not sp:
            time.sleep(0.03); continue
        hits.append({"symbol": sym, "vol_ratio": sp["vol_ratio"],
                     "price_chg_pct": sp["price_chg_pct"], "price": kl[-1][4]})
        time.sleep(0.05)

    mode, chosen = select_hits(hits)           # проход 2: решить, что доставлять
    budget = _load_budget()
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if budget.get("date") != today:
        budget = {"date": today, "singles": 0, "cascade_ts": budget.get("cascade_ts", 0.0)}

    sent_syms: set[str] = set()
    # Receipt timestamps are stored only after a successful individual
    # Telegram send.  This list is not consulted by any production decision.
    forward_receipts: list[tuple[dict, object]] = []

    # «Пробуждения» ≥15×: вне капа и вне топ-1, даже при каскаде (2026-07-05).
    awakenings = select_awakenings(hits)
    if awakenings:
        aw_room = max(0, AWAKENING_PER_DAY - int(budget.get("awakenings", 0)))
        if len(awakenings) > aw_room:
            print(f"[radar] пробуждений {len(awakenings)}, мини-кап {AWAKENING_PER_DAY}/день — "
                  f"сверх капа пойдут обычным одиночным путём: "
                  f"{[h['symbol'] for h in awakenings[aw_room:]]}")
        delivered_awk: set[str] = set()
        for h in awakenings[:aw_room]:
            if dry_run:
                import re as _re
                print(f"\n{'='*50}\n[RADAR AWAKENING]\n"
                      f"{_re.sub(r'<[^>]+>', '', build_awakening_message(h))}")
                delivered_awk.add(h["symbol"])
                continue
            try:
                from telegram_alerts import _send, load_config
                cfg = load_config()
                if _send(cfg["bot_token"], str(cfg["chat_id"]),
                         build_awakening_message(h), reply_markup=radar_buttons(h["symbol"])):
                    budget["awakenings"] = int(budget.get("awakenings", 0)) + 1
                    sent_syms.add(h["symbol"])
                    forward_receipts.append((h, datetime.now(timezone.utc)))
                    delivered_awk.add(h["symbol"])
            except Exception as e:
                print(f"[radar] awakening send failed: {e}")
        # дублировать одиночной карточкой не надо ТОЛЬКО доставленное как
        # пробуждение; недоставленное (сверх мини-капа / фейл TG) остаётся
        # кандидатом обычного пути — иначе гига-спайк пропадает совсем,
        # хотя дневной бюджет одиночных свободен (ревью 2026-07-06 #4)
        chosen = [h for h in chosen if h["symbol"] not in delivered_awk]

    if dry_run:
        import re
        for h in chosen:
            msg = (build_cascade_message(chosen) if mode == "cascade"
                   else build_radar_message(h["symbol"], h["vol_ratio"],
                                            price_chg_30m=h["price_chg_pct"]))
            print(f"\n{'='*50}\n[RADAR {mode.upper()}]\n{re.sub(r'<[^>]+>', '', msg)}")
            if mode == "cascade":
                break
    elif chosen:
        try:
            from telegram_alerts import _send, load_config
            cfg = load_config()
            if mode == "cascade":
                if now - budget.get("cascade_ts", 0.0) >= CASCADE_COOLDOWN_H * 3600:
                    if _send(cfg["bot_token"], str(cfg["chat_id"]), build_cascade_message(chosen)):
                        budget["cascade_ts"] = now
                        sent_syms = {h["symbol"] for h in chosen}
                else:
                    print(f"[radar] каскад ({len(chosen)}) подавлен кулдауном {CASCADE_COOLDOWN_H:g}ч")
            else:
                room = max(0, SINGLES_PER_DAY - int(budget.get("singles", 0)))
                for h in chosen[:room]:
                    msg = build_radar_message(h["symbol"], h["vol_ratio"],
                                              price_chg_30m=h["price_chg_pct"])
                    if _send(cfg["bot_token"], str(cfg["chat_id"]), msg,
                             reply_markup=radar_buttons(h["symbol"])):
                        budget["singles"] = int(budget.get("singles", 0)) + 1
                        sent_syms.add(h["symbol"])
                        forward_receipts.append((h, datetime.now(timezone.utc)))
                if len(chosen) > room:
                    print(f"[radar] дневной кап {SINGLES_PER_DAY} исчерпан, тихо в CSV: "
                          f"{[h['symbol'] for h in chosen[room:]]}")
        except Exception as e:
            print(f"[radar] send failed: {e}")

    # H-RADAR-MOVE-01 forward infrastructure.  It runs strictly after the
    # completed delivery path above.  Symbols skipped by the existing cooldown
    # are fetched here only to complete a matched-control snapshot; this later
    # read cannot feed back into this scan's detector, delivery or state.
    if not dry_run and forward_receipts:
        for sym in syms:
            if sym in scan_klines:
                continue
            kl = fetch_klines(sym, interval=scan_interval, limit=12)
            scan_klines[sym] = kl
            if detect_spike(kl):
                raw_hit_symbols.add(sym)
            time.sleep(0.03)
        for h, receipt_time in forward_receipts:
            try:
                from radar_move_forward import record_delivery
                record_delivery(symbol=h["symbol"], vol_ratio=h["vol_ratio"],
                                symbols=syms, klines_by_symbol=scan_klines,
                                raw_hits=raw_hit_symbols,
                                receipt_time=receipt_time)
            except Exception as e:
                # Measurement cannot block or alter an already delivered alert.
                print(f"[radar] forward observer snapshot failed for {h['symbol']}: {e}")

    for h in hits:                             # история пишется ВСЯ; sent = факт доставки
        # expiry: доставленный молчит 4ч; недоставленный — 30м CSV-дедупа,
        # потом снова кандидат (шанс на доставку/пробуждение не сгорает)
        cd[h["symbol"]] = now + (COOLDOWN_H * 3600 if h["symbol"] in sent_syms
                                 else UNSENT_COOLDOWN_MIN * 60)
        receipt_time = datetime.now(timezone.utc).replace(microsecond=0)
        _log_hit(h["symbol"], h, h["price"], sent=h["symbol"] in sent_syms,
                 server_received_at=receipt_time)
        _log_event_metadata(h["symbol"], h, h["price"], scan_interval, receipt_time)
    if not dry_run:
        _save_cooldown(cd)
        _save_budget(budget)
    print(f"\n[radar] скан завершён: {len(syms)} символов, {len(hits)} спайков, "
          f"режим {mode}, доставлено {len(sent_syms)}{' (dry-run)' if dry_run else ''}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        # ровный объём → нет спайка (10 базовых + 1 закрытый "текущий" + 1 незакрытый на хвосте)
        flat = [[i, 100, 101, 99, 100, 50] for i in range(12)]
        assert detect_spike(flat) is None
        # последний ЗАКРЫТЫЙ бар (индекс -2) 6× объём, цена стоит → спайк.
        # Незакрытый хвост: ОБЪЁМ дикий (игнорируется), цена рядом (участвует и тиха).
        spike = ([[i, 100, 101, 99, 100, 50] for i in range(10)]
                 + [[10, 100, 100.5, 99.5, 100.3, 300], [11, 100.3, 100.6, 100.1, 100.4, 999999]])
        d = detect_spike(spike)
        assert d and d["vol_ratio"] >= 2.5 and abs(d["price_chg_pct"]) <= 1.0, d
        # спайк объёма НО цена уже улетела (+3%) в закрытом баре → НЕ опережающий, отбраковка
        moved = ([[i, 100, 101, 99, 100, 50] for i in range(10)]
                 + [[10, 100, 104, 100, 103, 300], [11, 103, 103.5, 102.5, 103.1, 10]])
        assert detect_spike(moved) is None, "цена двинулась >1% — не опережающий спайк"
        # закрытый бар тихий, но LIVE-цена уже улетела +5% → отбраковка (ревью #8)
        live_ran = ([[i, 100, 101, 99, 100, 50] for i in range(10)]
                    + [[10, 100, 100.5, 99.5, 100.3, 300], [11, 100.3, 105.5, 100.2, 105.3, 10]])
        assert detect_spike(live_ran) is None, "live-цена улетела — алерт уже опоздал"
        # незакрытый (последний) бар с диким объёмом/ценой ДОЛЖЕН игнорироваться целиком
        ignore_unclosed = ([[i, 100, 101, 99, 100, 50] for i in range(11)]
                            + [[11, 100, 500, 10, 490, 999999]])
        assert detect_spike(ignore_unclosed) is None, "незакрытый бар не должен триггерить спайк"
        # мало баров
        assert detect_spike(flat[:5]) is None
        # отбор доставки: 2 хита -> одиночная карточка ТОЛЬКО сильнейшего
        h = lambda s, r: {"symbol": s, "vol_ratio": r, "price_chg_pct": 0.1, "price": 1.0}
        mode, ch = select_hits([h("A", 5.1), h("B", 7.2)])
        assert mode == "singles" and [x["symbol"] for x in ch] == ["B"], (mode, ch)
        # >=3 хитов -> каскад, ранжирован по силе
        mode, ch = select_hits([h("A", 5.1), h("B", 7.2), h("C", 6.0)])
        assert mode == "cascade" and [x["symbol"] for x in ch] == ["B", "C", "A"], (mode, ch)
        assert "КАСКАД" in build_cascade_message(ch)
        # мажор-одиночка НЕ доставляется (2026-07-02: 3/96 хороших у мажоров)
        mode, ch = select_hits([h("BTCUSDT", 9.0)])
        assert mode == "singles" and ch == [], (mode, ch)
        # мажор + альт (<3, не каскад): карточка АЛЬТА, даже если мажор сильнее
        mode, ch = select_hits([h("BTCUSDT", 9.0), h("EVAAUSDT", 5.5)])
        assert mode == "singles" and [x["symbol"] for x in ch] == ["EVAAUSDT"], (mode, ch)
        # в каскаде мажоры ОСТАЮТСЯ (рыночное событие)
        mode, ch = select_hits([h("BTCUSDT", 9.0), h("ETHUSDT", 6.0), h("EVAAUSDT", 5.5)])
        assert mode == "cascade" and len(ch) == 3, (mode, ch)
        # металлы фильтруются, стейблы как раньше
        assert is_commodity("XAUTUSDT") and is_commodity("PAXGUSDT")
        assert not is_commodity("BTCUSDT") and is_stablecoin("USDCUSDT")
        print("✓ self-check passed: спайк по последнему ЗАКРЫТОМУ бару; доставка: "
              "каскад >=3 -> дайджест, иначе топ-1 сильнейший")
    else:
        run(dry_run=a.dry_run, top=a.top)


# Публичный алиас (контракт для storm_radar и др.).
stock_symbols = _stock_symbols
