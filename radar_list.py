"""
Просмотр истории радар-алертов: время, монета, сила спайка, цена, ссылка на MEXC-график.
Запуск:  python radar_list.py          # все алерты (новые сверху)
         python radar_list.py 30       # последние 30
"""
import csv, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from radar import mexc_tv_link

HITS = Path(__file__).parent / "outcomes" / "radar_hits.csv"


def main(limit=None):
    if not HITS.exists() or HITS.stat().st_size == 0:
        print("Пока нет алертов. Радар сканит каждые 10 мин и пишет сюда при спайке ≥3.0×.")
        return
    rows = list(csv.DictReader(HITS.open(encoding="utf-8")))
    rows.sort(key=lambda r: r["ts_utc"], reverse=True)
    if limit:
        rows = rows[:limit]
    print(f"{'ВРЕМЯ МСК':<12}{'ДАТА':<12}{'МОНЕТА':<13}{'СИЛА':<7}{'ЦЕНА':<13}{'30м':<8}ГРАФИК (MEXC perp)")
    print("─" * 110)
    for r in rows:
        try:
            dt = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            msk = (dt + timedelta(hours=3)).strftime("%H:%M")
            date = dt.strftime("%m-%d")
        except Exception:
            msk, date = r["ts_utc"][11:16], r["ts_utc"][:10]
        sent = "" if r.get("sent") == "1" else " (не отпр.)"
        print(f"{msk:<12}{date:<12}{r['symbol']:<13}{float(r['vol_ratio']):>4.1f}×  "
              f"{r['price']:<13.10}{float(r['price_chg_30m']):>+5.1f}%  {mexc_tv_link(r['symbol'])}{sent}")
    print(f"\nВсего: {len(rows)} алертов")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None)
