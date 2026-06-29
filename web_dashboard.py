#!/usr/bin/env python3
"""NEXUS Trading Terminal — web_dashboard.py"""

import json, csv, time, os, subprocess, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# .env loader (креды дашборда DASHBOARD_AUTH; зеркалит claude_realtime_filter._load_dotenv)
def _load_dotenv():
    try:
        p = Path(__file__).parent / ".env"
        if p.exists():
            for _line in p.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass
_load_dotenv()

BASE     = Path(__file__).parent
PENDING  = BASE / "outcomes" / "pending.json"
RESOLVED = BASE / "outcomes" / "resolved.csv"
CHARTS   = BASE / "charts"
LIQ_DB        = BASE / "liquidations.db"
CHANNEL_CACHE = BASE / "channel_signals_cache.json"
PORT          = 5001

_CACHE     = {"d": None, "ts": 0.0}
_OBS_CACHE = {"d": None, "ts": 0.0}
_LIQ_CACHE = {"d": None, "ts": 0.0}
TTL = 20

# ── loaders ───────────────────────────────────────────────────────────────────

def load_pending():
    try:
        with open(PENDING) as f: data = json.load(f)
        if isinstance(data, list):
            return sorted(data, key=lambda x: x.get("run_ts",""), reverse=True)[:120]
    except Exception: pass
    return []

def load_resolved(limit=2000):
    try:
        rows = []
        with open(RESOLVED, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append({k:v for k,v in row.items() if k})
        return rows[-limit:]
    except Exception: return []

def compute_stats(rows):
    # audit 2026-06-17: считаем ТОЛЬКО на честном окне (≥HONEST_WINDOW_START). До него в
    # outcome-метках both-hit look-ahead → "ALL-TIME WR" был завышен (41.1% vs честных ~35.5%).
    try:
        from outcome_tracker import HONEST_WINDOW_START as _HON
    except Exception:
        _HON = "2026-06-02"
    total=wins=stops=losses=flat=0
    by_setup=defaultdict(lambda:{"t":0,"w":0,"s":0,"l":0})
    by_dir=defaultdict(lambda:{"t":0,"w":0,"s":0,"l":0})
    by_hour=defaultdict(lambda:{"t":0,"w":0,"s":0})
    for r in rows:
        if (r.get("run_ts") or "")[:10] < _HON: continue
        oc=r.get("outcome_24h","")
        if oc not in ("TP1","WIN","STOP","LOSS","FLAT"): continue
        setup=r.get("setup","?"); direction=r.get("direction","?")
        try: hour=datetime.fromisoformat(r["run_ts"]).hour
        except Exception: hour=-1
        is_w=oc in("TP1","WIN"); is_s=oc=="STOP"; is_l=oc=="LOSS"
        total+=1
        if is_w: wins+=1
        elif is_s: stops+=1
        elif is_l: losses+=1
        else: flat+=1
        for d in (by_setup[setup], by_dir[direction]):
            d["t"]+=1
            if is_w: d["w"]+=1
            elif is_s: d["s"]+=1
            elif is_l: d["l"]+=1
        if hour>=0:
            hd=by_hour[hour]; hd["t"]+=1
            if is_w: hd["w"]+=1
            elif is_s: hd["s"]+=1
    def wr(d): return round(d.get("w",0)/d["t"]*100,1) if d.get("t",0)>0 else None
    return {
        "total":total,"wins":wins,"stops":stops,"losses":losses,"flat":flat,
        "wr":wr({"w":wins,"t":total}),
        "by_setup":{k:{**v,"wr":wr(v)} for k,v in by_setup.items()},
        "by_direction":{k:{**v,"wr":wr(v)} for k,v in by_dir.items()},
        "by_hour":{str(k):{**v,"wr":wr(v)} for k,v in sorted(by_hour.items())},
    }

def slim(p):
    keys=("symbol","direction","setup","score","grade","pump_score",
          "price_entry","stop","tp1","tp2","funding","oi_24h_pct",
          "mtf_bull","mtf_bear","rsi_1h","cvd_kline","cvd_trade",
          "btc_trend_4h","alt_breadth_pct","in_zone","whale_flag",
          "run_ts","outcome_4h","outcome_24h","change_24h_pct",
          "vwap_dev","choch_bull_1h",
          "sc_squeeze","sc_bos_fvg","sc_short_dist","sc_breakout","sc_range_sweep",
          "ema_bull_1h","ema_bull_4h","rs_btc","bnb_cross_bonus",
          "listing_age_days","utc_hour","avg_vol_7d_usd")
    return {k: p.get(k) for k in keys}

def load_channel_map():
    """Returns {symbol: {'LONG': [ch,...], 'SHORT': [ch,...]}} from channel_signals_cache.json (max 24h old)."""
    try:
        if not CHANNEL_CACHE.exists(): return {}
        data = json.loads(CHANNEL_CACHE.read_text(encoding="utf-8"))
        ts_str = data.get("ts", "")
        if ts_str:
            ts = datetime.fromisoformat(ts_str).replace(tzinfo=None)
            if (datetime.now() - ts).total_seconds() > 86400: return {}
        sym_map = {}
        for channel, items in data.get("results", {}).items():
            for item in items:
                if item.get("type") != "signal": continue
                sym = item.get("symbol", "")
                direction = item.get("direction", "")
                if sym and direction in ("LONG", "SHORT"):
                    sym_map.setdefault(sym, {"LONG": [], "SHORT": []})
                    if channel not in sym_map[sym][direction]:
                        sym_map[sym][direction].append(channel)
        return sym_map
    except Exception: return {}

def svc_ok(name):
    try:
        import re
        r=subprocess.run(["launchctl","list",name],capture_output=True,text=True,timeout=2)
        if r.returncode!=0: return False
        # launchctl list <name> → plist dict; служба «живая» только если есть числовой PID
        # (раньше парсили как таблицу → всегда False, все демоны ложно DOWN)
        return re.search(r'"PID"\s*=\s*(\d+)', r.stdout) is not None
    except Exception: return False

def load_pump_signals(limit=30):
    """Памп/раг, реально отправленные в TG (pump_pending.json). pump=LONG, rug_prep=SHORT."""
    try:
        p = BASE / "outcomes" / "pump_pending.json"
        if not p.exists(): return []
        data = json.loads(p.read_text(encoding="utf-8"))
        out=[]
        for x in (data or [])[-limit:]:
            st=x.get("signal_type","")
            try: rt=datetime.fromtimestamp(x.get("ts",0),timezone.utc).strftime("%Y-%m-%dT%H:%M")
            except Exception: rt=""
            out.append({"symbol":x.get("symbol"),"dir":"LONG" if st=="pump" else "SHORT",
                        "label":"PUMP" if st=="pump" else "RUG","source":"pump",
                        "score":x.get("score"),"entry":x.get("price"),
                        "funding":x.get("funding"),"run_ts":rt})
        return out
    except Exception: return []

def build_tg_signals(active):
    """Унифицированная LONG/SHORT лента того, что уходит в TG: скринер + памп/раг."""
    out=[]
    for s in active:
        dr=s.get("direction")
        dd="LONG" if dr in ("ЛОНГ","LONG") else "SHORT" if dr in ("ШОРТ","SHORT") else None
        if not dd: continue
        out.append({"symbol":s.get("symbol"),"dir":dd,"label":(s.get("setup") or "").upper(),
                    "source":"screener","score":s.get("score"),"entry":s.get("price_entry"),
                    "funding":s.get("funding"),"run_ts":s.get("run_ts")})
    out += load_pump_signals()
    return out

def build_data():
    now=time.time()
    if _CACHE["d"] and now-_CACHE["ts"]<TTL: return _CACHE["d"]
    pending=load_pending(); resolved=load_resolved()
    active=[p for p in pending if not p.get("outcome_24h")]
    top=sorted(pending,key=lambda x:x.get("score",0),reverse=True)[:35]
    cutoff24=(datetime.now(timezone.utc)-timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    rows_24h=[r for r in resolved if r.get("run_ts","")>=cutoff24]
    recent_keys=("symbol","direction","setup","score","outcome_4h","outcome_24h","run_ts","price_entry","stop","tp1","change_24h_pct")
    recent=[{k:r.get(k) for k in recent_keys} for r in resolved[-60:]]
    market={}
    if pending:
        market={"btc_trend_4h":pending[0].get("btc_trend_4h","unknown"),
                "alt_breadth_pct":pending[0].get("alt_breadth_pct"),
                "last_scan":pending[0].get("run_ts","")}
    try:
        charts=sorted(CHARTS.glob("*.png"),key=lambda f:f.stat().st_mtime,reverse=True)
        chart_names=[f.name for f in charts[:24]]
    except Exception: chart_names=[]
    ch_map = load_channel_map()
    def add_ch(s):
        sym = s.get("symbol",""); dirs = ch_map.get(sym, {})
        scr_dir = "LONG" if s.get("setup","") in ("squeeze","breakout") else "SHORT"
        opp = "SHORT" if scr_dir=="LONG" else "LONG"
        s["channel_conf"]     = dirs.get(scr_dir, [])
        s["channel_conflict"] = dirs.get(opp, [])
        s["channel_score"]    = len(s["channel_conf"])
        return s
    active_slim=[add_ch(slim(p)) for p in active[:25]]
    d={
        "timestamp":datetime.now(timezone.utc).isoformat(),
        "market":market,
        "active_signals":active_slim,
        "tg_signals":build_tg_signals(active_slim),
        "top_candidates":[add_ch(slim(p)) for p in top],
        "recent_resolved":recent,
        "stats":compute_stats(resolved),
        "stats_24h":compute_stats(rows_24h),
        "charts":chart_names,
        "services":{"pumpdetector":svc_ok("com.trading.pumpdetector"),"screener":svc_ok("com.trading.screener"),
                    "bot":svc_ok("com.trading.bot"),"channelreader":svc_ok("com.trading.channelreader"),
                    "liqtracker":svc_ok("com.trading.liqtracker")},
        "filters":{"f5_btc_above_block":True,"f6_short_dist_below":True,"f2_between_penalty_30":True},
    }
    _CACHE["d"]=d; _CACHE["ts"]=now; return d

_RCA_CACHE={"d":None,"ts":0}
def build_selfanalysis():
    """Самоанализ сделок из rca_results.json: краткие заметки-инсайты + последние разборы."""
    now=time.time()
    if _RCA_CACHE["d"] and now-_RCA_CACHE["ts"]<300: return _RCA_CACHE["d"]
    from collections import Counter
    try:
        p=BASE/"outcomes"/"rca_results.json"
        rca=json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception: rca=[]
    res={"notes":[],"recent":[],"counts":{}}
    if rca:
        WIN=("WIN","TP1"); LOSS=("LOSS","STOP")
        los=[r for r in rca if r.get("outcome_category") in LOSS]
        win=[r for r in rca if r.get("outcome_category") in WIN]
        flat=[r for r in rca if r.get("outcome_category")=="FLAT"]
        res["counts"]={"total":len(rca),"win":len(win),"loss":len(los),"flat":len(flat)}
        ss=defaultdict(lambda:[0,0])
        for r in rca:
            oc=r.get("outcome_category")
            if oc in WIN+LOSS+("FLAT",):
                ss[r.get("setup","?")][0]+=1
                if oc in WIN: ss[r.get("setup","?")][1]+=1
        setups=sorted([(k,v[1]/v[0]*100,v[0]) for k,v in ss.items() if v[0]>=20],key=lambda x:-x[1])
        notes=[]
        if setups:
            b=setups[0]; w=setups[-1]
            notes.append({"icon":"🏆","title":"Лучший сетап","text":f"{b[0]} — винрейт {b[1]:.0f}% (n={b[2]})","tone":"good"})
            notes.append({"icon":"🔻","title":"Худший сетап","text":f"{w[0]} — винрейт {w[1]:.0f}% (n={w[2]})","tone":"bad"})
        if los:
            cause=Counter(r.get("primary_cause") for r in los if r.get("primary_cause"))
            if cause:
                c,n=cause.most_common(1)[0]
                notes.append({"icon":"⚠️","title":"Главная причина лоссов","text":f"{c} — {n}× из {len(los)} убытков","tone":"bad"})
            tags=Counter(t for r in los for t in (r.get("tags") or []))
            if tags:
                notes.append({"icon":"🏷","title":"Топ-паттерны провалов","text":", ".join(f"{t} ({n})" for t,n in tags.most_common(3)),"tone":"warn"})
                if tags.get("HIGH_SCORE",0)>50:
                    notes.append({"icon":"📊","title":"Score не разделяет","text":f"HIGH_SCORE — в {tags['HIGH_SCORE']} лоссах. Высокий score ≠ вин","tone":"warn"})
        notes.append({"icon":"📈","title":"Итог по выборке","text":f"{len(win)}W / {len(los)}L / {len(flat)} FLAT из {len(rca)} разобранных","tone":"neutral"})
        res["notes"]=notes
        res["recent"]=[{"symbol":r.get("symbol"),"setup":r.get("setup"),"direction":r.get("direction"),
                        "outcome":r.get("outcome_category"),"cause":r.get("primary_cause"),
                        "score":r.get("score"),"chg":r.get("price_change_pct"),"run_ts":r.get("run_ts")}
                       for r in rca[-15:][::-1]]
    _RCA_CACHE["d"]=res; _RCA_CACHE["ts"]=now
    return res

def load_liq_data():
    now=time.time()
    if _LIQ_CACHE["d"] and now-_LIQ_CACHE["ts"]<30: return _LIQ_CACHE["d"]
    if not LIQ_DB.exists(): return {"error":"liquidations.db not found"}
    con=None
    try:
        con=sqlite3.connect(str(LIQ_DB)); now_ms=int(now*1000)
        periods={"1h":3600000,"4h":14400000,"24h":86400000}
        by_period={}
        for name,ms in periods.items():
            cutoff=now_ms-ms
            rows=con.execute(
                "SELECT symbol,side,COUNT(*),SUM(usd) FROM liquidations WHERE ts>? "
                "GROUP BY symbol,side ORDER BY SUM(usd) DESC LIMIT 20",(cutoff,)
            ).fetchall()
            by_period[name]=[{"symbol":r[0],"side":r[1],"count":r[2],"usd":round(r[3] or 0,2)} for r in rows]
        # recent large
        cutoff4=now_ms-14400000
        recent=con.execute(
            "SELECT ts,symbol,side,price,qty,usd FROM liquidations "
            "WHERE ts>? ORDER BY usd DESC LIMIT 40",(cutoff4,)
        ).fetchall()
        recent_large=[{"ts":r[0],"symbol":r[1],"side":r[2],"price":r[3],"qty":r[4],"usd":round(r[5] or 0,2)} for r in recent]
        # totals
        totals={}
        for name,ms in periods.items():
            cutoff=now_ms-ms
            row=con.execute("SELECT SUM(usd),COUNT(*) FROM liquidations WHERE ts>?",(cutoff,)).fetchone()
            long_usd=con.execute("SELECT SUM(usd) FROM liquidations WHERE ts>? AND side='long_liq'",(cutoff,)).fetchone()[0] or 0
            short_usd=con.execute("SELECT SUM(usd) FROM liquidations WHERE ts>? AND side='short_liq'",(cutoff,)).fetchone()[0] or 0
            totals[name]={"total_usd":round((row[0] or 0),2),"count":row[1],"long_usd":round(long_usd,2),"short_usd":round(short_usd,2)}
        d={"by_period":by_period,"recent_large":recent_large,"totals":totals}
        _LIQ_CACHE["d"]=d; _LIQ_CACHE["ts"]=now; return d
    except Exception as e:
        return {"error":str(e)}
    finally:
        if con is not None:
            con.close()   # FIX 2026-06-02: закрывать и на ошибке (была FD-течь)

def load_obsidian_data():
    now=time.time()
    if _OBS_CACHE["d"] and now-_OBS_CACHE["ts"]<120: return _OBS_CACHE["d"]
    try:
        cfg_path=BASE/"obsidian_config.json"
        if not cfg_path.exists(): return {}
        with open(cfg_path) as f: cfg=json.load(f)
        if not cfg.get("enabled") or not cfg.get("vault_path"): return {}
        vault=Path(cfg["vault_path"])
        trading_root=vault/cfg.get("trading_folder","крипта")
        journal=None
        journal_path=trading_root/cfg.get("journal_file","Сделки (дневник).md")
        if journal_path.exists(): journal=journal_path.read_text(encoding="utf-8")[:10000]
        notes={}
        if trading_root.exists():
            md_files=sorted(trading_root.rglob("*.md"),key=lambda f:f.stat().st_mtime,reverse=True)
            for md in md_files[:8]:
                try:
                    rel=str(md.relative_to(trading_root))
                    notes[rel]=md.read_text(encoding="utf-8")[:3000]
                except Exception: pass
        d={"journal":journal,"notes":notes}
        _OBS_CACHE["d"]=d; _OBS_CACHE["ts"]=now; return d
    except Exception: return {}

# ── HTTP server ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _auth_ok(self):
        import base64
        # Basic-auth: креды из .env (DASHBOARD_AUTH=user:pass, .env gitignored). Фолбэк на старые,
        # если переменная не задана. FIX 2026-06-01: убрали захардкоженный nikita:1980 из кода (он в git).
        creds = os.environ.get("DASHBOARD_AUTH")
        if not creds:   # FIX 2026-06-02: нет фолбэка на публичные nikita:1980 — нет креды → недоступно
            creds = os.urandom(16).hex()
        want = "Basic " + base64.b64encode(creds.encode()).decode()
        if self.headers.get("Authorization","") == want:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ALPHA-7"')
        self.send_header("Content-Length","0")
        self.end_headers()
        return False
    def do_GET(self):
        if not self._auth_ok(): return
        path=urlparse(self.path).path
        if path in ("/","/index.html"): self._html(HTML)
        elif path in ("/alpha7","/alpha","/a7"):
            try: self._html((Path(__file__).parent/"dashboard_alpha7.html").read_text(encoding="utf-8"))
            except Exception as e: self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())
        elif path=="/api/data": self._json(build_data())
        elif path=="/api/rca": self._json(build_selfanalysis())
        elif path=="/api/liq": self._json(load_liq_data())
        elif path=="/api/obsidian": self._json(load_obsidian_data())
        elif path.startswith("/charts/"): self._chart(path[8:])
        else: self.send_response(404); self.end_headers()
    def _html(self,body):
        b=body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _json(self,data):
        b=json.dumps(data,ensure_ascii=False,default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(b)))
        self.send_header("Cache-Control","no-cache")
        self.end_headers(); self.wfile.write(b)
    def _chart(self,name):
        if not name.endswith(".png") or "/" in name or ".." in name:
            self.send_response(403); self.end_headers(); return
        p=CHARTS/name
        if not p.exists(): self.send_response(404); self.end_headers(); return
        b=p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type","image/png")
        self.send_header("Content-Length",str(len(b)))
        self.send_header("Cache-Control","max-age=300")
        self.end_headers(); self.wfile.write(b)

