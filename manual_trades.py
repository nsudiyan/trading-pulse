"""
Ручной леджер сделок по радару (план A).

Брат жмёт [Лонг]/[Шорт] на радар-алерте → пишем его РЕШЕНИЕ с фактической ценой
входа. Append-only. Это честная статистика ТВОЕЙ ручной торговли — учишься на
своих данных (win rate, средний R), а не на фантоме автобота.
"""
from __future__ import annotations
import csv, json, urllib.request
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path(__file__).parent / "outcomes" / "manual_trades.csv"
FIELDS = ["ts", "symbol", "side", "entry_price", "source"]


def _last_price(symbol: str) -> float | None:
    try:
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}"
        with urllib.request.urlopen(url, timeout=6) as r:
            return float(json.loads(r.read())["result"]["list"][0]["lastPrice"])
    except Exception:
        return None


def record_manual_trade(symbol: str, side: str, source: str = "radar") -> tuple[bool, str]:
    """Пишет ручную сделку с текущей ценой. Возвращает (ok, инфо-строка для ответа)."""
    if side not in ("long", "short"):
        return False, f"bad side {side!r}"
    price = _last_price(symbol)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    new = not LEDGER.exists() or LEDGER.stat().st_size == 0
    with LEDGER.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "symbol": symbol, "side": side,
                    "entry_price": price if price else "", "source": source})
    return True, (f"@ {price}" if price else "(цена не получена)")


if __name__ == "__main__":
    import tempfile
    LEDGER = Path(tempfile.mkdtemp()) / "manual_trades.csv"
    ok, _ = record_manual_trade("BTCUSDT", "long")
    assert ok
    assert not record_manual_trade("BTCUSDT", "wait")[0]   # bad side отклонён
    rows = list(csv.DictReader(LEDGER.open()))
    assert len(rows) == 1 and rows[0]["side"] == "long" and rows[0]["symbol"] == "BTCUSDT"
    print(f"✓ self-check passed: ручная сделка записана ({rows[0]['ts']}, цена {rows[0]['entry_price'] or 'n/a'})")
