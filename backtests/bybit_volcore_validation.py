#!/usr/bin/env python3
"""
backtests/bybit_volcore_validation.py — ШАГ #2.1: кросс-биржевая валидация объёмного ядра.

Вопрос: валидированная объёмная статистика (30-дн медиана 1h, непересекающаяся база)
была посчитана на BINANCE klines. Live-бот читает BYBIT. Переносится ли edge?

Тянем Bybit 1h klines (public, без ключа), считаем vol_ratio_1h/4h + vol_spike_count
ТОЙ ЖЕ валидированной статистикой, что feature_engineer.klines_features, и сверяем:
(1) согласуются ли Bybit и Binance vol_ratio (corr); (2) предсказывает ли Bybit-версия
магнитуду (Spearman vs реализованный MFE24h из joined_dataset.csv); (3) лифт high-vol
сабсета по MFE. Если переносится → правка live оправдана. Если нет → edge венdue-specific.

Только чтение Bybit (отдельный процесс, не трогает live-бот). Воспроизводимо.
"""
from __future__ import annotations
import csv, time
from pathlib import Path
from datetime import datetime, timezone
import numpy as np, pandas as pd, requests
from scipy.stats import spearmanr, pearsonr

BASE = Path(__file__).parent.parent
JD = BASE / "backtests" / "joined_dataset.csv"
OUT = BASE / "backtests" / "BYBIT_VOLCORE_VALIDATION.md"
BYBIT = "https://api.bybit.com/v5/market/kline"
H1 = 3600_000

def to_ms(ts):
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try: return int(datetime.strptime(ts.strip()[:19], f).replace(tzinfo=timezone.utc).timestamp()*1000)
        except ValueError: continue
    return None

def fnum(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def fetch_bybit_1h(sym, start_ms, end_ms):
    bars = {}
    cur_end = end_ms
    for _ in range(8):  # до ~8000 баров
        try:
            r = requests.get(BYBIT, params={"category": "linear", "symbol": sym,
                "interval": "60", "limit": "1000", "end": cur_end}, timeout=10).json()
            lst = r.get("result", {}).get("list", [])
        except Exception:
            break
        if not lst: break
        for b in lst:
            t = int(b[0]); bars[t] = (float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
        oldest = min(int(b[0]) for b in lst)
        if oldest <= start_ms: break
        cur_end = oldest - 1
        time.sleep(0.08)
    if len(bars) < 200: return None
    df = pd.DataFrame([(t,)+v for t, v in sorted(bars.items())],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    df["vol_med_30d"] = df["volume"].rolling(720, min_periods=168).median().shift(1)
    return df

def factors(df, start_ms):
    ts = df["ts"].values
    pos = int(np.searchsorted(ts, start_ms - 1, side="right") - 1)  # last bar < t
    if pos < 30: return None
    vm = df["vol_med_30d"].iloc[pos]
    if not np.isfinite(vm) or vm <= 0: return None
    v1 = float(df["volume"].iloc[pos])
    b4 = df["volume"].iloc[max(0, pos-3):pos+1]
    return {"vr1": v1/vm, "vr4": float(b4.mean())/vm,
            "spike": int((b4 > 2*vm).sum()),
            "kl_close": float(df["close"].iloc[pos])}

def main():
    sig = list(csv.DictReader(open(JD)))
    by_sym = {}
    for r in sig:
        by_sym.setdefault(r["symbol"], []).append(r)
    syms = sorted(by_sym)
    print(f"символов в joined_dataset: {len(syms)}  сигналов: {len(sig)}")

    rows = []
    for i, sym in enumerate(syms, 1):
        mss = [to_ms(r["run_ts"]) for r in by_sym[sym] if to_ms(r["run_ts"])]
        if not mss: continue
        df = fetch_bybit_1h(sym, min(mss) - 35*24*H1, max(mss) + H1)
        ok = 0
        if df is not None:
            for r in by_sym[sym]:
                ms = to_ms(r["run_ts"])
                f = factors(df, ms) if ms else None
                if not f: continue
                pe = fnum(r["price_entry"])
                rows.append({
                    "symbol": sym,
                    "bybit_vr1": f["vr1"], "bybit_vr4": f["vr4"], "bybit_spike": f["spike"],
                    "binance_vr1": fnum(r["vol_ratio_1h"]),
                    "binance_vscore": fnum(r["vol_score"]),
                    "mfe_24h": fnum(r["mfe_24h"]), "mae_24h": fnum(r["mae_24h"]),
                    "price_match": (pe and abs(f["kl_close"]-pe)/pe < 0.05),
                })
                ok += 1
        print(f"  [{i}/{len(syms)}] {sym}: {'нет klines' if df is None else f'{ok} сигналов'}")

    if len(rows) < 30:
        print("МАЛО данных для вывода:", len(rows)); return

    df = pd.DataFrame(rows)
    df["bybit_vscore"] = (15*(df.bybit_spike>=1) + 10*(df.bybit_vr1>1.0) + 8*(df.bybit_vr4>1.0))
    pm = df["price_match"].mean()

    # 1) согласие Bybit vs Binance vol_ratio
    m = df.dropna(subset=["bybit_vr1", "binance_vr1"])
    rho_xv = spearmanr(m.bybit_vr1, m.binance_vr1).correlation
    # 2) Bybit vol_ratio предсказывает MFE?
    mm = df.dropna(subset=["bybit_vr1", "mfe_24h"])
    rho_mfe_by = spearmanr(mm.bybit_vr1, mm.mfe_24h).correlation
    mb = df.dropna(subset=["binance_vr1", "mfe_24h"])
    rho_mfe_bn = spearmanr(mb.binance_vr1, mb.mfe_24h).correlation
    # 3) high-vol (bybit) лифт по MFE
    hi = df[df.bybit_vscore >= 23]; lo = df[df.bybit_vscore == 0]
    mfe_hi = hi["mfe_24h"].mean(); mfe_lo = lo["mfe_24h"].mean(); mfe_all = df["mfe_24h"].mean()

    out = [
        "# Кросс-биржевая валидация объёмного ядра (Bybit vs Binance)\n",
        f"**Покрытие:** {len(df)} сигналов, {df.symbol.nunique()} символов. price_match(Bybit) = {pm:.1%}.\n",
        "## Переносится ли edge на Bybit (живой венdue бота)?\n",
        f"- Согласие Bybit↔Binance vol_ratio_1h (Spearman): **{rho_xv:.3f}** (близко к 1 = одно и то же)",
        f"- Bybit vol_ratio_1h → MFE24h (Spearman): **{rho_mfe_by:.3f}**  (Binance был {rho_mfe_bn:.3f})",
        f"- High-vol (Bybit vscore≥23, n={len(hi)}) средний MFE24h = **{mfe_hi:.2f}%**  vs нет-объёма (n={len(lo)}) = {mfe_lo:.2f}%  vs все = {mfe_all:.2f}%",
        "\n## Вердикт",
        f"{'✅ ПЕРЕНОСИТСЯ' if (rho_xv>0.5 and rho_mfe_by>0.3) else '⚠️ ЧАСТИЧНО/НЕТ'} — "
        f"{'правка live на Bybit оправдана, edge сохраняется' if (rho_xv>0.5 and rho_mfe_by>0.3) else 'edge на Bybit слабее — нужна осторожность/донастройка порогов'}.",
    ]
    OUT.write_text("\n".join(out))
    print("\n" + "\n".join(out))
    print(f"\nSaved: {OUT.name}")

if __name__ == "__main__":
    main()