class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads=True

# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS — Trading Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#020812;--bg2:rgba(8,22,55,.88);
  --cyan:#00d4ff;--cyan-d:rgba(0,212,255,.13);--cyan-g:rgba(0,212,255,.32);
  --green:#00ff9d;--green-d:rgba(0,255,157,.11);
  --red:#ff3366;--red-d:rgba(255,51,102,.11);
  --amber:#ffb800;--amber-d:rgba(255,184,0,.11);
  --purple:#9d5cff;--purple-d:rgba(157,92,255,.11);
  --bdr:rgba(0,180,255,.1);--bdr-h:rgba(0,180,255,.28);
  --txt:#d4ecff;--tdim:rgba(180,220,255,.38);--tmid:rgba(180,220,255,.65);
  --mono:'JetBrains Mono',monospace;--sans:'Inter',system-ui,sans-serif;
  --sw:210px;--hh:56px;--r:8px;--rs:5px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;background:var(--bg);color:var(--txt);font-family:var(--mono);font-size:12px;overflow:hidden;-webkit-font-smoothing:antialiased}

#aurora{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.al{position:absolute;border-radius:50%;filter:blur(110px);animation:drift linear infinite}
.al1{width:900px;height:600px;background:radial-gradient(ellipse,rgba(0,80,255,.55),transparent 68%);top:-220px;left:-80px;animation-duration:30s;opacity:.07}
.al2{width:650px;height:480px;background:radial-gradient(ellipse,rgba(0,220,180,.55),transparent 68%);top:35%;right:-120px;animation-duration:38s;animation-direction:reverse;opacity:.06}
.al3{width:750px;height:380px;background:radial-gradient(ellipse,rgba(120,0,255,.5),transparent 68%);bottom:-80px;left:28%;animation-duration:44s;opacity:.06}
@keyframes drift{0%{transform:translate(0,0) scale(1)}33%{transform:translate(55px,-38px) scale(1.09)}66%{transform:translate(-38px,28px) scale(.94)}100%{transform:translate(0,0) scale(1)}}
#bg{position:fixed;inset:0;z-index:1;pointer-events:none}
#sl{position:fixed;inset:0;z-index:2;pointer-events:none;background:repeating-linear-gradient(0deg,transparent 0,transparent 2px,rgba(0,0,0,.04) 2px,rgba(0,0,0,.04) 3px)}
#vg{position:fixed;inset:0;z-index:2;pointer-events:none;background:radial-gradient(ellipse at center,transparent 38%,rgba(0,0,10,.72) 100%)}
#app{position:relative;z-index:3;display:flex;flex-direction:column;height:100vh;width:100vw;overflow:hidden}

/* HEADER */
header{flex-shrink:0;height:var(--hh);display:flex;align-items:center;
  background:rgba(2,8,24,.96);border-bottom:1px solid var(--bdr);backdrop-filter:blur(20px);position:relative;overflow:hidden}
header::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent 0%,var(--cyan) 30%,var(--purple) 70%,transparent 100%);opacity:.4}
.logo-block{width:var(--sw);flex-shrink:0;display:flex;align-items:center;padding:0 16px;border-right:1px solid var(--bdr);height:100%}
.logo{font-size:16px;font-weight:700;letter-spacing:5px;background:linear-gradient(135deg,var(--cyan) 0%,var(--purple) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;filter:drop-shadow(0 0 18px rgba(0,180,255,.45))}
.logo-sub{font-size:6px;letter-spacing:3px;color:var(--tdim);margin-top:2px}
.hdr-metrics{display:flex;align-items:center;flex:1;overflow:hidden}
.hm{display:flex;flex-direction:column;padding:0 14px;border-right:1px solid var(--bdr);min-width:76px;flex-shrink:0}
.hm-l{font-size:6px;letter-spacing:2px;color:var(--tdim);text-transform:uppercase;margin-bottom:2px}
.hm-v{font-size:13px;font-weight:700;color:var(--txt);letter-spacing:.5px}
.hdr-r{display:flex;align-items:center;gap:12px;padding:0 14px;flex-shrink:0}
#clk{font-size:13px;font-weight:600;color:var(--cyan);letter-spacing:2px}
.ldot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:lpulse 2.2s ease-in-out infinite}
@keyframes lpulse{0%,100%{opacity:1;box-shadow:0 0 8px var(--green),0 0 18px rgba(0,255,157,.28)}50%{opacity:.38;box-shadow:0 0 3px var(--green)}}

