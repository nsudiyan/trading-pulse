"""TSMOM verification: re-download daily, test L-robustness, survivorship, costs, by-year cluster.
Skeptic mode. No look-ahead: signal from sign of return over [t-L, t-1], applied to ret[t->t+1].
"""
import requests, time, sys
import numpy as np, pandas as pd

# Broad liquid universe that plausibly existed 2023-09. We download all; backtest
# handles entry/exit by listing date (NaN before listed) -> no survivorship injection
# beyond "was tradeable then". We test both a fixed-old-majors set and full set.
SYMS = ("BTCUSDT ETHUSDT SOLUSDT XRPUSDT BNBUSDT ADAUSDT DOGEUSDT AVAXUSDT LINKUSDT "
        "DOTUSDT MATICUSDT LTCUSDT BCHUSDT TRXUSDT ATOMUSDT UNIUSDT XLMUSDT NEARUSDT "
        "APTUSDT ARBUSDT OPUSDT FILUSDT INJUSDT SUIUSDT SEIUSDT TIAUSDT AAVEUSDT "
        "MKRUSDT RUNEUSDT ALGOUSDT FTMUSDT SANDUSDT MANAUSDT AXSUSDT EGLDUSDT "
        "THETAUSDT EOSUSDT GALAUSDT CHZUSDT IMXUSDT").split()

def fetch(sym):
    out=[]
    end=int(time.time()*1000)
    for _ in range(20):
        r=requests.get('https://api.bybit.com/v5/market/kline',
            params={'category':'linear','symbol':sym,'interval':'D','limit':1000,'end':end},timeout=20)
        j=r.json()
        if j.get('retCode')!=0: break
        lst=j['result']['list']
        if not lst: break
        out+=lst
        oldest=int(lst[-1][0])
        if oldest<=1693526400000: break  # ~2023-09-01
        end=oldest-1
        time.sleep(0.15)
    if not out: return None
    df=pd.DataFrame(out,columns=['ts','o','h','l','c','v','to'])
    df['ts']=pd.to_datetime(df['ts'].astype('int64'),unit='ms')
    for col in ['o','h','l','c','v','to']: df[col]=df[col].astype(float)
    df=df.drop_duplicates('ts').sort_values('ts').set_index('ts')
    return df['c']

print("downloading...",file=sys.stderr)
closes={}
for s in SYMS:
    try:
        c=fetch(s)
        if c is not None and len(c)>200:
            closes[s]=c
            print(f"  {s}: {len(c)} days {c.index[0].date()}->{c.index[-1].date()}",file=sys.stderr)
    except Exception as e:
        print(f"  {s} FAIL {e}",file=sys.stderr)

px=pd.DataFrame(closes)
px=px[px.index>='2023-09-01']
px.to_csv('research/daily_closes.csv')
print(f"\nuniverse={px.shape[1]} symbols, {px.shape[0]} days, {px.index[0].date()}->{px.index[-1].date()}")
print("symbols with full history from start:",
      int((px.iloc[0].notna()).sum()), "/", px.shape[1])
