import numpy as np, pandas as pd
px=pd.read_csv('research/daily_closes.csv',index_col=0,parse_dates=True)
ret=px.pct_change()
COST=0.00055; ANN=365**0.5

def stats(pnl):
    pnl=pnl.dropna()
    sh=pnl.mean()/pnl.std()*ANN
    eq=(1+pnl).cumprod()
    dd=(eq/eq.cummax()-1).min()
    ann=eq.iloc[-1]**(365/len(pnl))-1
    return sh,ann,dd,eq.iloc[-1]-1

def sig_of(ret,L):
    s=np.sign((1+ret).rolling(L).apply(lambda x:np.prod(x)-1,raw=True)).shift(1)
    return s

def pnl_from_w(w,ret):
    active=w.abs().sum(axis=1).replace(0,np.nan)
    wn=w.div(active,axis=0)
    gross=(wn*ret).sum(axis=1)
    turn=(wn-wn.shift(1)).abs().sum(axis=1)
    return gross-turn*COST

# 1) ENSEMBLE: average signal across many L -> avoids picking the lucky L
Ls=[20,40,60,90,120]
ens=sum(sig_of(ret,L) for L in Ls)/len(Ls)
ens_w=np.sign(ens)  # net direction from ensemble vote
p_ens=pnl_from_w(ens_w,ret)
sh,ann,dd,tot=stats(p_ens)
print(f"ENSEMBLE L={Ls} (no single-L cherry-pick): Sharpe {sh:.2f} ann {ann*100:.1f}% dd {dd*100:.1f}% tot {tot*100:.0f}%")
for y,g in p_ens.groupby(p_ens.index.year):
    s=stats(g); print(f"   {y}: Sharpe {s[0]:.2f}")

# 2) BLOCK BOOTSTRAP CI on the ensemble Sharpe (monthly blocks -> cluster, not iid)
print("\nBlock-bootstrap (monthly blocks, 2000 resamples) on ENSEMBLE Sharpe:")
p=p_ens.dropna()
months=p.groupby([p.index.year,p.index.month])
blocks=[g.values for _,g in months]
rng=np.random.default_rng(42)
shs=[]
nb=len(blocks)
for _ in range(2000):
    idx=rng.integers(0,nb,nb)
    cat=np.concatenate([blocks[i] for i in idx])
    shs.append(cat.mean()/cat.std()*ANN)
shs=np.array(shs)
print(f"   Sharpe CI95: [{np.percentile(shs,2.5):.2f}, {np.percentile(shs,97.5):.2f}]  median {np.median(shs):.2f}  P(Sharpe>0)={ (shs>0).mean()*100:.0f}%")

# 3) Robustness to universe: drop biggest contributor + only old majors
old=[c for c in px.columns if px[c].iloc[:5].notna().all() and px[c].iloc[-5:].notna().all()]
print(f"\n{len(old)} symbols survive full window (no delist).")
ens_old=sum(sig_of(ret[old],L) for L in Ls)/len(Ls)
p_old=pnl_from_w(np.sign(ens_old),ret[old])
sh,ann,dd,tot=stats(p_old)
print(f"ENSEMBLE on survivors-only: Sharpe {sh:.2f} ann {ann*100:.1f}% dd {dd*100:.1f}%  (if >> full universe -> survivorship lift)")

# 4) cost sensitivity
print("\nCost sensitivity (ensemble):")
for c in [0.0,0.00055,0.0011,0.002]:
    active=ens_w.abs().sum(axis=1).replace(0,np.nan)
    wn=ens_w.div(active,axis=0)
    gross=(wn*ret).sum(axis=1); turn=(wn-wn.shift(1)).abs().sum(axis=1)
    p=gross-turn*c
    s=stats(p); print(f"   cost {c*100:.3f}%/side: Sharpe {s[0]:.2f} ann {s[1]*100:.1f}%")

# avg turnover
active=ens_w.abs().sum(axis=1).replace(0,np.nan)
wn=ens_w.div(active,axis=0)
turn=(wn-wn.shift(1)).abs().sum(axis=1)
print(f"\navg daily turnover (fraction of book): {turn.mean():.3f}  -> ann round-trips ~{turn.mean()*365:.0f}")
