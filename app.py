from flask import Flask, jsonify, render_template_string, request
import requests, math, time, re, html, os, threading
import pandas as pd
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

app = Flask(__name__)

# ----------------------- CONFIG -----------------------

COINGECKO = "https://api.coingecko.com/api/v3"
FINNHUB = "https://finnhub.io/api/v1"
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

# Optional. If provided, CoinMarketCal-style future catalyst API support can be added later.
COINMARKETCAL_API_KEY = os.environ.get("COINMARKETCAL_API_KEY", "").strip()

AUTO_SCAN_SECONDS = int(os.environ.get("AUTO_SCAN_SECONDS", "900"))

HEADERS = {"User-Agent":"MarketOpportunityEngine/1.0 research-only"}
session = requests.Session()
session.headers.update(HEADERS)

ASSET_TYPES = ("all","crypto","stocks")
HORIZONS = {
    "24h":{"label":"Next 24 Hours","momentum_w":1.35,"news_w":1.35,"fund_w":0.25,"catalyst_w":1.25},
    "3d":{"label":"Next 3 Days","momentum_w":1.25,"news_w":1.30,"fund_w":0.35,"catalyst_w":1.30},
    "1w":{"label":"Next Week","momentum_w":1.10,"news_w":1.25,"fund_w":0.50,"catalyst_w":1.35},
    "1m":{"label":"Next Month","momentum_w":0.90,"news_w":1.15,"fund_w":0.75,"catalyst_w":1.45},
    "3m":{"label":"Next 3 Months","momentum_w":0.65,"news_w":0.95,"fund_w":1.10,"catalyst_w":1.30},
    "1y":{"label":"1 Year+","momentum_w":0.35,"news_w":0.55,"fund_w":1.55,"catalyst_w":0.80},
}
RISKS = {
    "conservative":{"label":"Conservative","min_liq":0.12,"smallcap_bonus":0,"vol_bonus":0.15,"quality_bonus":1.35,"moonshot":False},
    "moderate":{"label":"Moderate","min_liq":0.07,"smallcap_bonus":0.20,"vol_bonus":0.35,"quality_bonus":1.05,"moonshot":False},
    "aggressive":{"label":"Aggressive","min_liq":0.025,"smallcap_bonus":0.55,"vol_bonus":0.70,"quality_bonus":0.75,"moonshot":False},
    "extreme":{"label":"Extreme / Moonshot","min_liq":0.006,"smallcap_bonus":1.20,"vol_bonus":1.15,"quality_bonus":0.45,"moonshot":True},
}

POSITIVE = {
    "approval","approved","partnership","partners","integration","integrates","launch","launches",
    "upgrade","upgrades","release","releases","expands","expansion","adoption","adopts","contract",
    "award","wins","record","growth","surge","breakthrough","successful","success","phase 3",
    "phase iii","fda","etf","listing","listed","mainnet","testnet","burn","buyback","acquisition",
    "acquires","merger","collaboration","ai","artificial intelligence","roadmap","milestone",
}
NEGATIVE = {
    "hack","hacked","exploit","breach","lawsuit","investigation","probe","fraud","scam","outage",
    "delay","delayed","delist","delisting","bankruptcy","default","recall","rejected","rejection",
    "ban","banned","charges","liquidation","selloff","plunge","crash","misses","miss","warning",
    "downgrade","dilution","offering","unlock","token unlock",
}
FUTURE_WORDS = {
    "upcoming","soon","next","will","plans","planned","scheduled","expected","expects","launch",
    "release","upgrade","roadmap","conference","vote","decision","trial","earnings","mainnet",
    "listing","unlock","burn","airdrop","deadline","approval"
}

cache = {}
cache_lock = threading.Lock()
scanner_started = False

# ----------------------- HELPERS -----------------------

