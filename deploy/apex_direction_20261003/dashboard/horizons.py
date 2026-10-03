"""Versioned descriptive excursions from an immutable event baseline; not PnL."""
import math

STEP=900000


def event_horizons(scenario, bars, now_ms):
    baseline=(scenario or {}).get('baseline') or {}
    price,event=baseline.get('price'),baseline.get('event_ms')
    side=(scenario or {}).get('side')
    result={'method':'event_close_closed15m_v1','baseline':baseline,'side':side,'horizons':[]}
    if side not in ('BUY','SELL'):
        return {**result,'status':'not_directional'}
    if type(event) is not int or type(price) not in (int,float) or not math.isfinite(price) or price<=0:
        return {**result,'status':'baseline_unavailable'}
    start=((event+STEP-1)//STEP)*STEP
    for hours in (1,4,24):
        end=event+hours*3600000
        cutoff=min(now_ms,end)
        expected=list(range(start,(cutoff//STEP)*STEP,STEP))
        unique={};invalid=False
        for bar in bars:
            t=bar['start_ms']
            if t<start or t+STEP>cutoff:continue
            if t%STEP or bar['end_ms']!=t+STEP-1:
                invalid=True;continue
            values=[bar.get(k) for k in ('open','high','low','close')]
            if not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in values):
                invalid=True;continue
            o,h,l,c=values
            if not l<=min(o,c)<=max(o,c)<=h:invalid=True;continue
            if t in unique and any(unique[t].get(k)!=bar.get(k) for k in ('open','high','low','close','end_ms')):
                invalid=True;continue
            unique[t]=bar
        missing=[t for t in expected if t not in unique]
        item={'hours':hours,'event_ms':event,'window_start_ms':start,'window_end_ms':end,
              'observed_through_ms':expected[-1]+STEP if expected else None,
              'observed_bars':len(unique),'expected_closed_bars':len(expected),
              'full_window_bars':max(0,(end//STEP*STEP-start)//STEP),
              'status':'invalid_data' if invalid else 'data_unavailable' if missing else 'complete' if now_ms>=end else 'incomplete',
              'mfe_pct':None,'mae_pct':None,'mfe_price':None,'mae_price':None}
        if not invalid and not missing and unique:
            high=max(price,max(b['high'] for b in unique.values()))
            low=min(price,min(b['low'] for b in unique.values()))
            favorable,adverse=(high,low) if side=='BUY' else (low,high)
            sign=1 if side=='BUY' else -1
            item.update(mfe_pct=sign*100*(favorable/price-1),mae_pct=sign*100*(adverse/price-1),
                        mfe_price=favorable,mae_price=adverse)
        result['horizons'].append(item)
    return {**result,'status':'measured'}
