"""
sector_heat.py — детектор горячих секторов.

Если 3+ символов одной категории (memecoin, AI, L2, DePIN, gaming) одновременно
показывают price >= +SECTOR_HEAT_PCT за 24h → emit candidate с setup="sector_heat",
прогон через Claude RT-фильтр, GO → личка.

Каждый запуск проходит по всем категориям, дедуп через cooldown 6 часов.

Запуск (раз в час через cron или из run_screener.sh):
    python3 sector_heat.py            — один прогон
    python3 sector_heat.py --watch    — фон, каждые SCAN_INTERVAL
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR    = Path(__file__).parent
COOLDOWN_PATH = BASE_DIR / "outcomes" / "sector_heat_cooldown.json"

# Хардкод категорий. CoinGecko Categories — можно расширить позже.
SECTORS = {
    "memecoin": {
        "DOGEUSDT", "SHIBUSDT", "1000PEPEUSDT", "WIFUSDT", "BONKUSDT",
        "FLOKIUSDT", "1000BONKUSDT", "MEMEUSDT", "POPCATUSDT", "TURBOUSDT",
        "MEWUSDT", "1000SATSUSDT", "10000NEXUSDT", "BOBBOBUSDT",
    },
    "AI": {
        "TAOUSDT", "FETUSDT", "RNDRUSDT", "WLDUSDT", "AGIXUSDT",
        "OCEANUSDT", "ARKMUSDT", "AIUSDT", "GRASSUSDT", "IOUSDT",
        "AKTUSDT", "VIRTUALUSDT", "AIXBTUSDT", "ZEREBROUSDT",
    },
    "L2": {
        "ARBUSDT", "OPUSDT", "MATICUSDT", "STRKUSDT", "ZKUSDT",
        "IMXUSDT", "LRCUSDT", "METISUSDT", "BLASTUSDT", "MNTUSDT",
        "MORPHUSDT", "TAIKOUSDT",
    },
    "DePIN": {
        "FILUSDT", "ARUSDT", "RNDRUSDT", "IOUSDT", "AKTUSDT",
        "HNTUSDT", "GRASSUSDT", "AETHIRUSDT",
    },
    "gaming": {
        "AXSUSDT", "SANDUSDT", "MANAUSDT", "IMXUSDT", "GMTUSDT",
        "PIXELUSDT", "BIGTIMEUSDT", "BEAMUSDT", "RONINUSDT", "PRIMEUSDT",
    },
    "DeFi": {
        "UNIUSDT", "AAVEUSDT", "CRVUSDT", "MKRUSDT", "COMPUSDT",
        "LDOUSDT", "DYDXUSDT", "ENAUSDT", "ETHFIUSDT", "PENDLEUSDT",
        "MORPHOUSDT",
    },
    "RWA": {
        "ONDOUSDT", "RWAUSDT", "OMUSDT", "POLYXUSDT",
    },
}

SECTOR_HEAT_PCT_24H = 5.0    # 5%+ за 24h = «горячо»
SECTOR_HEAT_MIN_COUNT = 3    # минимум 3 символа в секторе
SECTOR_COOLDOWN_SEC = 6 * 3600    # 6ч не повторяем алерт по той же категории
SCAN_INTERVAL_SEC = 600           # 10 мин при --watch


def _load_dotenv():
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip()
            if v and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            os.environ.setdefault(k, v)


_load_dotenv()


def _load_cooldown() -> dict:
    if not COOLDOWN_PATH.exists():
        return {}
    try:
        return json.loads(COOLDOWN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cooldown(cd: dict):
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_PATH.write_text(json.dumps(cd, indent=2), encoding="utf-8")


def scan_sectors() -> list:
    """Возвращает список горячих секторов с метаданными."""
    try:
        import screener as _scr
        tickers = _scr.fetch_all_tickers()
    except Exception as e:
        print(f"[Sector] fetch_all_tickers error: {e}")
        return []

    hot_sectors = []
    cooldown = _load_cooldown()
    now = int(time.time())

    for sector, symbols in SECTORS.items():
        # Cooldown
        if now - cooldown.get(sector, 0) < SECTOR_COOLDOWN_SEC:
            continue

        hot_syms = []
        for sym in symbols:
            t = tickers.get(sym)
            if not t:
                continue
            price = float(t.get("lastPrice") or 0)
            prev  = float(t.get("prevPrice24h") or price)
            if prev <= 0:
                continue
            chg = (price - prev) / prev * 100
            if chg >= SECTOR_HEAT_PCT_24H:
                hot_syms.append({
                    "symbol": sym,
                    "chg_24h": round(chg, 2),
                    "price":   price,
                    "turnover": float(t.get("turnover24h") or 0),
                })

        if len(hot_syms) >= SECTOR_HEAT_MIN_COUNT:
            hot_syms.sort(key=lambda x: -x["chg_24h"])
            hot_sectors.append({
                "sector": sector,
                "count":  len(hot_syms),
                "avg_chg": round(sum(s["chg_24h"] for s in hot_syms) / len(hot_syms), 2),
                "leader":  hot_syms[0]["symbol"],
                "symbols": hot_syms[:8],
            })

    hot_sectors.sort(key=lambda x: -x["avg_chg"])
    return hot_sectors


def emit_alerts(hot_sectors: list):
    if not hot_sectors:
        print(f"[Sector] нет горячих секторов")
        return
    try:
        from claude_realtime_filter import filter_candidate
        import telegram_alerts as _tg
    except Exception as e:
        print(f"[Sector] импорт error: {e}")
        return

    cfg = _tg.load_config()
    token = cfg.get("bot_token", "")
    chat  = str(cfg.get("chat_id", ""))
    cooldown = _load_cooldown()
    now = int(time.time())

    for h in hot_sectors:
        sector = h["sector"]
        leader = h["leader"]
        # Для filter_candidate составляем candidate из лидера сектора
        cand = {
            "symbol":      leader,
            "setup":       f"sector_heat_{sector}",
            "stage":       f"🔥 SECTOR HEAT: {sector}",
            "direction":   "LONG",   # heat обычно про LONG
            "score":       80 + h["count"] * 5,   # больше символов = выше score
            "price":       h["symbols"][0]["price"],
            "price_chg_4h": h["avg_chg"],
            "signals":     [
                f"🔥 Сектор '{sector}' разогрет: {h['count']} символов >+{SECTOR_HEAT_PCT_24H}%",
                f"Лидер: {leader} (+{h['symbols'][0]['chg_24h']:.1f}%)",
                f"Средний рост: +{h['avg_chg']:.1f}%",
                f"Состав: {', '.join(s['symbol'] for s in h['symbols'][:5])}",
            ],
        }
        v = filter_candidate(leader, cand, source="sector_heat")
        action = v.get("action")
        print(f"[Sector] {sector} (avg +{h['avg_chg']:.1f}%, n={h['count']}) → {action} "
              f"conf={v.get('confidence',0):.2f}")
        if action != "GO":
            continue

        # GO → TG
        if token and chat:
            lines = [
                f"🔥 <b>SECTOR HEAT</b>  |  {datetime.now().strftime('%H:%M')}",
                f"<b>{sector.upper()}</b>  ×{h['count']} символов  avg +{h['avg_chg']:.1f}%",
                "",
                f"  Лидер: <b>{leader}</b>  +{h['symbols'][0]['chg_24h']:.1f}%",
                "",
                "Состав:",
            ]
            for s in h["symbols"][:8]:
                lines.append(f"  · {s['symbol']:14s}  +{s['chg_24h']:5.1f}%  "
                             f"vol ${s['turnover']/1e6:.1f}M")
            lines += [
                "",
                f"🧠 Claude conf={v.get('confidence',0):.0%}: {v.get('reasoning','')[:280]}",
            ]
            for risk in (v.get("risks") or [])[:3]:
                lines.append(f"  ⚠ {str(risk)[:120]}")

            import requests
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML"},
                timeout=10,
            )

        cooldown[sector] = now

    _save_cooldown(cooldown)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="фон, каждые 10 мин")
    args = ap.parse_args()
    if args.watch:
        print(f"[Sector] watch mode — каждые {SCAN_INTERVAL_SEC}с")
        while True:
            try:
                hot = scan_sectors()
                emit_alerts(hot)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[Sector] цикл error: {e}")
            time.sleep(SCAN_INTERVAL_SEC)
    else:
        hot = scan_sectors()
        emit_alerts(hot)


if __name__ == "__main__":
    main()
