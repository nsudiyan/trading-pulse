#!/usr/bin/env python3
"""TSMOM adversarial re-test: exclude 2023, check regime-dependence.
Downloads 2.7y daily from Bybit V5 public. Cluster/by-year CI. Skeptic default.
"""
import requests, time, sys, math
import numpy as np, pandas as pd
from pathlib import Path

CACHE = Path("/Users/nikitasudian/Desktop/трейдинг/research/daily_cache")
CACHE.mkdir(exist_ok=True)

# Liquid USDT perps with history back to ~2023 (the panel used originally).
# Note: this IS a survivor-tilted set (coins still listed+liquid in 2026).
UNIVERSE = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT",
"LINKUSDT","DOTUSDT","MATICUSDT","LTCUSDT","BCHUSDT","ATOMUSDT","UNIUSDT","XLMUSDT",
"NEARUSDT","FILUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","TRXUSDT",
"ETCUSDT","ICPUSDT","HBARUSDT","AAVEUSDT","ALGOUSDT","SANDUSDT","MANAUSDT","AXSUSDT",
"FTMUSDT","RUNEUSDT","GALAUSDT","EGLDUSDT","FLOWUSDT","CHZUSDT","CRVUSDT","DYDXUSDT",
"SEIUSDT","TIAUSDT"]

def fetch_daily(sym):
    f = CACHE / f"{sym}.csv"
    if f.exists():
        df = pd.read_csv(f, parse_dates=["date"])
        return df
    start = int(pd.Timestamp("2023-08-01", tz="UTC").timestamp()*1000)
    end = int(pd.Timestamp("2026-06-17", tz="UTC").timestamp()*1000)
    rows = []
    cur = start
    url = "https://api.bybit.com/v5/market/kline"
    while cur < end:
        try:
            r = requests.get(url, params={"category":"linear","symbol":sym,
                "interval":"D","start":cur,"limit":1000}, timeout=20)
            j = r.json()
            lst = j.get("result",{}).get("list",[])
        except Exception as e:
            print(f"  {sym} err {e}"); time.sleep(1); continue
        if not lst:
            break
        lst = sorted(lst, key=lambda x:int(x[0]))
        for k in lst:
            rows.append((int(k[0]), float(k[4])))  # ts, close
        last = int(lst[-1][0])
        nxt = last + 86400000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.12)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts","close"]).drop_duplicates("ts").sort_values("ts")
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    df = df[["date","close"]]
    df.to_csv(f, index=False)
    return df

def load_panel():
    series = {}
    for s in UNIVERSE:
        df = fetch_daily(s)
        if df is None or len(df) < 200:
            continue
        df = df.set_index("date")["close"]
        series[s] = df
    panel = pd.DataFrame(series).sort_index()
    return panel

def sharpe(daily_ret):
    r = daily_ret.dropna()
    if r.std() == 0 or len(r) < 30:
        return np.nan
    return r.mean()/r.std()*math.sqrt(365)

def maxdd(daily_ret):
    eq = (1+daily_ret.fillna(0)).cumprod()
    return (eq/eq.cummax()-1).min()

def by_year(daily_ret):
    out = {}
    for y,g in daily_ret.groupby(daily_ret.index.year):
        out[y] = round(sharpe(g),2)
    return out

# block bootstrap (weekly blocks) CI on Sharpe
def boot_ci(daily_ret, block=7, n=2000):
    r = daily_ret.dropna().values
    if len(r) < 60: return (np.nan,np.nan)
    nb = int(np.ceil(len(r)/block))
    rng = np.random.default_rng(42)
    out = []
    for _ in range(n):
        idx = rng.integers(0, len(r)-block, nb)
        samp = np.concatenate([r[i:i+block] for i in idx])[:len(r)]
        if samp.std()>0:
            out.append(samp.mean()/samp.std()*math.sqrt(365))
    return (round(np.percentile(out,2.5),2), round(np.percentile(out,97.5),2))

def run_tsmom(panel, L, cost=0.00055, voltarget=False):
    rets = panel.pct_change()
    # signal: sign of L-day return, lagged 1 day (decide on close, trade next day)
    sig = np.sign(panel.pct_change(L)).shift(1)
    if voltarget:
        vol = rets.rolling(30).std().shift(1)
        w = sig / vol
        w = w.div(w.abs().sum(axis=1).replace(0,np.nan), axis=0)  # gross=1
    else:
        # equal weight across active names, long/short net
        active = sig.abs()
        w = sig.div(active.sum(axis=1).replace(0,np.nan), axis=0)
    w = w.fillna(0)
    port = (w * rets).sum(axis=1)
    # turnover cost
    dw = w.diff().abs().sum(axis=1)
    port = port - dw*cost
    return port

def bh(panel, cost=0.00055):
    rets = panel.pct_change()
    n = panel.notna().sum(axis=1).replace(0,np.nan)
    w = panel.notna().astype(float).div(n, axis=0)
    port = (w*rets).sum(axis=1)
    dw = w.diff().abs().sum(axis=1)
    return port - dw*cost

def report(name, port):
    print(f"\n== {name} ==")
    print(f"  Sharpe {sharpe(port):.2f}  maxDD {maxdd(port)*100:.0f}%  "
          f"annRet {((1+port.fillna(0)).prod()**(365/len(port))-1)*100:.0f}%")
    print(f"  bootCI Sharpe {boot_ci(port)}")
    print(f"  by-year {by_year(port)}")

if __name__ == "__main__":
    print("loading panel...")
    panel = load_panel()
    print(f"panel: {panel.shape[1]} symbols, {panel.index.min().date()}..{panel.index.max().date()}, {len(panel)} days")
    print(f"symbols: {list(panel.columns)}")

    full = panel
    sub = panel[panel.index >= "2024-01-01"]   # exclude 2023
    sub25 = panel[panel.index >= "2025-01-01"]  # 2025-2026 only (toughest)

    for label, p in [("FULL 2023-2026", full), ("EX-2023 (2024-2026)", sub), ("2025-2026 only", sub25)]:
        print("\n" + "#"*60)
        print(f"WINDOW: {label}  ({p.index.min().date()}..{p.index.max().date()})")
        report("buy&hold EW", bh(p))
        for L in [30,50]:
            report(f"TSMOM L={L}", run_tsmom(p,L))
        report("TSMOM L=50 vol-target", run_tsmom(p,50,voltarget=True))