def sf(x,d=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else d
    except:
        return d

def clamp(x,a=0,b=100):
    return max(a,min(b,x))

def zscore(vals):
    vals=[sf(v) for v in vals]
    if not vals:
        return []
    s=pd.Series(vals)
    std=float(s.std()) or 1.0
    mean=float(s.mean())
    return [float((x-mean)/std) for x in vals]

def req_json(url, params=None, timeout=18):
    r=session.get(url,params=params,timeout=timeout)
    r.raise_for_status()
    return r.json()

def parse_rss(url, limit=20):
    try:
        r=session.get(url,timeout=12)
        r.raise_for_status()
        root=ET.fromstring(r.content)
        out=[]
        for item in root.findall(".//item")[:limit]:
            title=item.findtext("title") or ""
            link=item.findtext("link") or ""
            pub=item.findtext("pubDate") or ""
            title=html.unescape(re.sub(r"<[^>]+>"," ",title)).strip()
            if title:
                out.append({"title":title,"link":link,"published":pub})
        return out
    except:
        return []

def google_news(query, days=30, limit=12):
    q=quote_plus(f'{query} when:{days}d')
    url=f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return parse_rss(url,limit)

def sentiment_of_headline(title):
    t=title.lower()
    pos=sum(1 for w in POSITIVE if w in t)
    neg=sum(1 for w in NEGATIVE if w in t)
    fut=sum(1 for w in FUTURE_WORDS if w in t)
    return pos-neg, fut

def news_catalyst_metrics(name, symbol, horizon):
    days={"24h":3,"3d":7,"1w":14,"1m":45,"3m":100,"1y":365}[horizon]
    # Company/coin name is generally safer than ticker-only search.
    headlines=google_news(f'"{name}" {symbol}',days=days,limit=12)
    sent=0
    future=0
    recent=0
    catalyst_titles=[]
    for h in headlines:
        s,f=sentiment_of_headline(h["title"])
        sent+=s
        future+=f
        if s or f:
            catalyst_titles.append(h["title"])
        recent+=1
    # Scale lightly so headlines don't dominate.
    sent_score=clamp(50 + sent*7, 5, 95)
    catalyst_score=clamp(40 + future*8 + max(sent,0)*3, 5, 95)
    return {
        "news_score":sent_score,
        "catalyst_score":catalyst_score,
        "headlines":headlines[:5],
        "catalyst_titles":catalyst_titles[:4],
        "mentions":recent
    }

def priced_in_penalty(change24, change7, catalyst_score):
    # Strong recent runs may mean a known catalyst is already partly reflected.
    run=max(abs(change24), abs(change7)/2)
    if catalyst_score < 60:
        return 0
    if run > 25: return 12
    if run > 15: return 8
    if run > 8: return 4
    return 0

# ----------------------- CRYPTO -----------------------

def get_crypto_universe():
    rows=[]
    # Top 500 by market cap; broad enough for practical free-tier scanning.
    for page in (1,2):
        try:
            j=req_json(
                f"{COINGECKO}/coins/markets",
                {
                    "vs_currency":"usd","order":"market_cap_desc","per_page":250,"page":page,
                    "sparkline":"false","price_change_percentage":"1h,24h,7d,30d"
                }
            )
            rows.extend(j)
        except:
            break
    return rows

def prelim_crypto(rows, horizon, risk):
    if not rows:
        return []
    vols=[sf(x.get("total_volume")) for x in rows]
    mcaps=[sf(x.get("market_cap")) for x in rows]
    zv=zscore([math.log10(max(v,1)) for v in vols])
    zm=zscore([math.log10(max(m,1)) for m in mcaps])

    out=[]
    for i,x in enumerate(rows):
        mcap=sf(x.get("market_cap"))
        vol=sf(x.get("total_volume"))
        if mcap<=0 or vol<=0: continue
        liq=vol/mcap
        if liq<RISKS[risk]["min_liq"]: continue

        ch1=sf(x.get("price_change_percentage_1h_in_currency"))
        ch24=sf(x.get("price_change_percentage_24h_in_currency"))
        ch7=sf(x.get("price_change_percentage_7d_in_currency"))
        ch30=sf(x.get("price_change_percentage_30d_in_currency"))

        mom={
            "24h": ch1*0.45 + ch24*0.55,
            "3d": ch24*0.55 + ch7*0.45,
            "1w": ch24*0.25 + ch7*0.75,
            "1m": ch7*0.35 + ch30*0.65,
            "3m": ch30,
            "1y": ch30*0.25
        }[horizon]

        smallcap=max(0,-zm[i]) * RISKS[risk]["smallcap_bonus"]
        volatility=(abs(ch24)*0.25 + abs(ch7)*0.08) * RISKS[risk]["vol_bonus"]
        quality=max(0,zv[i]) * RISKS[risk]["quality_bonus"]
        momentum=clamp(50+mom*1.4,0,100)

        # Crypto long-term "fundamental proxy": market cap + liquidity + persistence.
        fund=clamp(52 + max(0,zm[i])*8 + max(0,zv[i])*6 - max(0,-zm[i])*3,10,95)

        pre=(
            momentum*0.50 +
            clamp(50+quality*7,0,100)*0.20 +
            fund*0.20 +
            clamp(50+smallcap*9+volatility*0.7,0,100)*0.10
        )

        out.append({
            "asset_type":"crypto",
            "id":x.get("id"),
            "symbol":str(x.get("symbol","")).upper(),
            "name":x.get("name",""),
            "price":sf(x.get("current_price")),
            "market_cap":mcap,
            "volume":vol,
            "liq_ratio":liq,
            "change_1h":ch1,
            "change_24h":ch24,
            "change_7d":ch7,
            "change_30d":ch30,
            "momentum_score":momentum,
            "fundamental_score":fund,
            "pre_score":pre,
        })
    out.sort(key=lambda x:x["pre_score"],reverse=True)
    return out

# ----------------------- STOCKS -----------------------

def finnhub_available():
    return bool(FINNHUB_API_KEY)

def finnhub_symbols():
    if not FINNHUB_API_KEY:
        return []
    try:
        return req_json(f"{FINNHUB}/stock/symbol",{"exchange":"US","token":FINNHUB_API_KEY})
    except:
        return []

def stock_quote(symbol):
    try:
        return req_json(f"{FINNHUB}/quote",{"symbol":symbol,"token":FINNHUB_API_KEY})
    except:
        return {}

def stock_metrics(symbol):
    try:
        return req_json(f"{FINNHUB}/stock/metric",{"symbol":symbol,"metric":"all","token":FINNHUB_API_KEY})
    except:
        return {}

def stock_news(symbol, days=30):
    if not FINNHUB_API_KEY:
        return []
    from datetime import datetime, timedelta
    to=datetime.utcnow().date()
    fr=to-timedelta(days=days)
    try:
        return req_json(
            f"{FINNHUB}/company-news",
            {"symbol":symbol,"from":fr.isoformat(),"to":to.isoformat(),"token":FINNHUB_API_KEY}
        )[:20]
    except:
        return []

def prelim_stocks(horizon, risk, max_symbols=140):
    if not FINNHUB_API_KEY:
        return []

    syms=finnhub_symbols()
    # Keep common US stocks; skip OTC-like symbols and obvious warrants/units.
    filt=[]
    for s in syms:
        sym=s.get("symbol","")
        typ=(s.get("type") or "").lower()
        if not sym or len(sym)>6 or "." in sym or "/" in sym:
            continue
        if any(k in typ for k in ["warrant","unit","right"]):
            continue
        filt.append(s)
        if len(filt)>=max_symbols:
            break

    out=[]
    for s in filt:
        sym=s["symbol"]
        q=stock_quote(sym)
        price=sf(q.get("c"))
        prev=sf(q.get("pc"))
        if price<=0 or prev<=0:
            continue
        ch24=(price/prev-1)*100
        m=stock_metrics(sym).get("metric",{})
        beta=sf(m.get("beta"),1)
        pe=sf(m.get("peBasicExclExtraTTM"),0)
        rev_growth=sf(m.get("revenueGrowthTTMYoy"),0)
        eps_growth=sf(m.get("epsGrowthTTMYoy"),0)
        mcap=sf(m.get("marketCapitalization"),0)*1_000_000
        vol_proxy=abs(ch24)*(0.8+min(abs(beta),3)*0.2)

        momentum=clamp(50+ch24*2.0,0,100)
        fund=50
        if rev_growth: fund += clamp(rev_growth,-30,50)*0.45
        if eps_growth: fund += clamp(eps_growth,-30,50)*0.25
        if pe>0 and pe<35: fund+=5
        if pe>80: fund-=5
        fund=clamp(fund,5,95)

        smallcap=0
        if mcap and mcap<2_000_000_000: smallcap=10
        if mcap and mcap<500_000_000: smallcap=18

        pre=momentum*0.45 + fund*0.35 + clamp(50+vol_proxy+smallcap*RISKS[risk]["smallcap_bonus"],0,100)*0.20
        out.append({
            "asset_type":"stock","symbol":sym,"name":s.get("description") or sym,
            "price":price,"market_cap":mcap,"volume":0,"liq_ratio":0,
            "change_1h":0,"change_24h":ch24,"change_7d":0,"change_30d":0,
            "momentum_score":momentum,"fundamental_score":fund,"pre_score":pre,
        })
    out.sort(key=lambda x:x["pre_score"],reverse=True)
    return out

# ----------------------- DEEP SCORING -----------------------

def upside_band(asset, risk, horizon):
    if asset["asset_type"]=="crypto":
        mcap=asset.get("market_cap",0)
        if risk=="extreme":
            if mcap and mcap<100_000_000: return "Possible 2–10×, but loss of 70–100% is also plausible"
            if mcap and mcap<1_000_000_000: return "Possible +50% to 3× in a strong catalyst cycle"
            return "High volatility; 20–100% moves are possible"
        if risk=="aggressive": return "Higher-volatility upside; large drawdowns are possible"
        if risk=="moderate": return "Balanced upside/risk"
        return "Lower relative risk within crypto; still volatile"
    else:
        if risk=="extreme": return "Small/high-beta stock moves can be extreme; 50%+ drawdowns are possible"
        if risk=="aggressive": return "High-beta upside with meaningful drawdown risk"
        if risk=="moderate": return "Balanced stock risk"
        return "Quality/liquidity weighted"

def deep_score(asset, horizon, risk):
    n=news_catalyst_metrics(asset["name"],asset["symbol"],horizon)
    penalty=priced_in_penalty(asset["change_24h"],asset["change_7d"],n["catalyst_score"])

    hw=HORIZONS[horizon]
    # Market/liquidity score.
    if asset["asset_type"]=="crypto":
        liq=asset.get("liq_ratio",0)
        liquidity=clamp(35 + math.log10(max(liq*10000,1))*18,5,95)
    else:
        liquidity=70 if asset.get("market_cap",0)>5_000_000_000 else 55

    raw=(
        asset["momentum_score"]*hw["momentum_w"] +
        n["news_score"]*hw["news_w"] +
        asset["fundamental_score"]*hw["fund_w"] +
        n["catalyst_score"]*hw["catalyst_w"] +
        liquidity*0.70
    )
    denom=hw["momentum_w"]+hw["news_w"]+hw["fund_w"]+hw["catalyst_w"]+0.70
    score=raw/denom - penalty

    # Risk-mode tailoring.
    mcap=asset.get("market_cap",0)
    if risk=="conservative":
        if asset["asset_type"]=="crypto" and mcap<2_000_000_000: score-=12
        if liquidity<50: score-=10
    elif risk=="aggressive":
        if asset["asset_type"]=="crypto" and mcap<1_000_000_000: score+=4
    elif risk=="extreme":
        if asset["asset_type"]=="crypto":
            if mcap and mcap<100_000_000: score+=12
            elif mcap and mcap<500_000_000: score+=8
        if abs(asset["change_24h"])>8: score+=3

    score=clamp(score)
    confidence=clamp(40 + liquidity*0.25 + min(n["mentions"],8)*3 + asset["fundamental_score"]*0.18,20,92)

    catalyst_status="LOW"
    if n["catalyst_score"]>=75: catalyst_status="HIGH"
    elif n["catalyst_score"]>=60: catalyst_status="MEDIUM"

    return {
        **asset,
        "score":round(score,1),
        "confidence":round(confidence,1),
        "news_score":round(n["news_score"],1),
        "catalyst_score":round(n["catalyst_score"],1),
        "catalyst_status":catalyst_status,
        "priced_in_penalty":penalty,
        "headlines":n["headlines"],
        "catalyst_titles":n["catalyst_titles"],
        "upside_band":upside_band(asset,risk,horizon),
        "why_now":[
            f"Momentum {asset['momentum_score']:.0f}/100",
            f"Catalyst {n['catalyst_score']:.0f}/100",
            f"News {n['news_score']:.0f}/100",
            f"Fundamentals {asset['fundamental_score']:.0f}/100",
            f"Liquidity {liquidity:.0f}/100",
        ]
    }

def scan_market(asset_type,horizon,risk):
    candidates=[]

    if asset_type in ("all","crypto"):
        cryptos=prelim_crypto(get_crypto_universe(),horizon,risk)
        # Deep-news scan on best 40 to stay practical on free hosting.
        candidates.extend(cryptos[:40])

    if asset_type in ("all","stocks") and FINNHUB_API_KEY:
        stocks=prelim_stocks(horizon,risk)
        candidates.extend(stocks[:35])

    candidates.sort(key=lambda x:x["pre_score"],reverse=True)
    # Deep score only strongest 50 total.
    deep=[]
    for c in candidates[:50]:
        try:
            deep.append(deep_score(c,horizon,risk))
        except:
            pass

    deep.sort(key=lambda x:(x["score"],x["confidence"]),reverse=True)
    return {
        "updated":int(time.time()),
        "asset_type":asset_type,
        "horizon":horizon,
        "horizon_label":HORIZONS[horizon]["label"],
        "risk":risk,
        "risk_label":RISKS[risk]["label"],
        "stocks_enabled":finnhub_available(),
        "results":deep[:20],
        "coverage":{
            "crypto_universe":"Up to 500 CoinGecko-listed coins per scan",
            "stock_universe":"US stocks via Finnhub when FINNHUB_API_KEY is configured",
            "deep_analysis":"Top 50 preliminary candidates receive news/catalyst scoring"
        }
    }

def cache_key(asset_type,horizon,risk):
    return f"{asset_type}:{horizon}:{risk}"

def get_scan(asset_type,horizon,risk,force=False):
    key=cache_key(asset_type,horizon,risk)
    with cache_lock:
        d=cache.get(key)
        if d and not force and time.time()-d["updated"]<AUTO_SCAN_SECONDS:
            return d
    d=scan_market(asset_type,horizon,risk)
    with cache_lock:
        cache[key]=d
    return d

# ----------------------- API -----------------------

@app.route("/api/scan")
def api_scan():
    asset_type=request.args.get("asset","crypto")
    horizon=request.args.get("horizon","1w")
    risk=request.args.get("risk","aggressive")
    force=request.args.get("force","0")=="1"
    if asset_type not in ASSET_TYPES: asset_type="crypto"
    if horizon not in HORIZONS: horizon="1w"
    if risk not in RISKS: risk="aggressive"
    try:
        return jsonify(get_scan(asset_type,horizon,risk,force))
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/health")
def health():
    return {"ok":True,"stocks_enabled":finnhub_available()}

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name":"Market Opportunity Engine",
        "short_name":"Market Engine",
        "start_url":"/",
        "display":"standalone",
        "background_color":"#0b0d12",
        "theme_color":"#0b0d12",
        "icons":[]
    })

