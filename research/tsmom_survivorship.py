import pandas as pd, numpy as np, glob, os

FILES = sorted(glob.glob('research/daily/*.csv'))
START = pd.Timestamp('2023-09-01')
COST = 0.00055  # per side, per turnover

def load():
    px = {}
    for f in FILES:
        sym = os.path.basename(f).replace('USDT.csv','')
        d = pd.read_csv(f)
        d['dt'] = pd.to_datetime(d['ts'], unit='ms')
        d = d.set_index('dt')['close'].sort_index()
        d = d[~d.index.duplicated()]
        px[sym] = d
    return pd.DataFrame(px)

def tsmom_returns(prices, L=50, voltgt=False):
    """Daily binary TSMOM: sign of L-day return -> position next day. Equal weight across active syms.
    Returns daily net portfolio return series (after costs on turnover)."""
    prices = prices[prices.index >= (START - pd.Timedelta(days=L+5))]
    rets = prices.pct_change()
    signal = np.sign(prices.pct_change(L))  # -1/0/+1, known at close of day t
    pos = signal.shift(1)  # trade next day
    if voltgt:
        vol = rets.rolling(20).std()
        w = (1.0/vol).replace([np.inf,-np.inf],np.nan)
        pos = pos * w
    # restrict to live window
    mask = prices.index >= START
    pos = pos[mask]; rets = rets[mask]
    # per-symbol gross
    gross = pos * rets
    # turnover per symbol = |pos_t - pos_{t-1}|
    dpos = pos.diff().abs()
    # normalize weights so portfolio is equal-risk: divide by count of active each day
    active = pos.abs().gt(0) | pos.notna()
    nactive = pos.notna().sum(axis=1).replace(0, np.nan)
    if voltgt:
        # normalize so sum of |weights| = 1 each day (gross leverage 1)
        norm = pos.abs().sum(axis=1).replace(0, np.nan)
        port_gross = (gross.div(norm, axis=0)).sum(axis=1)
        port_cost = (dpos.div(norm, axis=0)).sum(axis=1) * COST
    else:
        port_gross = (gross.div(nactive, axis=0)).sum(axis=1)
        port_cost = (dpos.div(nactive, axis=0)).sum(axis=1) * COST
    net = (port_gross - port_cost).fillna(0)
    return net

def stats(net):
    ann = net.mean()*365
    vol = net.std()*np.sqrt(365)
    sharpe = ann/vol if vol>0 else 0
    eq = (1+net).cumprod()
    dd = (eq/eq.cummax()-1).min()
    total = eq.iloc[-1]-1
    return dict(sharpe=round(sharpe,2), annRet=round(ann,3), maxDD=round(dd,3), total=round(total,3))

def by_year(net):
    out={}
    for y,g in net.groupby(net.index.year):
        s=g.mean()*365; v=g.std()*np.sqrt(365)
        out[y]=round(s/v,2) if v>0 else 0
    return out

def block_bootstrap_sharpe(net, block=20, n=2000, seed=1):
    """Stationary-ish block bootstrap CI on annualized Sharpe (clustered via blocks)."""
    rng=np.random.default_rng(seed)
    x=net.values; T=len(x); nb=int(np.ceil(T/block))
    sh=[]
    for _ in range(n):
        starts=rng.integers(0,T-block,size=nb)
        idx=np.concatenate([np.arange(s,s+block) for s in starts])[:T]
        b=x[idx]
        m=b.mean()*365; v=b.std()*np.sqrt(365)
        sh.append(m/v if v>0 else 0)
    return np.percentile(sh,[2.5,50,97.5])

if __name__=='__main__':
    P = load()
    print('panel symbols:', P.shape[1], 'window', P[P.index>=START].index.min().date(), P.index.max().date())
    print('='*60)
    for L in [30,50]:
        net=tsmom_returns(P,L=L)
        print(f'TSMOM binary L={L}:', stats(net), 'by_year', by_year(net))
    net50=tsmom_returns(P,L=50)
    print('  block-boot Sharpe CI L=50 [2.5,50,97.5]:', np.round(block_bootstrap_sharpe(net50),3))
    netvt=tsmom_returns(P,L=50,voltgt=True)
    print('TSMOM voltgt L=50:', stats(netvt),'by_year',by_year(netvt))
    # benchmark
    rets=P[P.index>=START].pct_change()
    bench=rets.mean(axis=1).fillna(0)
    print('BENCHMARK eq-weight buy&hold:', stats(bench),'by_year',by_year(bench))