/* LAYOUT */
.body{flex:1;display:flex;overflow:hidden;min-height:0}

/* SIDEBAR */
aside{width:var(--sw);flex-shrink:0;display:flex;flex-direction:column;background:rgba(2,8,22,.94);border-right:1px solid var(--bdr);overflow-y:auto;overflow-x:hidden}
aside::-webkit-scrollbar{width:2px}
aside::-webkit-scrollbar-thumb{background:var(--bdr-h)}
.ns{padding:10px 12px 3px;font-size:6px;letter-spacing:3px;color:var(--tdim);text-transform:uppercase}
.ni{display:flex;align-items:center;gap:8px;padding:8px 13px;cursor:pointer;font-size:10px;font-weight:500;color:var(--tmid);border-left:2px solid transparent;transition:all .16s}
.ni:hover{color:var(--txt);background:rgba(0,180,255,.04)}
.ni.active{color:var(--cyan);background:rgba(0,180,255,.08);border-left-color:var(--cyan)}
.ni-ic{font-size:12px;width:16px;text-align:center}
.nb{margin-left:auto;background:var(--cyan-d);border:1px solid rgba(0,180,255,.22);color:var(--cyan);font-size:7px;padding:1px 5px;border-radius:8px}
.ssep{height:1px;background:var(--bdr);margin:6px 0}
.svc-row{display:flex;align-items:center;gap:7px;padding:4px 13px;font-size:9px}
.svcd{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.svcd.ok{background:var(--green);box-shadow:0 0 4px var(--green)}
.svcd.err{background:var(--red);box-shadow:0 0 4px var(--red);animation:berr 1.4s infinite}
@keyframes berr{0%,100%{opacity:1}50%{opacity:.25}}
.svcn{flex:1;color:var(--tmid)}.svcs{font-size:7px;letter-spacing:1px}
.svcs.ok{color:var(--green)}.svcs.err{color:var(--red)}
.mr{display:flex;align-items:center;justify-content:space-between;padding:4px 13px;font-size:9px}
.ml{color:var(--tdim)}.mv{font-weight:600}
.fchip{display:flex;align-items:center;gap:4px;margin:2px 9px;padding:3px 7px;background:var(--purple-d);border:1px solid rgba(157,92,255,.18);border-radius:3px;font-size:7px;color:#b888ff;line-height:1.3}
.fchip::before{content:'●';color:var(--purple);font-size:5px;flex-shrink:0}

/* MAIN */
main{flex:1;overflow:hidden;display:flex;flex-direction:column;min-width:0}
.tabbar{flex-shrink:0;display:flex;align-items:center;padding:0 8px;border-bottom:1px solid var(--bdr);background:rgba(2,8,22,.88);gap:0;height:36px}
.tb{padding:0 11px;height:100%;display:flex;align-items:center;gap:5px;font-size:9px;font-weight:600;letter-spacing:1px;color:var(--tdim);cursor:pointer;border-bottom:2px solid transparent;transition:all .16s;white-space:nowrap}
.tb:hover{color:var(--txt)}
.tb.active{color:var(--cyan);border-bottom-color:var(--cyan)}
.tbb{background:var(--cyan-d);border:1px solid rgba(0,180,255,.18);color:var(--cyan);font-size:7px;padding:0 4px;border-radius:7px}
.panel{display:none;flex:1;flex-direction:column;overflow:hidden;min-height:0}
.panel.active{display:flex}
.pscroll{flex:1;overflow-y:auto;overflow-x:hidden;padding:12px}
.pscroll::-webkit-scrollbar{width:3px}
.pscroll::-webkit-scrollbar-thumb{background:var(--bdr-h);border-radius:2px}

/* CARDS */
.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:var(--r);backdrop-filter:blur(12px);position:relative;overflow:hidden;transition:border-color .2s,box-shadow .2s}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(0,180,255,.38),transparent);pointer-events:none}
.card:hover{border-color:var(--bdr-h)}
.chdr{padding:7px 11px;font-size:7px;font-weight:700;letter-spacing:2.5px;color:var(--tdim);text-transform:uppercase;border-bottom:1px solid rgba(0,180,255,.07);display:flex;align-items:center;justify-content:space-between;background:rgba(0,8,28,.42)}
.cbody{padding:10px 11px}

