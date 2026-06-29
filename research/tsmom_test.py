import numpy as np, pandas as pd
px=pd.read_csv('research/daily_closes.csv',index_col=0,parse_dates=True)
ret=px.pct_change()
COST=0.00055  # per side
ANN=365**0.5

def stats(pnl):
    pnl=pnl.dropna()
    if pnl.std()==0 or len(pnl)<30: return dict(sharpe=np.nan,ann=np.nan,dd=np.nan,tot=np.nan,n=len(pnl))
    sh=pnl.mean()/pnl.std()*ANN
    eq=(1+pnl).cumprod()
    dd=(eq/eq.cummax()-1).min()
    ann=eq.iloc[-1]**(365/len(pnl))-1
    return dict(sharpe=sh,ann=ann,dd=dd,tot=eq.iloc[-1]-1,n=len(pnl))

def tsmom(ret,L,voltarget=None,longonly=False):
    # signal = sign of cumulative return over [t-L, t-1], known at close t-1, hold t->t+1
    sig=np.sign((1+ret).rolling(L).apply(lambda x:np.prod(x)-1,raw=True))
    sig=sig.shift(1)  # avoid look-ahead: use info up to yesterday's close
    if longonly: sig=sig.clip(lower=0)
    w=sig.copy()
    if voltarget is not None:
        vol=ret.rolling(30).std()
        w=sig*(voltarget/ (vol*ANN)).clip(upper=3.0)  # cap leverage 3x per name
    # equal weight across active names each day
    active=w.abs().sum(axis=1).replace(0,np.nan)
    wn=w.div(active,axis=0)
    gross=(wn.shift(0)*ret).sum(axis=1)  # wn already known at t-1 (sig shifted)
    turn=(wn-wn.shift(1)).abs().sum(axis=1)
    pnl=gross - turn*COST
    return pnl

def byyear(pnl):
    out={}
    for y,g in pnl.groupby(pnl.index.year):
        s=stats(g)
        out[y]=round(s['sharpe'],2) if not np.isnan(s['sharpe']) else None
    return out

# Benchmark
bh=ret.mean(axis=1)  # equal weight buy&hold
print("BENCHMARK b&h:",{k:round(v,3) if isinstance(v,float) else v for k,v in stats(bh).items()})
print("  byyear:",byyear(bh))
print()

print("== TSMOM L-sweep (binary, full 39 universe incl delisted), net of costs ==")
print(f"{'L':>4} {'Sharpe':>7} {'annRet':>8} {'maxDD':>7} {'total':>8}  byyear")
rows={}
for L in [10,15,20,25,30,40,50,60,75,90,120,150,200]:
    p=tsmom(ret,L)
    s=stats(p); rows[L]=s
    print(f"{L:>4} {s['sharpe']:>7.2f} {s['ann']*100:>7.1f}% {s['dd']*100:>6.1f}% {s['tot']*100:>7.0f}%  {byyear(p)}")

print()
print("== vol-targeted (20% ann, 3x cap/name) ==")
for L in [30,50,75]:
    p=tsmom(ret,L,voltarget=0.20)
    s=stats(p)
    print(f"L={L:<3} Sharpe {s['sharpe']:.2f} ann {s['ann']*100:.1f}% dd {s['dd']*100:.1f}% tot {s['tot']*100:.0f}%  {byyear(p)}")

print()
print("== long-only TSMOM (kills shorts; tests if shorts add or just drag) ==")
for L in [30,50]:
    p=tsmom(ret,L,longonly=True)
    s=stats(p)
    print(f"L={L:<3} Sharpe {s['sharpe']:.2f} ann {s['ann']*100:.1f}% dd {s['dd']*100:.1f}%  {byyear(p)}")
