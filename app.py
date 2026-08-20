from flask import Flask, jsonify, render_template_string, request
import requests, math, time, threading
import pandas as pd
import numpy as np

app = Flask(__name__)

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"
FEAR_GREED_URL = "https://api.alternative.me/fng/"

session = requests.Session()
session.headers.update({"User-Agent":"CryptoRadarModes/1.0 research-only"})

EXCLUDED = {"USDC","FDUSD","TUSD","USDP","DAI","USDS","EUR","TRY","BRL","AEUR","BIDR","PAX","USTC"}

MODES = {
    "conservative": {
        "label":"Conservative",
        "risk_pct":0.50,
        "max_position_pct":20.0,
        "min_score":84.0,
        "min_conf":72.0,
        "min_liq":100_000_000,
        "max_spread":5.0,
        "max_atr":4.0,
        "universe":20,
        "deep":10,
    },
    "aggressive": {
        "label":"Aggressive",
        "risk_pct":1.25,
        "max_position_pct":20.0,
        "min_score":76.0,
        "min_conf":62.0,
        "min_liq":8_000_000,
        "max_spread":15.0,
        "max_atr":8.0,
        "universe":60,
        "deep":28,
    },
    "very_aggressive": {
        "label":"Very Aggressive",
        "risk_pct":2.00,
        "max_position_pct":15.0,
        "min_score":70.0,
        "min_conf":56.0,
        "min_liq":3_000_000,
        "max_spread":25.0,
        "max_atr":12.0,
        "universe":100,
        "deep":40,
    }
}

cache = {}
scan_lock = threading.Lock()

