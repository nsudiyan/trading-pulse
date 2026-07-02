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
BUDGET_PATH = Path(__file__).parent / "outcomes" / "radar_budget.json"
COOLDOWN_H = 4.0          # один символ не чаще раза в 4ч
VOL_MULT = 5.0            # объём последнего бара >= 5.0× среднего (2026-07-02: 4→5, отбор лучших)
PRICE_STILL_MAX = 1.0     # |изменение цены| <= 1% — цена ещё НЕ отреагировала (опережение)

# Анти-шум доставки (2026-07-02, по ретро radar_hits.csv: 131/76/23 сообщений в день):
# 50% хитов — рыночные каскады (>=3 монет разом при движении BTC), не пер-монетный сигнал.
CASCADE_N = 3             # >=3 спайков за один скан = каскад -> ОДИН дайджест вместо N карточек
CASCADE_COOLDOWN_H = 2.0  # каскад-дайджест не чаще раза в 2ч (шухер рынка — одно событие)
SCAN_TOP = 1              # одиночных карточек за скан: только сильнейший спайк
SINGLES_PER_DAY = 5       # дневной кап одиночных карточек (UTC-день); всё прочее -> CSV, sent=0


def detect_spike(klines: list, vol_mult: float = VOL_MULT,
                 price_still_max: float = PRICE_STILL_MAX) -> dict | None:
    """Чистое ядро (тестируемо). klines: [[ts,o,h,l,c,v],...] возр. времени, нужно >= 12 баров.
    klines[-1] — текущий НЕЗАКРЫТЫЙ бар (Bybit отдаёт его последним), ИСКЛЮЧАЕТСЯ из расчёта —
    та же конвенция, что в vol_core.py (pos=n-2), иначе partial-bar объём/цена сравниваются
    с full-bar средним/close и дают нестабильный, несравнимый между сканами результат (аудит
    2026-07-01: LA3/LA4-класс дефект). "Текущий" для расчёта — последний ЗАКРЫТЫЙ бар (pos).
    Возвращает {vol_ratio, price_chg_pct} если спайк на последнем закрытом баре, иначе None."""
    if len(klines) < 12:
        return None
    vols = [b[5] for b in klines]
    pos = len(klines) - 2                  # последний ЗАКРЫТЫЙ бар (-1 — незакрытый, исключаем)
    avg = mean(vols[pos - 10:pos])         # среднее за 10 закрытых баров ДО pos (непересекающееся)
    if avg <= 0:
        return None
    vol_ratio = vols[pos] / avg
    price_chg = (klines[pos][4] - klines[pos - 1][4]) / klines[pos - 1][4] * 100.0
    if vol_ratio >= vol_mult and abs(price_chg) <= price_still_max:
        return {"vol_ratio": round(vol_ratio, 2), "price_chg_pct": round(price_chg, 2)}
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


def _log_hit(symbol: str, sp: dict, price: float, sent: bool):
    """Append-only история алертов: ts, монета, сила спайка, цена, движение, доставлен ли."""
    import csv
    from datetime import datetime, timezone
    HITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not HITS_PATH.exists() or HITS_PATH.stat().st_size == 0
    with HITS_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts_utc", "symbol", "vol_ratio", "price", "price_chg_30m", "sent"])
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
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
                top_n: int = SCAN_TOP) -> tuple[str, list[dict]]:
    """Чистое решение по скану (тестируемо): >=cascade_n спайков — рыночный
    каскад, один дайджест; иначе только top_n сильнейших одиночными карточками."""
    ranked = sorted(hits, key=lambda h: -h["vol_ratio"])
    if len(hits) >= cascade_n:
        return "cascade", ranked
    return "singles", ranked[:top_n]


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
    cd = {k: v for k, v in cd.items() if now - v < COOLDOWN_H * 3600}  # чистим протухшие

    hits = []                                  # проход 1: собрать ВСЕ спайки скана
    for sym in syms:
        if sym in cd:
            continue
        kl = fetch_klines(sym, interval=scan_interval, limit=12)
        sp = detect_spike(kl)
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
                if len(chosen) > room:
                    print(f"[radar] дневной кап {SINGLES_PER_DAY} исчерпан, тихо в CSV: "
                          f"{[h['symbol'] for h in chosen[room:]]}")
        except Exception as e:
            print(f"[radar] send failed: {e}")

    for h in hits:                             # история пишется ВСЯ; sent = факт доставки
        cd[h["symbol"]] = now                  # кулдаун всем детектам — CSV без дублей раз в 5 мин
        _log_hit(h["symbol"], {"vol_ratio": h["vol_ratio"], "price_chg_pct": h["price_chg_pct"]},
                 h["price"], sent=h["symbol"] in sent_syms)
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
        # Последний элемент (-1, незакрытый) — мусорные значения, не должны влиять.
        spike = ([[i, 100, 101, 99, 100, 50] for i in range(10)]
                 + [[10, 100, 100.5, 99.5, 100.3, 300], [11, -1, -1, -1, -1, -1]])
        d = detect_spike(spike)
        assert d and d["vol_ratio"] >= 2.5 and abs(d["price_chg_pct"]) <= 1.0, d
        # спайк объёма НО цена уже улетела (+3%) в закрытом баре → НЕ опережающий, отбраковка
        moved = ([[i, 100, 101, 99, 100, 50] for i in range(10)]
                 + [[10, 100, 104, 100, 103, 300], [11, -1, -1, -1, -1, -1]])
        assert detect_spike(moved) is None, "цена двинулась >1% — не опережающий спайк"
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
        # металлы фильтруются, стейблы как раньше
        assert is_commodity("XAUTUSDT") and is_commodity("PAXGUSDT")
        assert not is_commodity("BTCUSDT") and is_stablecoin("USDCUSDT")
        print("✓ self-check passed: спайк по последнему ЗАКРЫТОМУ бару; доставка: "
              "каскад >=3 -> дайджест, иначе топ-1 сильнейший")
    else:
        run(dry_run=a.dry_run, top=a.top)


# Публичный алиас (контракт для storm_radar и др.).
stock_symbols = _stock_symbols
