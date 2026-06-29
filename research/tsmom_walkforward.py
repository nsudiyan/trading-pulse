"""
Adversarial TSMOM verdict: kill the L=50 selection-bias.
- Fetch daily klines from Bybit V5 for a fixed liquid universe (no new-token survivorship).
- Walk-forward: choose L on first half by Sharpe, test on second half (true OOS).
- Sensitivity: rebalance freq, threshold band, cost level.
- Cluster (block) CI on daily strategy returns + by-year Sharpe.
All costs 0.055%/side on turnover. Numbers from real fetched data only.
"""
import time, requests, numpy as np, pandas as pd, sys

BASE = "https://api.bybit.com/v5/market/kline"
# Universe chosen to exist for most of 2023-09..2026-06 (avoid 2024-25 new listings = survivorship).
# These are large-cap perps listed on Bybit well before 2023-09.
UNIV = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","BNBUSDT","LTCUSDT",
        "LINKUSDT","AVAXUSDT","DOTUSDT","MATICUSDT","TRXUSDT","ATOMUSDT","UNIUSDT","XLMUSDT",
        "NEARUSDT","FILUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT","SEIUSDT",
        "AAVEUSDT","ETCUSDT","BCHUSDT","ICPUSDT","RUNEUSDT","FTMUSDT","ALGOUSDT","SANDUSDT",
        "MANAUSDT","AXSUSDT","GALAUSDT","EOSUSDT","THETAUSDT","FLOWUSDT","CRVUSDT"]

START = pd.Timestamp("2023-09-01", tz="UTC")
COST = 0.00055  # per side

def fetch_daily(sym):
    start_ms = int(START.timestamp()*1000)
    end_ms = int(time.time()*1000)
    rows = []
    cur = end_ms
    while True:
        params = {"category":"linear","symbol":sym,"interval":"D","start":start_ms,"end":cur,"limit":1000}
        for attempt in range(4):
            try:
                r = requests.get(BASE, params=params, timeout=20).json()
                break
            except Exception:
                time.sleep(1.5)
        else:
            return None
        if r.get("retCode")!=0:
            return None
        lst = r["result"]["list"]
        if not lst:
            break
        rows += lst
        oldest = int(lst[-1][0])
        if oldest <= start_ms or len(lst)<1000:
            break
        cur = oldest-1
        time.sleep(0.12)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts","o","h","l","c","v","to"])
    df["ts"]=pd.to_datetime(df["ts"].astype(np.int64),unit="ms",utc=True)
    df["c"]=df["c"].astype(float)
    df=df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    df=df[df.index>=START]
    return df["c"]

print("Fetching daily closes...", file=sys.stderr)
closes={}
for s in UNIV:
    c=fetch_daily(s)
    if c is not None and len(c)>400:
        closes[s]=c
        print(f"  {s}: {len(c)} days {c.index[0].date()}..{c.index[-1].date()}", file=sys.stderr)
    else:
        print(f"  {s}: SKIP ({0 if c is None else len(c)})", file=sys.stderr)

px = pd.DataFrame(closes).sort_index()
px.to_csv("research/daily_closes.csv")
print(f"\nUniverse usable: {px.shape[1]} symbols, {px.shape[0]} days", file=sys.stderr)

ret = px.pct_change()  # daily simple returns
N = px.shape[0]

def tsmom_returns(L, rebal=1, thresh=0.0, cost=COST, voltarget=False):
    """Binary TSMOM: position = sign(L-day return), equal weight across symbols with signal.
    rebal: hold position rebal days (signal recomputed every rebal days).
    thresh: deadband on L-day return magnitude to take a position.
    Returns daily net strategy return series."""
    mom = px / px.shift(L) - 1.0
    sig = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    sig[mom > thresh] = 1.0
    sig[mom < -thresh] = -1.0
    # apply rebalance: only update signal every `rebal` days
    if rebal>1:
        mask = np.zeros(len(sig), dtype=bool)
        mask[::rebal]=True
        sig = sig.where(pd.Series(mask, index=sig.index), np.nan).ffill()
    sig = sig.shift(1)  # trade next day open ~ use prev close signal, no look-ahead
    if voltarget:
        vol = ret.rolling(30).std()
        w = (sig / vol).replace([np.inf,-np.inf],np.nan)
        w = w.div(w.abs().sum(axis=1), axis=0).fillna(0)
    else:
        n = sig.abs().sum(axis=1).replace(0,np.nan)
        w = sig.div(n, axis=0).fillna(0)
    gross = (w * ret).sum(axis=1)
    turnover = (w - w.shift(1)).abs().sum(axis=1)
    net = gross - turnover*cost
    return net.dropna()