def sf(x,d=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else d
    except:
        return d

def clamp(x,a=0,b=100):
    return max(a,min(b,x))

def pct(a,b):
    return (a/b-1)*100 if b else 0.0

def gj(base,path="",params=None):
    r=session.get(base+path,params=params,timeout=12)
    r.raise_for_status()
    return r.json()

def ema(s,n):
    return s.ewm(span=n,adjust=False).mean()

def rsi(s,n=14):
    d=s.diff()
    up=d.clip(lower=0)
    dn=-d.clip(upper=0)
    ag=up.ewm(alpha=1/n,adjust=False).mean()
    al=dn.ewm(alpha=1/n,adjust=False).mean()
    rs=ag/al.replace(0,np.nan)
    return (100-100/(1+rs)).fillna(50)

def atr(df,n=14):
    tr=pd.concat([
        df.high-df.low,
        (df.high-df.close.shift()).abs(),
        (df.low-df.close.shift()).abs()
    ],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False).mean()

def macd_hist(s):
    line=ema(s,12)-ema(s,26)
    sig=ema(line,9)
    return line-sig

def get_klines(sym,tf,limit=180):
    raw=gj(SPOT_BASE,"/api/v3/klines",{"symbol":sym,"interval":tf,"limit":limit})
    cols=["ot","open","high","low","close","volume","ct","quote_volume","trades","tb","tq","x"]
    df=pd.DataFrame(raw,columns=cols)
    for c in ["open","high","low","close","volume","quote_volume","tb","tq"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")
    return df.dropna()

def tf_metrics(df):
    c=df.close
    e20,e50=ema(c,20),ema(c,50)
    rr=rsi(c)
    aa=atr(df)
    mh=macd_hist(c)
    typical=(df.high+df.low+df.close)/3
    vw=(typical*df.quote_volume).rolling(30).sum()/df.quote_volume.rolling(30).sum().replace(0,np.nan)
    p=sf(c.iloc[-1])
    avg_vol=sf(df.quote_volume.iloc[-21:-1].mean(),1)
    buy=sf(df.tq.iloc[-6:].sum())
    total=sf(df.quote_volume.iloc[-6:].sum(),1)
    a=sf(aa.iloc[-1])
    return {
        "price":p,
        "ema20":sf(e20.iloc[-1]),
        "ema50":sf(e50.iloc[-1]),
        "rsi":sf(rr.iloc[-1],50),
        "atr":a,
        "atr_pct":a/p*100 if p else 0,
        "macd":sf(mh.iloc[-1]),
        "macd_prev":sf(mh.iloc[-2]),
        "vwap":sf(vw.iloc[-1],p),
        "vol_ratio":sf(df.quote_volume.iloc[-1])/avg_vol if avg_vol else 1,
        "taker_buy":buy/total if total else .5,
        "mom12":pct(p,sf(c.iloc[-13],p))
    }

def universe(mode):
    m=MODES[mode]
    rows=[]
    for t in gj(SPOT_BASE,"/api/v3/ticker/24hr"):
        s=t.get("symbol","")
        if not s.endswith("USDT"):
            continue
        base=s[:-4]
        if base in EXCLUDED or base.endswith(("UP","DOWN","BULL","BEAR")):
            continue
        qv=sf(t.get("quoteVolume"))
        p=sf(t.get("lastPrice"))
        if p<=0 or qv<m["min_liq"]:
            continue
        rows.append({
            "symbol":s,
            "qv":qv,
            "pct24":sf(t.get("priceChangePercent"))
        })
    rows.sort(key=lambda x:x["qv"],reverse=True)
    return rows[:m["universe"]]

def book(sym,mid):
    b=gj(SPOT_BASE,"/api/v3/depth",{"symbol":sym,"limit":100})
    bids=[(sf(p),sf(q)) for p,q in b.get("bids",[])]
    asks=[(sf(p),sf(q)) for p,q in b.get("asks",[])]
    if not bids or not asks:
        return {"spread":999,"depth":0,"imbalance":0}
    spread=(asks[0][0]-bids[0][0])/mid*10000
    lo,hi=mid*.995,mid*1.005
    bd=sum(p*q for p,q in bids if p>=lo)
    ad=sum(p*q for p,q in asks if p<=hi)
    total=bd+ad
    return {
        "spread":spread,
        "depth":total,
        "imbalance":(bd-ad)/total if total else 0
    }

def deriv(sym):
    o={"funding":0.0,"oi_change":0.0,"taker_ratio":1.0,"available":False}
    try:
        pr=gj(FUTURES_BASE,"/fapi/v1/premiumIndex",{"symbol":sym})
        o["funding"]=sf(pr.get("lastFundingRate"))*100
        hist=gj(FUTURES_BASE,"/futures/data/openInterestHist",{"symbol":sym,"period":"5m","limit":3})
        if len(hist)>=2:
            old=sf(hist[0].get("sumOpenInterest"))
            new=sf(hist[-1].get("sumOpenInterest"))
            o["oi_change"]=pct(new,old) if old else 0
        tk=gj(FUTURES_BASE,"/futures/data/takerlongshortRatio",{"symbol":sym,"period":"5m","limit":1})
        if tk:
            o["taker_ratio"]=sf(tk[-1].get("buySellRatio"),1)
        o["available"]=True
    except:
        pass
    return o

def fear_greed():
    try:
        d=session.get(FEAR_GREED_URL,timeout=8).json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except:
        return 50,"Unavailable"

def btc_regime():
    try:
        m15=tf_metrics(get_klines("BTCUSDT","15m"))
        h1=tf_metrics(get_klines("BTCUSDT","1h"))
        s=0
        s += 1 if h1["price"]>h1["ema20"]>h1["ema50"] else -1
        s += 1 if m15["price"]>m15["vwap"] else -1
        s += 1 if h1["macd"]>0 else -1
        return "RISK-ON" if s>=2 else "RISK-OFF" if s<=-2 else "MIXED"
    except:
        return "UNKNOWN"

def score_coin(meta,tfs,bm,dm,regime,fg,mode):
    cfg=MODES[mode]
    m5,m15,h1=tfs["5m"],tfs["15m"],tfs["1h"]
    p=m5["price"]
    sc=50.0
    why=[]
    risks=[]

    votes=0
    for x,w in [(m5,1),(m15,2),(h1,3)]:
        if x["price"]>x["ema20"]>x["ema50"]:
            votes+=w
        elif x["price"]<x["ema20"]<x["ema50"]:
            votes-=w
    sc += votes*3.5
    if votes>=4:
        why.append("Trend aligned 5m/15m/1h")
    elif votes<=-4:
        risks.append("Higher-timeframe downtrend")

    if 52<=m5["rsi"]<=68:
        sc+=6
        why.append(f"RSI {m5['rsi']:.0f}")
    elif m5["rsi"]>=78:
        sc-=10
        risks.append("Overbought RSI")

    if m5["macd"]>0 and m5["macd"]>m5["macd_prev"]:
        sc+=5
        why.append("MACD improving")

    if p>m5["vwap"]:
        sc+=4
        why.append("Above VWAP")
    else:
        sc-=3

    if mode=="conservative":
        if .15<=m5["mom12"]<=2.5:
            sc+=5
    else:
        if .25<=m5["mom12"]<=5.0:
            sc+=8
            why.append("Strong breakout momentum")
        elif 5.0<m5["mom12"]<=8.0:
            sc+=2
            risks.append("Very extended momentum")
        elif m5["mom12"]>8:
            sc-=8
            risks.append("Extreme overextension")

    if mode=="conservative":
        if m5["vol_ratio"]>=1.8:
            sc+=7
            why.append(f"{m5['vol_ratio']:.1f}x volume")
        elif m5["vol_ratio"]>=1.25:
            sc+=3
    else:
        if m5["vol_ratio"]>=2.5:
            sc+=10
            why.append(f"{m5['vol_ratio']:.1f}x breakout volume")
        elif m5["vol_ratio"]>=1.6:
            sc+=6
        elif m5["vol_ratio"]>=1.2:
            sc+=2

    if m5["taker_buy"]>=.56:
        sc+=4
        why.append("Spot buyers aggressive")
    elif m5["taker_buy"]<=.44:
        sc-=4

    if bm["spread"]<=2:
        sc+=5
    elif bm["spread"]>cfg["max_spread"]:
        sc-=12
        risks.append("Spread too wide")

    if bm["depth"]>=2_000_000:
        sc+=4
    elif bm["depth"]<150_000:
        sc-=10
        risks.append("Thin order book")

    if bm["imbalance"]>=.12:
        sc+=5
        why.append("Bid-heavy order book")
    elif bm["imbalance"]<=-.18:
        sc-=6
        risks.append("Ask-heavy order book")

    if dm["available"]:
        if -.01<=dm["funding"]<=.025:
            sc+=2
        elif dm["funding"]>.08:
            sc-=8
            risks.append("Crowded funding")
        if 0<dm["oi_change"]<=4:
            sc+=4
            why.append("Open interest rising")
        elif dm["oi_change"]>8:
            sc-=4
            risks.append("OI expanding too fast")
        if dm["taker_ratio"]>=1.08:
            sc+=3
        elif dm["taker_ratio"]<.85:
            sc-=3

    if regime=="RISK-ON":
        sc+=5
        why.append("BTC risk-on")
    elif regime=="RISK-OFF":
        sc-=9
        risks.append("BTC risk-off")

    if fg>=85:
        sc-=3
        risks.append("Extreme greed")
    elif fg<=15:
        sc-=2
        risks.append("Extreme fear")

    hard_no = (
        meta["qv"] < cfg["min_liq"] or
        bm["spread"] > cfg["max_spread"] or
        m5["atr_pct"] > cfg["max_atr"]
    )

    if h1["price"]<h1["ema50"] and m15["price"]<m15["ema50"]:
        sc=min(sc,59)

    sc=clamp(sc)
    conf=clamp(45+abs(votes)/6*35+min(meta["qv"]/1e9,1)*10)
    if not dm["available"]:
        conf*=.85

    a=max(m5["atr"],p*.002)
    center=max(m5["ema20"],min(p,m5["vwap"]))
    entry_low=center-.20*a
    entry_high=center+.30*a
    entry_mid=(entry_low+entry_high)/2
    stop=entry_low-1.30*a
    risk_per_coin=max(entry_mid-stop,p*.001)
    target1=entry_mid+1.5*risk_per_coin
    target2=entry_mid+2.5*risk_per_coin
    rr=(target2-entry_mid)/risk_per_coin

    qualifies = (
        not hard_no and
        sc>=cfg["min_score"] and
        conf>=cfg["min_conf"] and
        rr>=2.0
    )

    return {
        "symbol":meta["symbol"],
        "score":round(sc,1),
        "confidence":round(conf,1),
        "qualifies":qualifies,
        "price":p,
        "pct24":round(meta["pct24"],2),
        "qv":meta["qv"],
        "spread":round(bm["spread"],2),
        "depth":round(bm["depth"],2),
        "imbalance":round(bm["imbalance"],3),
        "funding":round(dm["funding"],4),
        "oi_change":round(dm["oi_change"],2),
        "entry_low":entry_low,
        "entry_high":entry_high,
        "stop":stop,
        "target1":target1,
        "target2":target2,
        "rr":round(rr,2),
        "why":why[:7] or ["No strong confirmations"],
        "risks":risks[:7] or ["None"]
    }

def size_position(c,account,mode):
    cfg=MODES[mode]
    entry=(c["entry_low"]+c["entry_high"])/2
    risk_per_coin=max(entry-c["stop"],entry*.001)
    max_loss=account*(cfg["risk_pct"]/100)
    qty_by_risk=max_loss/risk_per_coin
    max_notional=account*(cfg["max_position_pct"]/100)
    qty=min(qty_by_risk,max_notional/entry)
    return {
        "notional":qty*entry,
        "account_risk":qty*risk_per_coin,
        "risk_pct":qty*risk_per_coin/account*100 if account else 0
    }

def run_scan(mode,account):
    fg,fg_label=fear_greed()
    regime=btc_regime()
    results=[]
    for meta in universe(mode)[:MODES[mode]["deep"]]:
        try:
            tfs={tf:tf_metrics(get_klines(meta["symbol"],tf)) for tf in ("5m","15m","1h")}
            bm=book(meta["symbol"],tfs["5m"]["price"])
            dm=deriv(meta["symbol"])
            results.append(score_coin(meta,tfs,bm,dm,regime,fg,mode))
        except:
            pass

    results.sort(key=lambda x:(x["score"],x["confidence"]),reverse=True)
    qualified=[x for x in results if x["qualifies"]]
    best=qualified[0] if qualified else None

    if best:
        best=dict(best)
        best["sizing"]=size_position(best,account,mode)

    return {
        "updated":int(time.time()),
        "mode":mode,
        "mode_label":MODES[mode]["label"],
        "account":account,
        "regime":regime,
        "fear_greed":{"value":fg,"label":fg_label},
        "decision":"TRADE" if best else "NO TRADE",
        "best":best,
        "ranked":results[:10]
    }

@app.route("/api/scan")
def api_scan():
    mode=request.args.get("mode","aggressive")
    if mode not in MODES:
        mode="aggressive"
    try:
        account=float(request.args.get("account","1000"))
        account=max(50,min(account,10_000_000))
    except:
        account=1000
    return jsonify(run_scan(mode,account))

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name":"Crypto Radar Pro",
        "short_name":"Crypto Radar",
        "start_url":"/",
        "display":"standalone",
        "background_color":"#0b0d12",
        "theme_color":"#0b0d12",
        "icons":[]
    })

