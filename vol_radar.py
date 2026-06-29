"""
vol_radar — самостоятельный радар волатильности для ручной торговли (план A).

Сканит USDT-перпы, ловит ОПЕРЕЖАЮЩИЙ vol_spike (объём скакнул ≥ порога, но цена
ещё НЕ двинулась — фора ~30-60мин), шлёт радар-алерт (монета, сила спайка,
VP-уровни, MEXC TV-ссылка). Направление — за человеком (доказано: машина сторону не угадывает).

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
COOLDOWN_H = 4.0          # один символ не чаще раза в 4ч
VOL_MULT = 2.5            # объём последнего бара >= 2.5× среднего
PRICE_STILL_MAX = 1.0     # |изменение цены| <= 1% — цена ещё НЕ отреагировала (опережение)


def detect_spike(klines: list, vol_mult: float = VOL_MULT,
                 price_still_max: float = PRICE_STILL_MAX) -> dict | None:
    """Чистое ядро (тестируемо). klines: [[ts,o,h,l,c,v],...] возр. времени, нужно >= 12 баров.
    Возвращает {vol_ratio, price_chg_pct} если опережающий спайк, иначе None."""
    if len(klines) < 12:
        return None
    vols = [b[5] for b in klines]
    avg = mean(vols[-11:-1])                       # среднее за 10 баров до последнего
    if avg <= 0:
        return None
    vol_ratio = vols[-1] / avg
    price_chg = (klines[-1][4] - klines[-2][4]) / klines[-2][4] * 100.0
    if vol_ratio >= vol_mult and abs(price_chg) <= price_still_max:
        return {"vol_ratio": round(vol_ratio, 2), "price_chg_pct": round(price_chg, 2)}
    return None


def fetch_perp_symbols(top: int | None = None) -> list[str]:
    """USDT-перпы Bybit, опц. топ-N по обороту (turnover24h)."""
    try:
        with urllib.request.urlopen(f"{BYBIT}/tickers?category=linear", timeout=10) as r:
            lst = json.loads(r.read())["result"]["list"]
        usdt = [t for t in lst if t["symbol"].endswith("USDT")]
        usdt.sort(key=lambda t: float(t.get("turnover24h", 0) or 0), reverse=True)
        syms = [t["symbol"] for t in usdt]
        return syms[:top] if top else syms
    except Exception as e:
        print(f"[radar] tickers fetch failed: {e}")
        return []


def _load_cooldown() -> dict:
    try:
        return json.loads(COOLDOWN_PATH.read_text())
    except Exception:
        return {}


def _save_cooldown(cd: dict):
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(cd))


def run(dry_run: bool = False, top: int | None = 150, scan_interval: str = "30"):
    syms = fetch_perp_symbols(top)
    if not syms:
        print("[radar] нет символов"); return
    cd = _load_cooldown()
    now = time.time()
    cd = {k: v for k, v in cd.items() if now - v < COOLDOWN_H * 3600}  # чистим протухшие
    hits = 0
    for sym in syms:
        if sym in cd:
            continue
        kl = fetch_klines(sym, interval=scan_interval, limit=12)
        sp = detect_spike(kl)
        if not sp:
            time.sleep(0.03); continue
        msg = build_radar_message(sym, sp["vol_ratio"], price_chg_30m=sp["price_chg_pct"])
        hits += 1
        if dry_run:
            import re
            print(f"\n{'='*50}\n[RADAR HIT] {sym}\n{re.sub(r'<[^>]+>','',msg)}")
        else:
            try:
                from telegram_alerts import _send, load_config
                cfg = load_config()
                _send(cfg["bot_token"], str(cfg["chat_id"]), msg, reply_markup=radar_buttons(sym))
                cd[sym] = now
            except Exception as e:
                print(f"[radar] send failed {sym}: {e}")
        time.sleep(0.05)
    if not dry_run:
        _save_cooldown(cd)
    print(f"\n[radar] скан завершён: {len(syms)} символов, {hits} спайков{' (dry-run)' if dry_run else ' отправлено'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=150)
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        # ровный объём → нет спайка
        flat = [[i, 100, 101, 99, 100, 50] for i in range(12)]
        assert detect_spike(flat) is None
        # последний бар 5× объём, цена стоит → спайк
        spike = [[i, 100, 101, 99, 100, 50] for i in range(11)] + [[11, 100, 100.5, 99.5, 100.3, 300]]
        d = detect_spike(spike)
        assert d and d["vol_ratio"] >= 2.5 and abs(d["price_chg_pct"]) <= 1.0, d
        # спайк объёма НО цена уже улетела (+3%) → НЕ опережающий, отбраковка
        moved = [[i, 100, 101, 99, 100, 50] for i in range(11)] + [[11, 100, 104, 100, 103, 300]]
        assert detect_spike(moved) is None, "цена двинулась >1% — не опережающий спайк"
        # мало баров
        assert detect_spike(flat[:5]) is None
        print("✓ self-check passed: спайк ловится только при объём↑ И цена ещё стоит")
    else:
        run(dry_run=a.dry_run, top=a.top)