/* SIGNAL CARDS */
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:10px}
.sc{background:rgba(4,14,40,.88);border:1px solid var(--bdr);border-radius:var(--r);padding:11px 12px;position:relative;overflow:hidden;transition:transform .18s,box-shadow .18s;cursor:pointer;animation:sin .35s ease both}
.sc::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:3px 0 0 3px}
.sc::after{content:'';position:absolute;top:-40px;right:-40px;width:130px;height:130px;border-radius:50%;pointer-events:none}
.sc.long{border-color:rgba(0,255,157,.16)}.sc.long::before{background:var(--green)}.sc.long::after{background:radial-gradient(circle,rgba(0,255,157,.055),transparent 68%)}
.sc.short{border-color:rgba(255,51,102,.16)}.sc.short::before{background:var(--red)}.sc.short::after{background:radial-gradient(circle,rgba(255,51,102,.055),transparent 68%)}
.sc.wait{border-color:rgba(255,184,0,.16)}.sc.wait::before{background:var(--amber)}
.sc:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(0,0,0,.38)}
@keyframes sin{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.sc-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:7px}
.sc-sym{font-size:15px;font-weight:700;letter-spacing:.5px}.sc-sym-s{font-size:8px;color:var(--tdim)}.sc-ts{font-size:7px;color:var(--tdim)}
.sc-mid{display:flex;align-items:center;gap:5px;margin-bottom:8px;flex-wrap:wrap}
.sc-lvls{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3px;margin-bottom:6px}
.sc-lvl{background:rgba(0,0,0,.22);border-radius:4px;padding:4px 6px}
.sc-lvl-l{font-size:6px;color:var(--tdim);margin-bottom:1px}.sc-lvl-v{font-size:10px;font-weight:600}
.sc-bot{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.sc-sbar{height:2px;background:rgba(0,180,255,.09);border-radius:2px;overflow:hidden;margin-top:7px}
.sc-sf{height:100%;background:linear-gradient(90deg,var(--cyan),var(--purple));border-radius:2px;box-shadow:0 0 5px rgba(0,180,255,.38);transition:width .7s ease}
.sc-hint{position:absolute;bottom:4px;right:7px;font-size:6px;color:var(--tdim);letter-spacing:1px}

/* BADGES */
.bd{display:inline-flex;align-items:center;gap:3px;font-size:7px;font-weight:700;letter-spacing:1px;padding:2px 6px;border-radius:3px}
.bd.long{color:var(--green);background:var(--green-d);border:1px solid rgba(0,255,157,.22)}
.bd.short{color:var(--red);background:var(--red-d);border:1px solid rgba(255,51,102,.22)}
.bd.wait{color:var(--amber);background:var(--amber-d);border:1px solid rgba(255,184,0,.22)}
.bsetup{font-size:6px;padding:2px 5px;border-radius:3px;letter-spacing:1px;background:var(--purple-d);border:1px solid rgba(157,92,255,.2);color:#c088ff;text-transform:uppercase}
.bmini{font-size:6px;padding:1px 5px;border-radius:3px;letter-spacing:.5px}
.bwhale{background:rgba(0,212,255,.11);color:var(--cyan);border:1px solid rgba(0,212,255,.18)}
.bzone{background:rgba(157,92,255,.11);color:var(--purple);border:1px solid rgba(157,92,255,.18)}
.bch{background:rgba(255,184,0,.1);color:var(--amber);border:1px solid rgba(255,184,0,.22)}
.bgrade{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px}
.g-ap{background:linear-gradient(135deg,#00ff9d,#00d4ff);color:#001020}
.g-a{color:var(--green);border:1px solid rgba(0,255,157,.28)}
.g-b{color:var(--cyan);border:1px solid rgba(0,180,255,.28)}
.g-c{color:var(--tdim);border:1px solid var(--bdr)}

/* SCREENER TABLE */
.scr-toolbar{display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid var(--bdr);background:rgba(2,6,20,.9);flex-shrink:0}
.scr-search{background:rgba(0,180,255,.06);border:1px solid rgba(0,180,255,.18);border-radius:4px;padding:5px 10px;color:var(--txt);font-family:var(--mono);font-size:10px;outline:none;width:160px;transition:border-color .18s}
.scr-search::placeholder{color:var(--tdim)}
.scr-search:focus{border-color:var(--cyan)}
.sort-info{font-size:8px;color:var(--tdim);letter-spacing:1px}
.sw{overflow-x:auto;flex:1}
table.st{width:100%;border-collapse:collapse;font-size:11px}
table.st th{position:sticky;top:0;z-index:2;padding:7px 9px;font-size:6px;letter-spacing:2px;font-weight:700;color:var(--tdim);text-align:left;white-space:nowrap;background:rgba(2,6,22,.98);border-bottom:1px solid rgba(0,180,255,.14);cursor:pointer;user-select:none;transition:color .15s}
table.st th:hover{color:var(--cyan)}
table.st th.sorted{color:var(--cyan)}
table.st th .sarr{font-size:8px;margin-left:2px;color:var(--cyan)}
table.st td{padding:6px 9px;border-bottom:1px solid rgba(255,255,255,.022);white-space:nowrap;vertical-align:middle}
table.st tbody tr:hover td{background:rgba(0,180,255,.03)}
table.st tbody tr.long td:first-child{border-left:2px solid rgba(0,255,157,.65)}
table.st tbody tr.short td:first-child{border-left:2px solid rgba(255,51,102,.65)}
table.st tbody tr.wait td:first-child{border-left:2px solid rgba(255,184,0,.65)}
table.st tbody tr{cursor:pointer}
.tscore{display:flex;align-items:center;gap:6px}
.tsn{font-weight:700;font-size:12px;min-width:26px}
.tsb{flex:1;height:3px;background:rgba(0,180,255,.07);border-radius:2px;min-width:44px;overflow:hidden}
.tsf{height:100%;background:linear-gradient(90deg,var(--cyan),var(--purple));border-radius:2px;box-shadow:0 0 4px rgba(0,180,255,.28)}
.btcp{font-size:7px;padding:1px 4px;border-radius:3px;font-weight:700}
.ba{background:rgba(0,255,157,.09);color:var(--green);border:1px solid rgba(0,255,157,.18)}
.bb2{background:rgba(255,51,102,.09);color:var(--red);border:1px solid rgba(255,51,102,.18)}
.bm{background:rgba(255,184,0,.09);color:var(--amber);border:1px solid rgba(255,184,0,.18)}
.oc-tp1{color:var(--green)}.oc-win{color:rgba(0,255,157,.55)}.oc-stop{color:var(--red)}.oc-loss{color:var(--amber)}.oc-flat{color:var(--tdim)}.oc-n{color:rgba(255,255,255,.14)}
.fp{color:var(--green)}.fn{color:var(--red)}

/* ANALYTICS */
.rings-row{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;padding:12px 8px}
.ring-item{display:flex;flex-direction:column;align-items:center;gap:4px}
.ring-lbl{font-size:6px;letter-spacing:2px;color:var(--tdim);text-align:center}
.dcards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:8px}
.dcard{padding:10px 8px;border-radius:var(--rs);text-align:center;background:rgba(0,0,0,.18);border:1px solid rgba(0,180,255,.06)}
.dcard-wr{font-size:20px;font-weight:700;margin-bottom:2px}.dcard-l{font-size:6px;letter-spacing:2px;color:var(--tdim)}.dcard-n{font-size:7px;color:var(--tdim);margin-top:2px}
.spills{display:flex;gap:7px;flex-wrap:wrap;padding:8px 11px}
.spill{display:flex;flex-direction:column;align-items:center;padding:7px 12px;border-radius:var(--rs);background:rgba(0,0,0,.18);border:1px solid rgba(0,180,255,.06);min-width:65px}
.spill-v{font-size:17px;font-weight:700}.spill-l{font-size:6px;letter-spacing:1.5px;color:var(--tdim);margin-top:2px;text-transform:uppercase}
.hmgrid{display:grid;grid-template-columns:repeat(12,1fr);gap:3px;padding:8px}
.hmc{display:flex;flex-direction:column;align-items:center;padding:4px 2px;border-radius:4px;border:1px solid transparent;cursor:default;transition:transform .14s}
.hmc:hover{transform:scale(1.18);z-index:5;position:relative}
.hmc-h{font-size:6px;color:var(--tdim)}.hmc-w{font-size:8px;font-weight:700;margin-top:2px}
.sbr{display:flex;align-items:center;gap:8px;padding:6px 11px;border-bottom:1px solid rgba(255,255,255,.025)}
.sbn{min-width:75px;font-size:8px;color:var(--tmid)}.sbt{flex:1;height:5px;background:rgba(255,255,255,.04);border-radius:3px;overflow:hidden}
.sbf{height:100%;border-radius:3px;transition:width .9s ease}
.sbwr{min-width:36px;text-align:right;font-size:9px;font-weight:700}.sbnn{font-size:7px;color:var(--tdim);min-width:28px;text-align:right}
.hdots{display:flex;flex-wrap:wrap;gap:4px;padding:8px 11px}
.hd{width:10px;height:10px;border-radius:50%;cursor:default;transition:transform .14s}
.hd:hover{transform:scale(1.65);z-index:5}

/* LIQ TAB */
.liq-totals{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px}
.liq-period{border-radius:var(--r);border:1px solid var(--bdr);background:var(--bg2);padding:12px}
.liq-period-h{font-size:7px;letter-spacing:3px;color:var(--tdim);margin-bottom:8px}
.liq-total-usd{font-size:22px;font-weight:700;margin-bottom:6px}
.liq-bar-wrap{height:5px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden;margin-bottom:4px}
.liq-bar-long{height:100%;background:var(--green);border-radius:3px 0 0 3px;display:inline-block;transition:width .8s}
.liq-bar-short{height:100%;background:var(--red);display:inline-block;transition:width .8s}
.liq-side-row{display:flex;justify-content:space-between;font-size:8px}

.liq-sym-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.liq-sym-row{display:flex;align-items:center;gap:7px;padding:5px 10px;border-bottom:1px solid rgba(255,255,255,.025);font-size:10px}
.liq-sym-name{min-width:80px;font-weight:700}
.liq-sym-bar-wrap{flex:1;height:4px;background:rgba(255,255,255,.04);border-radius:2px;overflow:hidden}
.liq-sym-bar{height:100%;border-radius:2px;transition:width .8s}
.liq-sym-usd{min-width:70px;text-align:right;font-size:9px;font-weight:600}
.liq-sym-cnt{font-size:7px;color:var(--tdim);min-width:28px;text-align:right}

table.lt{width:100%;border-collapse:collapse;font-size:10px}
table.lt th{padding:6px 9px;font-size:6px;letter-spacing:2px;color:var(--tdim);text-align:left;border-bottom:1px solid rgba(0,180,255,.12);background:rgba(2,6,22,.95);position:sticky;top:0}
table.lt td{padding:5px 9px;border-bottom:1px solid rgba(255,255,255,.022);white-space:nowrap;vertical-align:middle}
table.lt tbody tr:hover td{background:rgba(0,180,255,.025)}

/* CHARTS */
.cgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.cthumb{position:relative;cursor:pointer;border-radius:var(--r);overflow:hidden;border:1px solid var(--bdr);aspect-ratio:16/9;background:rgba(0,8,28,.5);transition:border-color .2s,transform .2s,box-shadow .2s}
.cthumb:hover{border-color:var(--cyan);transform:scale(1.03);box-shadow:0 0 22px rgba(0,180,255,.12)}
.cthumb img{width:100%;height:100%;object-fit:cover;display:block}
.clbl{position:absolute;bottom:0;left:0;right:0;padding:4px 8px;background:linear-gradient(0deg,rgba(0,4,20,.95),transparent);font-size:7px;color:var(--cyan);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* JOURNAL */
.jcontent{font-family:var(--sans);font-size:13px;line-height:1.72;color:var(--txt);max-width:820px}
.jcontent h1{font-size:19px;color:var(--cyan);margin:14px 0 7px}.jcontent h2{font-size:14px;color:var(--purple);margin:12px 0 5px}.jcontent h3{font-size:12px;color:var(--amber);margin:9px 0 3px}
.jcontent p{margin:5px 0;color:var(--tmid)}.jcontent table{border-collapse:collapse;margin:8px 0;font-size:11px}
.jcontent th,.jcontent td{padding:4px 10px;border:1px solid var(--bdr)}.jcontent th{background:rgba(0,180,255,.07);color:var(--cyan)}
.jcontent code{background:rgba(0,180,255,.07);padding:1px 5px;border-radius:3px;font-family:var(--mono);font-size:10px;color:var(--green)}
.jcontent li{margin:3px 0 3px 18px;color:var(--tmid)}.jcontent hr{border:none;border-top:1px solid var(--bdr);margin:12px 0}.jcontent strong{color:var(--txt);font-weight:600}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:180px;color:var(--tdim);font-size:10px;gap:8px;letter-spacing:1px}
.empty-ic{font-size:30px;opacity:.28}

/* CHART MODAL */
#modal{position:fixed;inset:0;z-index:999;display:none;align-items:center;justify-content:center;background:rgba(0,0,10,.94);backdrop-filter:blur(18px)}
#modal.open{display:flex}
#modal img{max-width:90vw;max-height:88vh;border:1px solid var(--bdr-h);border-radius:var(--r);box-shadow:0 0 80px rgba(0,180,255,.14)}
#mcl{position:absolute;top:18px;right:22px;font-size:20px;cursor:pointer;color:var(--tdim);transition:color .18s}
#mcl:hover{color:var(--cyan)}

/* SIGNAL DETAIL MODAL */
#sigmodal{position:fixed;inset:0;z-index:998;display:none;align-items:center;justify-content:center;background:rgba(0,0,10,.9);backdrop-filter:blur(16px)}
#sigmodal.open{display:flex}
.sigmodal-box{background:rgba(5,15,42,.97);border:1px solid var(--bdr-h);border-radius:var(--r);width:min(740px,95vw);max-height:88vh;overflow-y:auto;position:relative;box-shadow:0 0 80px rgba(0,180,255,.12)}
.sigmodal-box::-webkit-scrollbar{width:3px}
.sigmodal-box::-webkit-scrollbar-thumb{background:var(--bdr-h)}
.sigmodal-hdr{padding:16px 20px 12px;border-bottom:1px solid var(--bdr);display:flex;align-items:flex-start;justify-content:space-between;background:rgba(0,8,28,.5)}
.sigmodal-sym{font-size:22px;font-weight:700;letter-spacing:1px}
.sigmodal-close{font-size:18px;cursor:pointer;color:var(--tdim);transition:color .18s;line-height:1;padding:2px}
.sigmodal-close:hover{color:var(--cyan)}
.sigmodal-body{padding:16px 20px}
.sm-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:14px}
.sm-cell{background:rgba(0,0,0,.25);border-radius:5px;padding:8px 10px;border:1px solid rgba(0,180,255,.06)}
.sm-l{font-size:6px;letter-spacing:2px;color:var(--tdim);margin-bottom:3px;text-transform:uppercase}
.sm-v{font-size:13px;font-weight:600}
.sm-section{font-size:6px;letter-spacing:3px;color:var(--tdim);margin:12px 0 6px;border-bottom:1px solid rgba(0,180,255,.07);padding-bottom:4px}
.sc-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:12px}
.sc-cell{background:rgba(0,0,0,.2);border-radius:4px;padding:6px 8px;text-align:center}
.sc-cell-l{font-size:6px;color:var(--tdim);margin-bottom:2px}
.sc-cell-v{font-size:11px;font-weight:700}
.sm-flags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.sm-flag{font-size:8px;padding:3px 8px;border-radius:4px;font-weight:500}
.flag-on{background:rgba(0,255,157,.1);color:var(--green);border:1px solid rgba(0,255,157,.22)}
.flag-off{background:rgba(255,255,255,.03);color:var(--tdim);border:1px solid rgba(255,255,255,.07)}
</style>
</head>
<body>
<div id="aurora"><div class="al al1"></div><div class="al al2"></div><div class="al al3"></div></div>
<canvas id="bg"></canvas><div id="sl"></div><div id="vg"></div>

<div id="app">
<header>
  <div class="logo-block"><div><div class="logo">NEXUS</div><div class="logo-sub">TRADING TERMINAL v2</div></div></div>
  <div class="hdr-metrics">
    <div class="hm"><span class="hm-l">BTC PRICE</span><span class="hm-v" id="h-btc">—</span></div>
    <div class="hm"><span class="hm-l">ALT BREADTH</span><span class="hm-v" id="h-brd">—</span></div>
    <div class="hm"><span class="hm-l">BTC EMA 4H</span><span class="hm-v" id="h-ema">—</span></div>
    <div class="hm"><span class="hm-l">ACTIVE</span><span class="hm-v" id="h-act" style="color:var(--cyan)">—</span></div>
    <div class="hm"><span class="hm-l">24H WIN RATE</span><span class="hm-v" id="h-wr24">—</span></div>
    <div class="hm"><span class="hm-l">WR ЧЕСТНОЕ ОКНО ≥06-02</span><span class="hm-v" id="h-wrat">—</span></div>
    <div class="hm"><span class="hm-l">LAST SCAN</span><span class="hm-v" id="h-scan" style="font-size:10px">—</span></div>
  </div>
  <div class="hdr-r"><div id="clk">00:00:00 UTC</div><div class="ldot"></div></div>
</header>

<div class="body">
<aside>
  <div class="ns">NAVIGATION</div>
  <div class="ni active" onclick="go('live')" id="n-live"><span class="ni-ic">⚡</span>LIVE SIGNALS<span class="nb" id="nb-live">0</span></div>
  <div class="ni" onclick="go('screener')" id="n-screener"><span class="ni-ic">📊</span>SCREENER<span class="nb" id="nb-scr">0</span></div>
  <div class="ni" onclick="go('stats')" id="n-stats"><span class="ni-ic">📈</span>ANALYTICS</div>
  <div class="ni" onclick="go('liq')" id="n-liq"><span class="ni-ic">💧</span>LIQUIDATIONS</div>
  <div class="ni" onclick="go('charts')" id="n-charts"><span class="ni-ic">🖼</span>CHARTS<span class="nb" id="nb-ch">0</span></div>
  <div class="ni" onclick="go('journal')" id="n-journal"><span class="ni-ic">📔</span>JOURNAL</div>
  <div class="ssep"></div>
  <div class="ns">SERVICES</div>
  <div id="svcs"></div>
  <div class="ssep"></div>
  <div class="ns">MACRO</div>
  <div id="macro"></div>
  <div class="ssep"></div>
  <div class="ns">ACTIVE FILTERS</div>
  <div id="fltrs"></div>
</aside>

<main>
  <div class="tabbar">
    <div class="tb active" onclick="go('live')" id="t-live">⚡ LIVE <span class="tbb" id="tb-live">0</span></div>
    <div class="tb" onclick="go('screener')" id="t-screener">📊 SCREENER <span class="tbb" id="tb-scr">0</span></div>
    <div class="tb" onclick="go('stats')" id="t-stats">📈 ANALYTICS</div>
    <div class="tb" onclick="go('liq')" id="t-liq">💧 LIQUIDATIONS</div>
    <div class="tb" onclick="go('charts')" id="t-charts">🖼 CHARTS <span class="tbb" id="tb-ch">0</span></div>
    <div class="tb" onclick="go('journal')" id="t-journal">📔 JOURNAL</div>
  </div>

  <!-- LIVE -->
  <div class="panel active" id="p-live">
    <div class="pscroll"><div class="sgrid" id="sgrid"></div></div>
  </div>

  <!-- SCREENER -->
  <div class="panel" id="p-screener" style="flex-direction:column">
    <div class="scr-toolbar">
      <input class="scr-search" id="scr-search" placeholder="🔍  Filter by symbol..." oninput="applyScreener()">
      <span class="sort-info" id="sort-info">SORTED BY SCORE ↓</span>
    </div>
    <div class="sw">
      <table class="st"><thead><tr>
        <th onclick="sortBy('#')">#</th>
        <th onclick="sortBy('symbol')">SYMBOL<span class="sarr" id="sa-symbol"></span></th>
        <th onclick="sortBy('score')" class="sorted">SCORE<span class="sarr" id="sa-score">↓</span></th>
        <th>GRADE</th>
        <th onclick="sortBy('setup')">SETUP<span class="sarr" id="sa-setup"></span></th>
        <th onclick="sortBy('direction')">DIR<span class="sarr" id="sa-direction"></span></th>
        <th onclick="sortBy('price_entry')">ENTRY<span class="sarr" id="sa-price_entry"></span></th>
        <th onclick="sortBy('stop')">STOP<span class="sarr" id="sa-stop"></span></th>
        <th onclick="sortBy('tp1')">TP1<span class="sarr" id="sa-tp1"></span></th>
        <th onclick="sortBy('tp2')">TP2<span class="sarr" id="sa-tp2"></span></th>
        <th onclick="sortBy('funding')">FUND<span class="sarr" id="sa-funding"></span></th>
        <th onclick="sortBy('rsi_1h')">RSI<span class="sarr" id="sa-rsi_1h"></span></th>
        <th onclick="sortBy('oi_24h_pct')">OI 24H<span class="sarr" id="sa-oi_24h_pct"></span></th>
        <th onclick="sortBy('cvd_kline')">CVD<span class="sarr" id="sa-cvd_kline"></span></th>
        <th>BTC</th>
        <th onclick="sortBy('in_zone')">ZONE<span class="sarr" id="sa-in_zone"></span></th>
        <th onclick="sortBy('whale_flag')">WHALE<span class="sarr" id="sa-whale_flag"></span></th>
        <th>OC 4H</th><th>OC 24H</th>
      </tr></thead><tbody id="stbody"></tbody></table>
    </div>
  </div>

  <!-- ANALYTICS -->
  <div class="panel" id="p-stats">
    <div class="pscroll">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <div class="card"><div class="chdr">WIN RATE BY SETUP</div><div class="rings-row" id="rings"></div></div>
        <div class="card"><div class="chdr">BY DIRECTION</div><div class="dcards" id="dcards"></div></div>
      </div>
      <div class="card" style="margin-bottom:10px"><div class="chdr">SIGNAL SUMMARY</div><div class="spills" id="spills"></div></div>
      <div class="card" style="margin-bottom:10px"><div class="chdr">UTC HOUR WIN RATE HEATMAP</div><div class="hmgrid" id="hmgrid"></div></div>
      <div class="card" style="margin-bottom:10px"><div class="chdr">SETUP PERFORMANCE</div><div id="sbars"></div></div>
      <div class="card"><div class="chdr">SIGNAL HISTORY DOTS</div><div class="hdots" id="hdots"></div></div>
    </div>
  </div>

  <!-- LIQUIDATIONS -->
  <div class="panel" id="p-liq">
    <div class="pscroll">
      <div class="liq-totals" id="liq-totals"></div>
      <div class="liq-sym-grid">
        <div class="card">
          <div class="chdr">TOP BY USD — 1H</div>
          <div id="liq-sym-1h"></div>
        </div>
        <div class="card">
          <div class="chdr">TOP BY USD — 4H</div>
          <div id="liq-sym-4h"></div>
        </div>
      </div>
      <div class="card">
        <div class="chdr">RECENT LARGE LIQUIDATIONS (4H)</div>
        <div style="overflow-x:auto">
          <table class="lt">
            <thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>PRICE</th><th>QTY</th><th>USD VALUE</th></tr></thead>
            <tbody id="liq-recent"></tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- CHARTS -->
  <div class="panel" id="p-charts">
    <div class="pscroll"><div class="cgrid" id="cgrid"></div></div>
  </div>

  <!-- JOURNAL -->
  <div class="panel" id="p-journal">
    <div class="pscroll"><div id="jbody"></div></div>
  </div>
</main>
</div>
</div>

<!-- CHART MODAL -->
<div id="modal"><span id="mcl" onclick="closeModal()">✕</span><img id="mimg" src="" alt=""></div>

<!-- SIGNAL DETAIL MODAL -->
<div id="sigmodal">
  <div class="sigmodal-box">
    <div class="sigmodal-hdr">
      <div>
        <div class="sigmodal-sym" id="sm-sym">—</div>
        <div style="margin-top:4px;display:flex;gap:6px;align-items:center" id="sm-badges"></div>
      </div>
      <div class="sigmodal-close" onclick="closeSigModal()">✕</div>
    </div>
    <div class="sigmodal-body" id="sm-body"></div>
  </div>
</div>

<script>
/* PARTICLES */
(function(){
  const cv=document.getElementById('bg'),cx=cv.getContext('2d');
  let W,H,P;const N=75,LK=88,SP=.2,COL='0,170,255';
  function rsz(){W=cv.width=innerWidth;H=cv.height=innerHeight}
  function init(){rsz();P=Array.from({length:N},()=>({x:Math.random()*W,y:Math.random()*H,vx:(Math.random()-.5)*SP,vy:(Math.random()-.5)*SP,r:Math.random()*1.1+.3}))}
  function frm(){
    cx.clearRect(0,0,W,H);
    for(let i=0;i<N;i++)for(let j=i+1;j<N;j++){const dx=P[i].x-P[j].x,dy=P[i].y-P[j].y,d=Math.sqrt(dx*dx+dy*dy);if(d<LK){cx.beginPath();cx.strokeStyle=`rgba(${COL},${(1-d/LK)*.16})`;cx.lineWidth=.5;cx.moveTo(P[i].x,P[i].y);cx.lineTo(P[j].x,P[j].y);cx.stroke()}}
    P.forEach(p=>{cx.beginPath();cx.arc(p.x,p.y,p.r,0,Math.PI*2);cx.fillStyle=`rgba(${COL},.48)`;cx.shadowBlur=4;cx.shadowColor=`rgba(${COL},.6)`;cx.fill();cx.shadowBlur=0;p.x+=p.vx;p.y+=p.vy;if(p.x<0)p.x=W;if(p.x>W)p.x=0;if(p.y<0)p.y=H;if(p.y>H)p.y=0});
    requestAnimationFrame(frm);
  }
  window.addEventListener('resize',rsz);init();frm();
})();

/* CLOCK */
function tick(){const n=new Date(),p=v=>String(v).padStart(2,'0');document.getElementById('clk').textContent=`${p(n.getUTCHours())}:${p(n.getUTCMinutes())}:${p(n.getUTCSeconds())} UTC`}
setInterval(tick,1000);tick();

/* TABS */
const TABS=['live','screener','stats','liq','charts','journal'];
let _tab='live';
function go(name){
  _tab=name;
  TABS.forEach(t=>{
    document.getElementById('p-'+t).classList.toggle('active',t===name);
    document.getElementById('t-'+t).classList.toggle('active',t===name);
    document.getElementById('n-'+t).classList.toggle('active',t===name);
  });
  if(name==='liq') loadLiq();
}

/* FORMATTERS */
function fP(v){if(v==null)return'—';v=parseFloat(v);if(isNaN(v))return'—';if(v>=10000)return v.toFixed(0);if(v>=100)return v.toFixed(1);if(v>=1)return v.toFixed(3);if(v>=.01)return v.toFixed(4);return v.toFixed(6)}
function fPct(v){if(v==null)return'—';const n=parseFloat(v);return isNaN(n)?'—':(n>0?'+':'')+n.toFixed(2)+'%'}
function fT(ts){return ts?ts.slice(11,16)+' UTC':'—'}
function fN(v,d=1){return v==null?'—':parseFloat(v).toFixed(d)}
function fUSD(v){if(!v)return'$0';if(v>=1e6)return'$'+(v/1e6).toFixed(2)+'M';if(v>=1e3)return'$'+(v/1e3).toFixed(0)+'K';return'$'+v.toFixed(0)}

function dirB(d){const m={ЛОНГ:['long','▲','LONG'],ШОРТ:['short','▼','SHORT'],ЖДАТЬ:['wait','◆','WAIT']};const[c,i,t]=m[d]||['wait','?',d||'?'];return`<span class="bd ${c}">${i} ${t}</span>`}
function setupB(s){const sh={squeeze:'SQZ',bos_fvg:'BOS/FVG',short_dist:'SHDIST',breakout:'PUMP',range_sweep:'SWEEP'};return`<span class="bsetup">${sh[s]||s||'?'}</span>`}
function ocB(o){if(!o)return'<span class="oc-n">·</span>';const m={TP1:'oc-tp1',WIN:'oc-win',STOP:'oc-stop',LOSS:'oc-loss',FLAT:'oc-flat'};return`<span class="${m[o]||'oc-n'}">${o}</span>`}
function gradeB(g){if(!g||g==='—')return'<span class="oc-n">—</span>';const c=g==='A+'?'g-ap':g==='A'?'g-a':g==='B'?'g-b':'g-c';return`<span class="bgrade ${c}">${g}</span>`}
function btcB(p){const m={above:['ba','↑ABV'],below:['bb2','↓BLW'],between:['bm','~MID']};const[c,t]=m[p]||['','—'];return`<span class="btcp ${c}">${t}</span>`}
function sbar(sc){const p=Math.min(100,(sc||0)/280*100);return`<div class="tscore"><span class="tsn">${sc||0}</span><div class="tsb"><div class="tsf" style="width:${p}%"></div></div></div>`}

/* LIVE */
let _liveData=[];
function renderLive(sigs){
  _liveData=sigs;
  document.getElementById('nb-live').textContent=sigs.length;
  document.getElementById('tb-live').textContent=sigs.length;
  const g=document.getElementById('sgrid');
  if(!sigs.length){g.innerHTML='<div class="empty"><div class="empty-ic">📭</div><div>NO ACTIVE SIGNALS</div></div>';return}
  g.innerHTML=sigs.slice(0,28).map((s,i)=>{
    const c=s.direction==='ЛОНГ'?'long':s.direction==='ШОРТ'?'short':'wait';
    const pct=Math.min(100,(s.score||0)/280*100);
    const chConf=(s.channel_conf||[]);
    const ex=[s.whale_flag?'<span class="bmini bwhale">🐋 WHALE</span>':'',s.in_zone?'<span class="bmini bzone">IN ZONE</span>':'',chConf.length?`<span class="bmini bch">📢 ${chConf.length}CH</span>`:''].filter(Boolean).join('');
    return`<div class="sc ${c}" style="animation-delay:${(i*.04).toFixed(2)}s" onclick="showSig(${i})">
      <div class="sc-top"><div><div class="sc-sym">${(s.symbol||'?').replace('USDT','')}<span class="sc-sym-s">USDT</span></div></div><div class="sc-ts">${fT(s.run_ts)}</div></div>
      <div class="sc-mid">${dirB(s.direction)}${setupB(s.setup)}${gradeB(s.grade)}</div>
      <div class="sc-lvls">
        <div class="sc-lvl"><div class="sc-lvl-l">ENTRY</div><div class="sc-lvl-v">${fP(s.price_entry)}</div></div>
        <div class="sc-lvl"><div class="sc-lvl-l">STOP</div><div class="sc-lvl-v" style="color:var(--red)">${fP(s.stop)}</div></div>
        <div class="sc-lvl"><div class="sc-lvl-l">TP1</div><div class="sc-lvl-v" style="color:var(--green)">${fP(s.tp1)}</div></div>
      </div>
      <div class="sc-bot"><span style="font-size:7px;color:var(--tdim)">RSI ${fN(s.rsi_1h,0)}</span><span style="font-size:7px;color:var(--tdim)">FUND ${fPct(s.funding)}</span>${ex}</div>
      <div class="sc-sbar"><div class="sc-sf" style="width:${pct}%"></div></div>
      <div class="sc-hint">TAP FOR DETAILS</div>
    </div>`;
  }).join('');
}

/* SIGNAL DETAIL MODAL */
function showSig(idx){
  const s=_liveData[idx]; if(!s) return;
  const cls=s.direction==='ЛОНГ'?'long':s.direction==='ШОРТ'?'short':'wait';
  document.getElementById('sm-sym').textContent=(s.symbol||'?').replace('USDT','')+'/USDT';
  document.getElementById('sm-badges').innerHTML=dirB(s.direction)+setupB(s.setup)+gradeB(s.grade)+(s.whale_flag?'<span class="bmini bwhale">🐋 WHALE</span>':'')+(s.in_zone?'<span class="bmini bzone">IN ZONE</span>':'');
  const rsi=s.rsi_1h!=null?parseFloat(s.rsi_1h).toFixed(1):'—';
  const rsiC=parseFloat(rsi)>70?'var(--red)':parseFloat(rsi)<30?'var(--green)':'var(--txt)';
  const oi=s.oi_24h_pct!=null?(parseFloat(s.oi_24h_pct)>0?'+':'')+parseFloat(s.oi_24h_pct).toFixed(2)+'%':'—';
  const oiC=parseFloat(s.oi_24h_pct)>0?'var(--green)':'var(--red)';
  let h='';
  h+=`<div class="sm-section">PRICE LEVELS</div>`;
  h+=`<div class="sm-grid">
    <div class="sc-cell"><div class="sm-l">ENTRY</div><div class="sm-v">${fP(s.price_entry)}</div></div>
    <div class="sc-cell"><div class="sm-l">STOP</div><div class="sm-v" style="color:var(--red)">${fP(s.stop)}</div></div>
    <div class="sc-cell"><div class="sm-l">TP1</div><div class="sm-v" style="color:var(--green)">${fP(s.tp1)}</div></div>
    <div class="sc-cell"><div class="sm-l">TP2</div><div class="sm-v" style="color:rgba(0,255,157,.55)">${fP(s.tp2)}</div></div>
    <div class="sc-cell"><div class="sm-l">VWAP DEV</div><div class="sm-v">${fN(s.vwap_dev,2)}%</div></div>
    <div class="sc-cell"><div class="sm-l">PUMP SCORE</div><div class="sm-v" style="color:var(--amber)">${s.pump_score||'—'}</div></div>
  </div>`;
  h+=`<div class="sm-section">TECHNICAL INDICATORS</div>`;
  h+=`<div class="sm-grid">
    <div class="sc-cell"><div class="sm-l">RSI 1H</div><div class="sm-v" style="color:${rsiC}">${rsi}</div></div>
    <div class="sc-cell"><div class="sm-l">CVD KLINE</div><div class="sm-v" style="color:${parseFloat(s.cvd_kline)>0?'var(--green)':'var(--red)'}">${fN(s.cvd_kline,2)}</div></div>
    <div class="sc-cell"><div class="sm-l">CVD TRADE</div><div class="sm-v" style="color:${parseFloat(s.cvd_trade)>0?'var(--green)':'var(--red)'}">${fN(s.cvd_trade,2)}</div></div>
    <div class="sc-cell"><div class="sm-l">FUNDING</div><div class="sm-v" style="color:${parseFloat(s.funding)>=0?'var(--green)':'var(--red)'}">${fPct(s.funding)}</div></div>
    <div class="sc-cell"><div class="sm-l">OI 24H</div><div class="sm-v" style="color:${oiC}">${oi}</div></div>
    <div class="sc-cell"><div class="sm-l">RS BTC</div><div class="sm-v">${fN(s.rs_btc,2)}</div></div>
  </div>`;
  h+=`<div class="sm-section">MTF ALIGNMENT</div>`;
  h+=`<div class="sm-grid">
    <div class="sc-cell"><div class="sm-l">MTF BULL</div><div class="sm-v" style="color:var(--green)">${s.mtf_bull??'—'}/5</div></div>
    <div class="sc-cell"><div class="sm-l">MTF BEAR</div><div class="sm-v" style="color:var(--red)">${s.mtf_bear??'—'}/5</div></div>
    <div class="sc-cell"><div class="sm-l">EMA BULL 1H</div><div class="sm-v" style="color:${s.ema_bull_1h?'var(--green)':'var(--tdim)'}">${s.ema_bull_1h?'YES':'NO'}</div></div>
    <div class="sc-cell"><div class="sm-l">EMA BULL 4H</div><div class="sm-v" style="color:${s.ema_bull_4h?'var(--green)':'var(--tdim)'}">${s.ema_bull_4h?'YES':'NO'}</div></div>
    <div class="sc-cell"><div class="sm-l">CHoCH BULL 1H</div><div class="sm-v" style="color:${s.choch_bull_1h?'var(--green)':'var(--tdim)'}">${s.choch_bull_1h?'YES':'NO'}</div></div>
    <div class="sc-cell"><div class="sm-l">BNB BONUS</div><div class="sm-v" style="color:var(--amber)">${s.bnb_cross_bonus||0}</div></div>
  </div>`;
  h+=`<div class="sm-section">SUB-SCORES</div>`;
  h+=`<div class="sc-grid">
    ${[['SQUEEZE',s.sc_squeeze,'var(--cyan)'],['BOS/FVG',s.sc_bos_fvg,'var(--purple)'],['SH.DIST',s.sc_short_dist,'var(--red)'],['PUMP',s.sc_breakout,'var(--green)'],['SWEEP',s.sc_range_sweep,'var(--amber)']].map(([l,v,c])=>
      `<div class="sc-cell"><div class="sc-cell-l">${l}</div><div class="sc-cell-v" style="color:${c}">${v??'—'}</div></div>`).join('')}
  </div>`;
  h+=`<div class="sm-section">MARKET CONTEXT</div>`;
  h+=`<div class="sm-flags">
    ${[['BTC '+({above:'↑ ABOVE',below:'↓ BELOW',between:'~ MID'}[s.btc_trend_4h]||s.btc_trend_4h||'?'),true,''],
       ['ALT BREADTH '+(s.alt_breadth_pct!=null?s.alt_breadth_pct+'%':'—'),true,''],
       ['WHALE',!!s.whale_flag,'🐋'],['IN ZONE',!!s.in_zone,'📌'],
       ['CHoCH 1H',!!s.choch_bull_1h,'🔄'],['WHALE FLAG',!!s.whale_flag,'⚡'],
    ].map(([l,on,ic])=>`<span class="sm-flag ${on?'flag-on':'flag-off'}">${ic?ic+' ':''}${l}</span>`).join('')}
  </div>`;
  h+=`<div class="sm-section">META</div>`;
  h+=`<div class="sm-grid">
    <div class="sc-cell"><div class="sm-l">SCAN TIME</div><div class="sm-v" style="font-size:10px">${fT(s.run_ts)}</div></div>
    <div class="sc-cell"><div class="sm-l">UTC HOUR</div><div class="sm-v">${s.utc_hour??'—'}</div></div>
    <div class="sc-cell"><div class="sm-l">LISTING AGE</div><div class="sm-v">${s.listing_age_days!=null?s.listing_age_days+'d':'—'}</div></div>
    <div class="sc-cell"><div class="sm-l">OUTCOME 4H</div><div class="sm-v">${ocB(s.outcome_4h)}</div></div>
    <div class="sc-cell"><div class="sm-l">OUTCOME 24H</div><div class="sm-v">${ocB(s.outcome_24h)}</div></div>
    <div class="sc-cell"><div class="sm-l">CHG 24H</div><div class="sm-v">${fPct(s.change_24h_pct)}</div></div>
  </div>`;
  const conf=s.channel_conf||[], conflict=s.channel_conflict||[];
  if(conf.length||conflict.length){
    h+=`<div class="sm-section">CHANNEL SIGNALS</div>`;
    if(conf.length) h+=`<div style="margin-bottom:8px"><div style="font-size:7px;color:var(--tdim);letter-spacing:1px;margin-bottom:4px">CONFIRMING (${conf.length}):</div><div style="display:flex;flex-wrap:wrap;gap:4px">${conf.map(ch=>`<span class="sm-flag flag-on">📢 ${ch}</span>`).join('')}</div></div>`;
    if(conflict.length) h+=`<div><div style="font-size:7px;color:var(--tdim);letter-spacing:1px;margin-bottom:4px">CONFLICTING (${conflict.length}):</div><div style="display:flex;flex-wrap:wrap;gap:4px">${conflict.map(ch=>`<span class="sm-flag flag-off">⚡ ${ch}</span>`).join('')}</div></div>`;
  } else {
    h+=`<div class="sm-section">CHANNEL SIGNALS</div><div style="font-size:9px;color:var(--tdim);margin-bottom:8px">No channel mentions for this symbol</div>`;
  }
  document.getElementById('sm-body').innerHTML=h;
  document.getElementById('sigmodal').classList.add('open');
}
function closeSigModal(){document.getElementById('sigmodal').classList.remove('open')}
document.getElementById('sigmodal').addEventListener('click',e=>{if(e.target.id==='sigmodal')closeSigModal()});

/* SCREENER */
let _cands=[], _sortCol='score', _sortAsc=false;

function sortBy(col){
  if(col==='#') return;
  document.querySelectorAll('table.st th.sorted').forEach(el=>el.classList.remove('sorted'));
  document.querySelectorAll('.sarr').forEach(el=>el.textContent='');
  if(_sortCol===col) _sortAsc=!_sortAsc; else{_sortCol=col;_sortAsc=false}
  const el=document.getElementById('sa-'+col);
  if(el){el.textContent=_sortAsc?'↑':'↓';el.closest('th').classList.add('sorted')}
  document.getElementById('sort-info').textContent=`SORTED BY ${col.toUpperCase().replace('_',' ')} ${_sortAsc?'↑':'↓'}`;
  applyScreener();
}

function applyScreener(){
  const q=(document.getElementById('scr-search')?.value||'').toUpperCase().trim();
  let rows=[..._cands];
  if(q) rows=rows.filter(r=>(r.symbol||'').includes(q)||(r.setup||'').toUpperCase().includes(q));
  rows.sort((a,b)=>{
    let av=a[_sortCol], bv=b[_sortCol];
    av=av==null?(_sortAsc?Infinity:-Infinity):isNaN(parseFloat(av))?av:parseFloat(av);
    bv=bv==null?(_sortAsc?Infinity:-Infinity):isNaN(parseFloat(bv))?bv:parseFloat(bv);
    if(typeof av==='number'&&typeof bv==='number') return _sortAsc?av-bv:bv-av;
    return _sortAsc?String(av).localeCompare(String(bv)):String(bv).localeCompare(String(av));
  });
  renderScreenerRows(rows);
}

function renderScreenerRows(rows){
  document.getElementById('stbody').innerHTML=rows.map((r,i)=>{
    const c=r.direction==='ЛОНГ'?'long':r.direction==='ШОРТ'?'short':'wait';
    const f=parseFloat(r.funding)||0;
    const rsi=r.rsi_1h!=null?parseFloat(r.rsi_1h).toFixed(0):'—';
    const rC=rsi>70?'var(--red)':rsi<30?'var(--green)':'var(--tdim)';
    const oi=r.oi_24h_pct!=null?(parseFloat(r.oi_24h_pct)>0?'+':'')+parseFloat(r.oi_24h_pct).toFixed(1)+'%':'—';
    const cvd=r.cvd_kline!=null?fN(r.cvd_kline,1):'—';
    const idx=_liveData.findIndex(s=>s.symbol===r.symbol&&s.run_ts===r.run_ts);
    const click=idx>=0?`onclick="showSig(${idx})"`:'' ;
    return`<tr class="${c}" ${click}>
      <td style="color:var(--tdim);font-size:8px">${i+1}</td>
      <td style="font-weight:700;font-size:12px">${(r.symbol||'?').replace('USDT','')}</td>
      <td>${sbar(r.score)}</td><td>${gradeB(r.grade)}</td><td>${setupB(r.setup)}</td><td>${dirB(r.direction)}</td>
      <td>${fP(r.price_entry)}</td>
      <td style="color:var(--red)">${fP(r.stop)}</td>
      <td style="color:var(--green)">${fP(r.tp1)}</td>
      <td style="color:rgba(0,255,157,.45)">${fP(r.tp2)}</td>
      <td><span class="${f>=0?'fp':'fn'}">${fPct(r.funding)}</span></td>
      <td style="color:${rC}">${rsi}</td>
      <td style="color:${parseFloat(oi)>0?'var(--green)':'var(--red)'}">${oi}</td>
      <td style="color:${parseFloat(cvd)>0?'var(--green)':'var(--red)'}">${cvd}</td>
      <td>${btcB(r.btc_trend_4h)}</td>
      <td style="color:${r.in_zone?'var(--cyan)':'var(--tdim)'}">${r.in_zone?'✓':'—'}</td>
      <td>${r.whale_flag?'<span class="bmini bwhale">🐋</span>':'<span style="color:var(--tdim)">—</span>'}</td>
      <td>${ocB(r.outcome_4h)}</td><td>${ocB(r.outcome_24h)}</td>
    </tr>`;
  }).join('');
}

/* ANALYTICS */
function ring(wr,lbl,n,col){
  const r=37,ci=2*Math.PI*r,fl=wr!=null?(wr/100)*ci:0;
  const c=col||(wr>=55?'#00ff9d':wr>=45?'#00d4ff':wr>=35?'#ffb800':'#ff3366');
  return`<div class="ring-item">
    <svg width="86" height="86" viewBox="0 0 100 100" overflow="visible">
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${c}" stroke-width="5.5" opacity=".1"/>
      <circle cx="50" cy="50" r="${r}" fill="none" stroke="${c}" stroke-width="5.5"
        stroke-dasharray="${fl.toFixed(1)} ${ci.toFixed(1)}" stroke-dashoffset="${(ci*.25).toFixed(1)}"
        transform="rotate(-90 50 50)" stroke-linecap="round"
        style="transition:stroke-dasharray 1.2s ease;filter:drop-shadow(0 0 4px ${c})"/>
      <text x="50" y="46" text-anchor="middle" fill="${c}" font-size="15" font-family="JetBrains Mono" font-weight="700">${wr!=null?wr+'%':'—'}</text>
      <text x="50" y="58" text-anchor="middle" fill="rgba(180,220,255,.3)" font-size="7" font-family="JetBrains Mono">${n!=null?'n='+n:''}</text>
    </svg>
    <div class="ring-lbl">${lbl}</div>
  </div>`;
}

function renderStats(st,s24){
  const bs=st?.by_setup||{},bd=st?.by_direction||{};
  const SETUPS=[{k:'squeeze',l:'SQUEEZE',c:'#00d4ff'},{k:'bos_fvg',l:'BOS/FVG',c:'#9d5cff'},{k:'short_dist',l:'SH.DIST',c:'#ff3366'},{k:'breakout',l:'PUMP',c:'#00ff9d'}];
  document.getElementById('rings').innerHTML=ring(st?.wr,'ALL TIME',st?.total)+SETUPS.map(s=>{const d=bs[s.k];return ring(d?.wr??null,s.l,d?.t,s.c)}).join('');
  const DIRS=[{k:'ЛОНГ',l:'LONG',c:'var(--green)'},{k:'ШОРТ',l:'SHORT',c:'var(--red)'},{k:'ЖДАТЬ',l:'WAIT',c:'var(--amber)'}];
  document.getElementById('dcards').innerHTML=DIRS.map(d=>{const dd=bd[d.k];return`<div class="dcard"><div class="dcard-wr" style="color:${d.c}">${dd?.wr!=null?dd.wr+'%':'—'}</div><div class="dcard-l">${d.l}</div><div class="dcard-n">n=${dd?.t??0}</div></div>`}).join('');
  const s=st||{},s2=s24||{};
  document.getElementById('spills').innerHTML=[
    {v:s.total??0,l:'TOTAL',c:'var(--cyan)'},{v:s.wins??0,l:'WINS',c:'var(--green)'},
    {v:s.stops??0,l:'STOPS',c:'var(--red)'},{v:s.losses??0,l:'LOSSES',c:'var(--amber)'},
    {v:s.flat??0,l:'FLAT',c:'var(--tdim)'},{v:s2.total??0,l:'24H SIG',c:'var(--cyan)'},
    {v:s2.wins??0,l:'24H WINS',c:'var(--green)'},
    {v:s2.wr!=null?s2.wr+'%':'—',l:'24H WR',c:s2.wr>=55?'var(--green)':s2.wr>=45?'var(--cyan)':s2.wr>=35?'var(--amber)':'var(--red)'},
  ].map(p=>`<div class="spill"><div class="spill-v" style="color:${p.c}">${p.v}</div><div class="spill-l">${p.l}</div></div>`).join('');
  const bh=st?.by_hour||{};
  document.getElementById('hmgrid').innerHTML=Array.from({length:24},(_,i)=>{
    const hd=bh[String(i)];const wr=hd?.wr;
    const bg=wr==null?'rgba(255,255,255,.02)':wr>=58?'rgba(0,255,157,.2)':wr>=48?'rgba(0,180,255,.18)':wr>=38?'rgba(255,184,0,.18)':'rgba(255,51,102,.18)';
    const bc=wr==null?'rgba(255,255,255,.05)':wr>=58?'#00ff9d':wr>=48?'#00d4ff':wr>=38?'#ffb800':'#ff3366';
    return`<div class="hmc" style="background:${bg};border-color:${bc}" title="${String(i).padStart(2,'0')}:00 UTC — ${wr!=null?wr+'%':'n/a'} (${hd?.t||0})"><div class="hmc-h">${String(i).padStart(2,'0')}</div><div class="hmc-w" style="color:${bc}">${wr!=null?wr:'—'}</div></div>`;
  }).join('');
  const SC={squeeze:'var(--cyan)',bos_fvg:'var(--purple)',short_dist:'var(--red)',breakout:'var(--green)'};
  document.getElementById('sbars').innerHTML=Object.entries(bs).sort((a,b)=>(b[1].wr??0)-(a[1].wr??0)).map(([k,v])=>{
    const wr=v.wr??0;const col=wr>=55?'var(--green)':wr>=45?'var(--cyan)':wr>=35?'var(--amber)':'var(--red)';const bc=SC[k]||'var(--cyan)';
    return`<div class="sbr"><div class="sbn">${k}</div><div class="sbt"><div class="sbf" style="width:${wr}%;background:${bc};box-shadow:0 0 5px ${bc}44"></div></div><div class="sbwr" style="color:${col}">${wr!=null?wr+'%':'—'}</div><div class="sbnn">n=${v.t}</div></div>`;
  }).join('');
}

function renderDots(resolved){
  const OC={TP1:'#00ff9d',WIN:'rgba(0,255,157,.5)',STOP:'#ff3366',LOSS:'#ffb800',FLAT:'#3a3a5a'};
  document.getElementById('hdots').innerHTML=[...resolved].reverse().map(r=>{
    const oc=r.outcome_24h||r.outcome_4h;const col=OC[oc]||'#1a1a3a';
    return`<div class="hd" style="background:${col};box-shadow:0 0 5px ${col}" title="${r.symbol||'?'} | ${r.direction||'?'} | ${r.setup||'?'}\n${oc||'pending'} | ${(r.run_ts||'').slice(0,16)}"></div>`;
  }).join('');
}

/* LIQUIDATIONS */
let _liqLoaded=false;
async function loadLiq(){
  if(_liqLoaded) return;
  try{
    const r=await fetch('/api/liq');if(!r.ok)throw new Error();
    const d=await r.json();
    if(d.error){document.getElementById('liq-totals').innerHTML=`<div class="empty"><div class="empty-ic">⚠️</div><div>${d.error}</div></div>`;return}
    renderLiqTotals(d.totals);
    renderLiqSymbols('liq-sym-1h',d.by_period?.['1h']||[]);
    renderLiqSymbols('liq-sym-4h',d.by_period?.['4h']||[]);
    renderLiqRecent(d.recent_large||[]);
    _liqLoaded=true;
    setTimeout(()=>{_liqLoaded=false},60000); // refresh every 60s
  }catch(e){document.getElementById('liq-totals').innerHTML='<div class="empty"><div class="empty-ic">💧</div><div>LIQUIDATION DATA UNAVAILABLE</div></div>'}
}

function renderLiqTotals(totals){
  const PERIODS=[['1h','1 HOUR'],['4h','4 HOURS'],['24h','24 HOURS']];
  document.getElementById('liq-totals').innerHTML=PERIODS.map(([k,l])=>{
    const t=totals?.[k]||{};const total=t.total_usd||0;const ls=t.long_usd||0;const ss=t.short_usd||0;
    const longPct=total>0?ls/total*100:50;const shortPct=total>0?ss/total*100:50;
    const domCol=ls>ss?'var(--green)':'var(--red)';const domTxt=ls>ss?'LONG DOM':'SHORT DOM';
    return`<div class="liq-period">
      <div class="liq-period-h">${l}</div>
      <div class="liq-total-usd" style="color:${domCol}">${fUSD(total)}</div>
      <div class="liq-bar-wrap">
        <div style="display:flex;height:100%;width:100%">
          <div style="width:${longPct.toFixed(1)}%;background:var(--green);height:100%;transition:width .8s;box-shadow:0 0 4px rgba(0,255,157,.3)"></div>
          <div style="width:${shortPct.toFixed(1)}%;background:var(--red);height:100%;transition:width .8s;box-shadow:0 0 4px rgba(255,51,102,.3)"></div>
        </div>
      </div>
      <div class="liq-side-row">
        <span style="color:var(--green)">LONGS ${fUSD(ls)}</span>
        <span style="color:var(--tdim);font-size:7px;font-weight:700">${domTxt}</span>
        <span style="color:var(--red)">SHORTS ${fUSD(ss)}</span>
      </div>
      <div style="font-size:7px;color:var(--tdim);margin-top:4px">${t.count||0} liquidations</div>
    </div>`;
  }).join('');
}

function renderLiqSymbols(elId,rows){
  // aggregate by symbol
  const bySymbol={};
  rows.forEach(r=>{
    if(!bySymbol[r.symbol]) bySymbol[r.symbol]={long:0,short:0,cnt:0};
    if(r.side==='long_liq') bySymbol[r.symbol].long+=r.usd;
    else bySymbol[r.symbol].short+=r.usd;
    bySymbol[r.symbol].cnt+=r.count;
  });
  const sorted=Object.entries(bySymbol).map(([sym,v])=>({sym,total:v.long+v.short,long:v.long,short:v.short,cnt:v.cnt})).sort((a,b)=>b.total-a.total).slice(0,12);
  const maxUSD=sorted[0]?.total||1;
  document.getElementById(elId).innerHTML=sorted.map(r=>{
    const pct=(r.total/maxUSD*100).toFixed(1);const domCol=r.long>r.short?'var(--green)':'var(--red)';
    const lPct=r.total>0?r.long/r.total*100:50;const sPct=r.total>0?r.short/r.total*100:50;
    return`<div class="liq-sym-row">
      <div class="liq-sym-name">${r.sym.replace('USDT','')}</div>
      <div class="liq-sym-bar-wrap">
        <div style="display:flex;height:100%;width:${pct}%">
          <div style="width:${lPct.toFixed(0)}%;background:rgba(0,255,157,.6);height:100%"></div>
          <div style="width:${sPct.toFixed(0)}%;background:rgba(255,51,102,.6);height:100%"></div>
        </div>
      </div>
      <div class="liq-sym-usd" style="color:${domCol}">${fUSD(r.total)}</div>
      <div class="liq-sym-cnt">${r.cnt}</div>
    </div>`;
  }).join('')||'<div class="empty" style="min-height:60px"><div>No data</div></div>';
}

function renderLiqRecent(rows){
  document.getElementById('liq-recent').innerHTML=rows.map(r=>{
    const isLong=r.side==='long_liq';const col=isLong?'var(--red)':'var(--green)';
    const sideLabel=isLong?'🔴 LONG LIQ':'🟢 SHORT LIQ';
    const ts=new Date(r.ts).toUTCString().slice(17,22)+' UTC';
    return`<tr>
      <td style="color:var(--tdim);font-size:9px">${ts}</td>
      <td style="font-weight:700">${(r.symbol||'').replace('USDT','')}</td>
      <td><span style="color:${col};font-size:8px;font-weight:700">${sideLabel}</span></td>
      <td>${fP(r.price)}</td>
      <td style="color:var(--tdim)">${fN(r.qty,3)}</td>
      <td style="color:${col};font-weight:700">${fUSD(r.usd)}</td>
    </tr>`;
  }).join('')||'<tr><td colspan="6" style="text-align:center;color:var(--tdim);padding:16px">No large liquidations in 4h</td></tr>';
}

/* CHARTS */
function renderCharts(charts){
  document.getElementById('nb-ch').textContent=charts.length;
  document.getElementById('tb-ch').textContent=charts.length;
  document.getElementById('cgrid').innerHTML=charts.map(n=>{
    const lbl=n.replace(/_1H_/,' ').replace('USDT','').replace('.png','');
    return`<div class="cthumb" onclick="openModal('/charts/${n}')"><img src="/charts/${n}" loading="lazy" alt="${n}"><div class="clbl">${lbl}</div></div>`;
  }).join('');
}

/* SIDEBAR */
function renderSide(svcs,filters,market){
  const SN={bot:'Telegram Bot',screener:'Screener',channelreader:'Chan Reader',liqtracker:'Liq Tracker'};
  document.getElementById('svcs').innerHTML=Object.entries(svcs||{}).map(([k,ok])=>
    `<div class="svc-row"><div class="svcd ${ok?'ok':'err'}"></div><span class="svcn">${SN[k]||k}</span><span class="svcs ${ok?'ok':'err'}">${ok?'OK':'DOWN'}</span></div>`).join('');
  const pos=market?.btc_trend_4h,br=market?.alt_breadth_pct;
  const pC={above:'var(--green)',below:'var(--red)',between:'var(--amber)'}[pos]||'var(--tdim)';
  const bC=br>80?'var(--green)':br>50?'var(--amber)':'var(--red)';
  document.getElementById('macro').innerHTML=`
    <div class="mr"><span class="ml">BTC EMA</span><span class="mv" style="color:${pC}">${pos||'—'}</span></div>
    <div class="mr"><span class="ml">Alt Breadth</span><span class="mv" style="color:${bC}">${br!=null?br+'%':'—'}</span></div>
    <div class="mr"><span class="ml">Last Scan</span><span class="mv" style="font-size:8px">${fT(market?.last_scan)}</span></div>`;
  const FL={f5_btc_above_block:'F5: Block shorts ↑EMA',f6_short_dist_below:'F6: Short dist ↓only',f2_between_penalty_30:'F2: −30 pts between'};
  document.getElementById('fltrs').innerHTML=Object.entries(FL).filter(([k])=>filters?.[k]).map(([k,v])=>`<div class="fchip">${v}</div>`).join('')||'<div style="padding:3px 12px;font-size:8px;color:var(--tdim)">none</div>';
}

/* HEADER */
function renderHdr(market,active,s24,sat){
  const btc=active?.find(s=>s.symbol==='BTCUSDT');
  if(btc)document.getElementById('h-btc').textContent='$'+parseFloat(btc.price_entry).toLocaleString('en',{maximumFractionDigits:0});
  const br=market?.alt_breadth_pct,bel=document.getElementById('h-brd');
  if(br!=null){bel.textContent=br+'%';bel.style.color=br>80?'var(--green)':br>50?'var(--amber)':'var(--red)'}
  const pos=market?.btc_trend_4h,eel=document.getElementById('h-ema');
  eel.textContent={above:'↑ ABOVE',below:'↓ BELOW',between:'~ MID'}[pos]||pos||'—';
  eel.style.color={above:'var(--green)',below:'var(--red)',between:'var(--amber)'}[pos]||'var(--tdim)';
  document.getElementById('h-act').textContent=active?.length??'—';
  const w24=s24?.wr,w24e=document.getElementById('h-wr24');
  if(w24!=null){w24e.textContent=w24+'%';w24e.style.color=w24>=55?'var(--green)':w24>=45?'var(--cyan)':w24>=35?'var(--amber)':'var(--red)'}
  const wat=sat?.wr,wate=document.getElementById('h-wrat');
  if(wat!=null){wate.textContent=wat+'%';wate.style.color=wat>=55?'var(--green)':wat>=45?'var(--cyan)':wat>=35?'var(--amber)':'var(--red)'}
  document.getElementById('h-scan').textContent=fT(market?.last_scan);
}

/* JOURNAL */
function mdToHtml(md){
  return md.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/^# (.+)$/mg,'<h1>$1</h1>').replace(/^## (.+)$/mg,'<h2>$1</h2>').replace(/^### (.+)$/mg,'<h3>$1</h3>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`(.+?)`/g,'<code>$1</code>')
    .replace(/^---$/mg,'<hr>').replace(/^[-•] (.+)$/mg,'<li>$1</li>')
    .replace(/^\□ (.+)$/mg,'<li style="list-style:none">☐ $1</li>')
    .replace(/\n{2,}/g,'<br><br>');
}
async function loadJournal(){
  const el=document.getElementById('jbody');
  try{
    const r=await fetch('/api/obsidian');if(!r.ok)throw new Error();
    const d=await r.json();
    if(d.journal){el.innerHTML=`<div class="jcontent">${mdToHtml(d.journal)}</div>`;return}
    if(d.notes&&Object.keys(d.notes).length){
      el.innerHTML=Object.entries(d.notes).slice(0,8).map(([nm,ct])=>
        `<div class="card" style="margin-bottom:10px"><div class="chdr">${nm}</div><div class="cbody"><div class="jcontent">${mdToHtml(ct.slice(0,2000))}</div></div></div>`
      ).join('');return}
    el.innerHTML='<div class="empty"><div class="empty-ic">📔</div><div>OBSIDIAN NOT CONFIGURED</div></div>';
  }catch(e){el.innerHTML='<div class="empty"><div class="empty-ic">📔</div><div>JOURNAL UNAVAILABLE</div></div>'}
}

/* MAIN REFRESH */
async function refresh(){
  try{
    const r=await fetch('/api/data');if(!r.ok)return;
    const d=await r.json();
    renderHdr(d.market,d.active_signals,d.stats_24h,d.stats);
    renderLive(d.active_signals||[]);
    _cands=d.top_candidates||[];
    document.getElementById('nb-scr').textContent=_cands.length;
    document.getElementById('tb-scr').textContent=_cands.length;
    applyScreener();
    renderStats(d.stats,d.stats_24h);
    renderDots(d.recent_resolved||[]);
    renderCharts(d.charts||[]);
    renderSide(d.services,d.filters,d.market);
  }catch(e){console.error('[NEXUS]',e)}
}
refresh();setInterval(refresh,20000);
loadJournal();

/* MODALS */
function openModal(src){document.getElementById('mimg').src=src;document.getElementById('modal').classList.add('open')}
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('mimg').src=''}
document.getElementById('modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeSigModal()}});
</script>
</body>
</html>"""

if __name__ == "__main__":
    try: build_data()
    except Exception as e: print(f"[warn] cache warm: {e}")
    server = ThreadedServer(("", PORT), Handler)
    print(f"\n  NEXUS Trading Terminal v2")
    print(f"  ➜  http://localhost:{PORT}\n")
    print("  Press Ctrl+C to stop")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped.")