PAGE=r"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12">
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Crypto Radar Pro</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0b0d12;color:#eef2f7;font-family:-apple-system,BlinkMacSystemFont,Inter,Arial,sans-serif}
.wrap{max-width:900px;margin:auto;padding:calc(env(safe-area-inset-top) + 18px) 14px 40px}
h1{font-size:27px;margin:0 0 3px}.sub{color:#8c96a6;font-size:13px}
.controls{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:16px 0}
select,input,button{width:100%;box-sizing:border-box;border:1px solid #283041;background:#151923;color:#eef2f7;border-radius:12px;padding:12px;font-size:15px}
button{grid-column:1/-1;background:#eef2f7;color:#101216;font-weight:800;border:none}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.pill{background:#151923;border:1px solid #252c3b;padding:8px 10px;border-radius:10px;font-size:12px}
.hero{background:#121720;border:2px solid #49566d;border-radius:18px;padding:16px;margin:12px 0}
.no{border-color:#5a3940}.decision{font-size:12px;color:#99a3b3}.pair{font-size:29px;font-weight:900;margin:3px 0}.score{font-size:15px;color:#aeb7c6}
.money{font-size:30px;font-weight:900;margin:15px 0}.money small{display:block;font-size:12px;font-weight:500;color:#9da6b5;margin-top:3px}
.levels{background:#0e1218;border-radius:12px;padding:11px;line-height:1.75;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:11px 0}.metric{background:#191f2a;border-radius:10px;padding:8px;font-size:11px;color:#99a3b3}.metric b{display:block;color:#eef2f7;font-size:13px;margin-top:3px}
.reason{font-size:13px;line-height:1.45;margin-top:10px}.rank{background:#121720;border:1px solid #222938;border-radius:14px;padding:12px;margin:8px 0;display:flex;justify-content:space-between}.muted{color:#8e98a8}
.foot{font-size:11px;color:#707a8b;line-height:1.45;margin-top:18px}
@media(max-width:430px){.grid{grid-template-columns:repeat(2,1fr)}}
</style></head><body><div class="wrap">
<h1>Crypto Radar Pro</h1><div class="sub">Pick a risk mode, enter your account size, scan the market.</div>

<div class="controls">
<select id="mode">
<option value="conservative">Conservative</option>
<option value="aggressive" selected>Aggressive</option>
<option value="very_aggressive">Very Aggressive</option>
</select>
<input id="account" type="number" value="1000" min="50" step="50" placeholder="Account size">
<button id="scan">SCAN MARKET</button>
</div>

<div class="meta"><div class="pill" id="regime">BTC: —</div><div class="pill" id="fg">Fear & Greed: —</div><div class="pill" id="time">Updated: —</div></div>
<div id="result"><div class="hero"><div class="muted">Press SCAN MARKET</div></div></div>
<div id="ranked"></div>
<div class="foot">Research-only decision engine. “Trade” means the rules currently qualify the setup; it is not a guarantee. Suggested position size is based on the selected risk mode and stop distance. Never chase price outside the displayed entry zone.</div>
</div>

<script>
const money=x=>{if(x>=1000)return '$'+x.toLocaleString(undefined,{maximumFractionDigits:2});if(x>=1)return '$'+x.toFixed(4);if(x>=.01)return '$'+x.toFixed(5);return '$'+x.toFixed(8)}
const btn=document.getElementById('scan');

async function run(){
  btn.disabled=true;btn.textContent='SCANNING…';
  const mode=document.getElementById('mode').value;
  const account=document.getElementById('account').value||1000;
  try{
    const r=await fetch(`/api/scan?mode=${encodeURIComponent(mode)}&account=${encodeURIComponent(account)}`);
    const d=await r.json();
    document.getElementById('regime').textContent='BTC: '+d.regime;
    document.getElementById('fg').textContent='Fear & Greed: '+d.fear_greed.value+' · '+d.fear_greed.label;
    document.getElementById('time').textContent='Updated: '+new Date(d.updated*1000).toLocaleTimeString();

    if(d.decision==='TRADE'){
      const c=d.best;
      document.getElementById('result').innerHTML=`
      <div class="hero">
        <div class="decision">BEST TRADE RIGHT NOW · ${d.mode_label}</div>
        <div class="pair">${c.symbol}</div>
        <div class="score">Score ${c.score}/100 · Confidence ${c.confidence}/100</div>
        <div class="money">$${c.sizing.notional.toFixed(2)}
          <small>Suggested capital · estimated account risk $${c.sizing.account_risk.toFixed(2)} (${c.sizing.risk_pct.toFixed(2)}%)</small>
        </div>
        <div class="levels">
          <b>Entry:</b> ${money(c.entry_low)} – ${money(c.entry_high)}<br>
          <b>Stop:</b> ${money(c.stop)}<br>
          <b>Target 1:</b> ${money(c.target1)}<br>
          <b>Target 2:</b> ${money(c.target2)}<br>
          <b>Risk / reward:</b> 1:${c.rr}
        </div>
        <div class="grid">
          <div class="metric">24h move<b>${c.pct24>=0?'+':''}${c.pct24}%</b></div>
          <div class="metric">Spread<b>${c.spread} bp</b></div>
          <div class="metric">Book depth<b>$${(c.depth/1e6).toFixed(2)}M</b></div>
          <div class="metric">Imbalance<b>${c.imbalance>=0?'+':''}${c.imbalance}</b></div>
          <div class="metric">Funding<b>${c.funding}%</b></div>
          <div class="metric">OI change<b>${c.oi_change>=0?'+':''}${c.oi_change}%</b></div>
        </div>
        <div class="reason"><b>Why:</b> ${c.why.join(' · ')}<br><b>Risks:</b> ${c.risks.join(' · ')}</div>
      </div>`;
    }else{
      document.getElementById('result').innerHTML=`
      <div class="hero no">
        <div class="decision">${d.mode_label}</div>
        <div class="pair">NO TRADE</div>
        <div class="muted">Nothing currently meets this mode's score, confidence, liquidity and risk rules.</div>
      </div>`;
    }

    document.getElementById('ranked').innerHTML='<h3>Top ranked</h3>'+d.ranked.slice(0,6).map((c,i)=>`
      <div class="rank"><span>${i+1}. <b>${c.symbol}</b><br><span class="muted">${c.qualifies?'QUALIFIED':'WAIT'}</span></span><span><b>${c.score}</b>/100<br><span class="muted">conf ${c.confidence}</span></span></div>
    `).join('');
  }catch(e){
    document.getElementById('result').innerHTML=`<div class="hero no"><div class="pair">SCAN FAILED</div><div class="muted">${e}</div></div>`;
  }
  btn.disabled=false;btn.textContent='SCAN MARKET';
}
btn.onclick=run;
</script>
</body></html>"""

@app.route("/")
def index():
    return render_template_string(PAGE)

@app.route("/health")
def health():
    return {"ok":True}

if __name__=="__main__":
    app.run(host="0.0.0.0",port=5000)