def sharpe(r):
    if r.std()==0 or len(r)<10: return np.nan
    return r.mean()/r.std()*np.sqrt(365)

def maxdd(r):
    eq=(1+r).cumprod()
    return (eq/eq.cummax()-1).min()

def block_boot_sharpe_ci(r, block=20, B=2000, seed=1):
    rng=np.random.default_rng(seed)
    arr=r.values; n=len(arr); nb=int(np.ceil(n/block))
    out=[]
    for _ in range(B):
        idx=rng.integers(0,n-block,nb)
        samp=np.concatenate([arr[i:i+block] for i in idx])[:n]
        s=samp.std()
        out.append(samp.mean()/s*np.sqrt(365) if s>0 else 0)
    return np.percentile(out,[2.5,50,97.5])

def by_year(r):
    return {int(y): round(sharpe(g),2) for y,g in r.groupby(r.index.year)}

print("\n"+"="*70)
print("WALK-FORWARD (independent L selection)")
print("="*70)
# Split in half by time
mid = px.index[N//2]
print(f"Train: {px.index[0].date()}..{mid.date()}  |  Test: {mid.date()}..{px.index[-1].date()}")
Ls=[10,20,30,40,50,60,75,90,120]
print("\nL-grid Sharpe on TRAIN half (this is where selection happens):")
train_sh={}
for L in Ls:
    r=tsmom_returns(L)
    rt=r[r.index<=mid]
    train_sh[L]=sharpe(rt)
    print(f"  L={L:3d}  train Sharpe {train_sh[L]:+.3f}")
bestL=max(train_sh, key=lambda k: (train_sh[k] if not np.isnan(train_sh[k]) else -9))
print(f"\n>>> L chosen on TRAIN ONLY: L={bestL} (Sharpe {train_sh[bestL]:+.3f})")

r_full=tsmom_returns(bestL)
r_test=r_full[r_full.index>mid]
sh_test=sharpe(r_test)
ci=block_boot_sharpe_ci(r_test)
print(f"\n>>> OOS TEST Sharpe (L={bestL}): {sh_test:+.3f}")
print(f"    Block-boot 95% CI: [{ci[0]:+.2f}, {ci[2]:+.2f}]  median {ci[1]:+.2f}")
print(f"    OOS maxDD: {maxdd(r_test):.1%}   by-year: {by_year(r_test)}")

# benchmark buy&hold on test
bh=ret.mean(axis=1)
bh_test=bh[bh.index>mid].dropna()
print(f"\n    Buy&Hold EW on TEST: Sharpe {sharpe(bh_test):+.3f}  maxDD {maxdd(bh_test):.1%}  by-year {by_year(bh_test)}")

print("\n"+"="*70)
print("SENSITIVITY (full sample, L=bestL) — robustness of the knob")
print("="*70)
for reb in [1,2,5]:
    r=tsmom_returns(bestL, rebal=reb)
    print(f"  rebal={reb}d: Sharpe {sharpe(r):+.3f} maxDD {maxdd(r):.1%}")
for th in [0.0,0.02,0.05,0.10]:
    r=tsmom_returns(bestL, thresh=th)
    print(f"  thresh={th:.2f}: Sharpe {sharpe(r):+.3f} maxDD {maxdd(r):.1%}")
for cm in [COST, COST*2, COST*4]:
    r=tsmom_returns(bestL, cost=cm)
    print(f"  cost={cm*100:.3f}%/side: Sharpe {sharpe(r):+.3f}")
r_vt=tsmom_returns(bestL, voltarget=True)
print(f"  vol-targeted: Sharpe {sharpe(r_vt):+.3f} maxDD {maxdd(r_vt):.1%} by-year {by_year(r_vt)}")

print("\n"+"="*70)
print("ALL-L OOS (did ANY L survive, or is it all noise?)")
print("="*70)
for L in Ls:
    r=tsmom_returns(L)
    rt=r[r.index>mid]
    print(f"  L={L:3d}: train {sharpe(r[r.index<=mid]):+.2f} | OOS {sharpe(rt):+.2f}")

print("\n"+"="*70)
print("FULL-SAMPLE bestL summary + cluster CI")
print("="*70)
ci_f=block_boot_sharpe_ci(r_full)
print(f"  L={bestL} full: Sharpe {sharpe(r_full):+.3f} CI[{ci_f[0]:+.2f},{ci_f[2]:+.2f}] maxDD {maxdd(r_full):.1%} by-year {by_year(r_full)}")
print(f"  Buy&Hold full: Sharpe {sharpe(bh.dropna()):+.3f} maxDD {maxdd(bh.dropna()):.1%}")