# ----------------------- UI -----------------------

PAGE=r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12">
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Market Opportunity Engine</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#090c11;color:#eef2f7;font-family:-apple-system,BlinkMacSystemFont,Inter,Arial,sans-serif}
.wrap{max-width:1100px;margin:auto;padding:calc(env(safe-area-inset-top) + 18px) 14px 50px}
h1{font-size:27px;margin:0 0 4px}.sub{font-size:13px;color:#8893a4;line-height:1.4}
.controls{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0}
select,button{width:100%;border:1px solid #273042;background:#141923;color:#eef2f7;border-radius:12px;padding:12px;font-size:14px}
button{grid-column:1/-1;background:#eef2f7;color:#101216;font-weight:800;border:none}
.meta{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 14px}.pill{background:#141923;border:1px solid #242c3a;border-radius:10px;padding:8px 10px;font-size:11px;color:#a7b0bf}
.card{background:#11161f;border:1px solid #222a39;border-radius:17px;padding:14px;margin:10px 0}
.topcard{border-width:2px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.rank{font-size:12px;color:#8691a2}.name{font-size:22px;font-weight:850}.sym{font-size:13px;color:#9aa5b6}
.score{font-size:22px;font-weight:850;text-align:right}.conf{font-size:11px;color:#8f99aa;text-align:right}
.price{font-size:18px;font-weight:700;margin:9px 0}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:10px 0}
.metric{background:#181e29;border-radius:10px;padding:8px;font-size:10px;color:#8f99aa}
.metric b{display:block;color:#eef2f7;font-size:13px;margin-top:3px}
.why{font-size:12px;line-height:1.5;color:#c4cbd5}.upside{background:#0d1117;border-radius:10px;padding:9px;font-size:12px;line-height:1.45;margin-top:9px}
.headlines{font-size:11px;color:#97a1b0;line-height:1.45;margin-top:8px}
.headlines div{margin-top:4px}
.note{font-size:11px;color:#778294;line-height:1.45;margin-top:16px}
.warn{background:#261b1c;border:1px solid #493033;padding:10px;border-radius:12px;margin-bottom:12px;font-size:12px}
.empty{text-align:center;padding:35px;color:#8f99aa}
@media(max-width:560px){
 .controls{grid-template-columns:1fr}
 button{grid-column:auto}
 .grid{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body><div class="wrap">
<h1>Market Opportunity Engine</h1>
<div class="sub">Top 20 opportunities ranked by market action, fundamentals, news, catalysts and your chosen time horizon/risk.</div>

<div class="controls">
<select id="asset">
<option value="all">Stocks + Crypto</option>
<option value="crypto" selected>Crypto</option>
<option value="stocks">Stocks</option>
</select>
<select id="horizon">
<option value="24h">Next 24 Hours</option>
<option value="3d">Next 3 Days</option>
<option value="1w" selected>Next Week</option>
<option value="1m">Next Month</option>
<option value="3m">Next 3 Months</option>
<option value="1y">1 Year+</option>
</select>
<select id="risk">
<option value="conservative">Conservative</option>
<option value="moderate">Moderate</option>
<option value="aggressive" selected>Aggressive</option>
<option value="extreme">Extreme / Moonshot</option>
</select>
<button id="scan">SCAN / REFRESH</button>
</div>

<div class="meta">
<div class="pill" id="updated">Updated —</div>
<div class="pill" id="coverage">Coverage —</div>
</div>

<div id="warning"></div>
<div id="results"><div class="empty">Choose your filters and scan.</div></div>

<div class="note">
Research tool only. Scores are heuristic rankings, not guarantees or true probabilities. “10× potential” means the asset's size/volatility makes that outcome conceivable under an unusually strong catalyst cycle; it can also lose most or all of its value. News feeds can miss events or misclassify headlines. Stock scanning requires a Finnhub API key configured in Render.
</div>
</div>

<script>
const money=x=>{
 if(!x && x!==0)return '—';
 if(x>=1e9)return '$'+(x/1e9).toFixed(2)+'B';
 if(x>=1e6)return '$'+(x/1e6).toFixed(2)+'M';
 if(x>=1000)return '$'+x.toLocaleString(undefined,{maximumFractionDigits:2});
 if(x>=1)return '$'+x.toFixed(4);
 if(x>=.01)return '$'+x.toFixed(5);
 return '$'+x.toFixed(8);
}
const pct=x=>(x>=0?'+':'')+(x||0).toFixed(2)+'%';

async function scan(force=false){
 const asset=document.getElementById('asset').value;
 const horizon=document.getElementById('horizon').value;
 const risk=document.getElementById('risk').value;
 const btn=document.getElementById('scan');
 btn.disabled=true;btn.textContent='SCANNING MARKET…';
 document.getElementById('results').innerHTML='<div class="empty">Analyzing market + catalysts…</div>';
 try{
   const r=await fetch(`/api/scan?asset=${asset}&horizon=${horizon}&risk=${risk}&force=${force?1:0}`);
   const text=await r.text();
   let d;
   try{d=JSON.parse(text)}catch(_){throw new Error('Server returned an invalid response')}
   if(!r.ok || d.error)throw new Error(d.error||('HTTP '+r.status));

   document.getElementById('updated').textContent='Updated '+new Date(d.updated*1000).toLocaleTimeString();
   document.getElementById('coverage').textContent=d.coverage.crypto_universe;

   if((asset==='stocks'||asset==='all') && !d.stocks_enabled){
     document.getElementById('warning').innerHTML='<div class="warn">Stock data is not enabled yet. Add a FINNHUB_API_KEY environment variable in Render to activate US-stock scanning. Crypto scanning still works.</div>';
   }else{
     document.getElementById('warning').innerHTML='';
   }

   if(!d.results.length){
     document.getElementById('results').innerHTML='<div class="empty">No results returned for this scan.</div>';
   }else{
     document.getElementById('results').innerHTML=d.results.map((c,i)=>`
       <div class="card ${i===0?'topcard':''}">
         <div class="row">
           <div>
             <div class="rank">#${i+1} · ${c.asset_type.toUpperCase()}</div>
             <div class="name">${c.name}</div>
             <div class="sym">${c.symbol}</div>
           </div>
           <div>
             <div class="score">${c.score}/100</div>
             <div class="conf">confidence ${c.confidence}</div>
           </div>
         </div>

         <div class="price">${money(c.price)} · ${pct(c.change_24h)} 24h</div>

         <div class="grid">
           <div class="metric">Momentum<b>${c.momentum_score.toFixed(0)}</b></div>
           <div class="metric">Catalyst<b>${c.catalyst_score}</b></div>
           <div class="metric">News<b>${c.news_score}</b></div>
           <div class="metric">Fundamentals<b>${c.fundamental_score.toFixed(0)}</b></div>
           <div class="metric">Market cap<b>${money(c.market_cap)}</b></div>
           <div class="metric">Volume<b>${money(c.volume)}</b></div>
           <div class="metric">7d move<b>${pct(c.change_7d)}</b></div>
           <div class="metric">Priced-in penalty<b>${c.priced_in_penalty}</b></div>
         </div>

         <div class="upside"><b>${d.risk_label} outlook:</b> ${c.upside_band}</div>
         <div class="why"><b>Why now:</b> ${c.why_now.join(' · ')}</div>

         ${c.catalyst_titles && c.catalyst_titles.length ? `
         <div class="headlines"><b>Potential catalysts:</b>
         ${c.catalyst_titles.map(h=>`<div>• ${h}</div>`).join('')}
         </div>`:''}
       </div>
     `).join('');
   }
 }catch(e){
   document.getElementById('results').innerHTML='<div class="warn">Scan failed: '+e.message+'</div>';
 }
 btn.disabled=false;btn.textContent='SCAN / REFRESH';
}
document.getElementById('scan').onclick=()=>scan(true);
document.getElementById('asset').onchange=()=>scan(false);
document.getElementById('horizon').onchange=()=>scan(false);
document.getElementById('risk').onchange=()=>scan(false);
scan(false);
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(PAGE)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=5000)
