from flask import Flask, jsonify, render_template_string, request
import requests
import math
import time
import os
import re
import html
import statistics
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

app = Flask(__name__)

# ============================================================
# CRYPTO RADAR PRO v13 — DAY-TRADING RADAR + FOCUS MODE
# ============================================================
#
# Goals:
# - Broad crypto discovery
# - Staged analysis so free hosting does not die from API calls
# - Better separation of:
#     opportunity / risk / data quality / catalyst strength
# - Avoid pretending a score is a true probability
# - Avoid small-cap = automatically bullish
# - Avoid stablecoins / wrapped duplicates dominating rankings
# - Use liquidity, volatility, momentum, market regime, news,
#   technical structure, and catalyst language together
#
# Research only. No order execution.
# ============================================================

# ---------------------- PROVIDERS ----------------------

COINGECKO = "https://api.coingecko.com/api/v3"
COINPAPRIKA = "https://api.coinpaprika.com/v1"
BINANCE_US = "https://api.binance.us"
ALTERNATIVE_FNG = "https://api.alternative.me/fng/"
FINNHUB = "https://finnhub.io/api/v1"

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

def env_int(name, default, lo=None, hi=None):
    """Read integer env vars without allowing a bad Render value to crash startup."""
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except Exception:
        value = int(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value

CACHE_SECONDS = env_int("CACHE_SECONDS", 75, 15, 900)
MAX_CRYPTO_UNIVERSE = env_int("MAX_CRYPTO_UNIVERSE", 1000, 100, 2000)
PRELIM_LIMIT = env_int("PRELIM_LIMIT", 80, 20, 200)
TECH_LIMIT = env_int("TECH_LIMIT", 36, 10, 80)
NEWS_LIMIT = env_int("NEWS_LIMIT", 24, 5, 60)

session = requests.Session()
session.headers.update({
    "User-Agent": "CryptoRadarPro/4.0 research-only",
    "Accept": "application/json,text/plain,*/*"
})

cache = {}
cache_lock = threading.Lock()

news_cache = {}
news_cache_lock = threading.Lock()
NEWS_CACHE_SECONDS = env_int("NEWS_CACHE_SECONDS", 420, 60, 3600)
scan_compute_lock = threading.Lock()

# ---------------------- CONFIG ----------------------

HORIZONS = {
    "1h":{"label":"Next 1 Hour · Scalping","momentum":1.85,"technical":1.80,"liquidity":1.45,"news":0.55,"catalyst":0.35,"fundamental":0.10,"regime":1.15},
    "4h":{"label":"Next 4 Hours · Day Trade","momentum":1.75,"technical":1.70,"liquidity":1.30,"news":0.75,"catalyst":0.50,"fundamental":0.15,"regime":1.10},
    "12h":{"label":"Next 12 Hours","momentum":1.60,"technical":1.55,"liquidity":1.15,"news":1.00,"catalyst":0.65,"fundamental":0.20,"regime":1.05},
    "24h":{"label":"Next 24 Hours","momentum":1.45,"technical":1.35,"liquidity":1.00,"news":1.20,"catalyst":0.85,"fundamental":0.30,"regime":1.00},
    "3d":{"label":"Next 3 Days","momentum":1.35,"technical":1.25,"liquidity":0.95,"news":1.30,"catalyst":1.05,"fundamental":0.40,"regime":0.95},
    "1w":{"label":"Next Week","momentum":1.15,"technical":1.10,"liquidity":0.90,"news":1.15,"catalyst":1.25,"fundamental":0.55,"regime":0.85},
    "1m":{"label":"Next Month","momentum":0.90,"technical":0.85,"liquidity":0.80,"news":1.00,"catalyst":1.45,"fundamental":0.80,"regime":0.70},
    "3m":{"label":"Next 3 Months","momentum":0.65,"technical":0.65,"liquidity":0.70,"news":0.75,"catalyst":1.30,"fundamental":1.20,"regime":0.55},
    "1y":{"label":"1 Year+","momentum":0.30,"technical":0.40,"liquidity":0.55,"news":0.45,"catalyst":0.80,"fundamental":1.65,"regime":0.45},
}

RISK_MODES = {
    "conservative": {
        "label": "Conservative",
        "min_mcap": 2_000_000_000,
        "min_volume": 25_000_000,
        "min_liq_ratio": 0.004,
        "smallcap_bonus": 0,
        "volatility_preference": 0.20,
        "quality_weight": 1.35,
        "risk_penalty": 1.40,
    },
    "moderate": {
        "label": "Moderate",
        "min_mcap": 250_000_000,
        "min_volume": 8_000_000,
        "min_liq_ratio": 0.006,
        "smallcap_bonus": 0.10,
        "volatility_preference": 0.35,
        "quality_weight": 1.10,
        "risk_penalty": 1.00,
    },
    "aggressive": {
        "label": "Aggressive",
        "min_mcap": 30_000_000,
        "min_volume": 2_000_000,
        "min_liq_ratio": 0.008,
        "smallcap_bonus": 0.35,
        "volatility_preference": 0.70,
        "quality_weight": 0.85,
        "risk_penalty": 0.70,
    },
    "extreme": {
        "label": "Extreme / Moonshot",
        "min_mcap": 2_000_000,
        "min_volume": 250_000,
        "min_liq_ratio": 0.010,
        "smallcap_bonus": 0.70,
        "volatility_preference": 1.00,
        "quality_weight": 0.60,
        "risk_penalty": 0.45,
    },
}

STABLE_SYMBOLS = {
    "USDT","USDC","DAI","FDUSD","TUSD","USDP","USDS","PYUSD","GUSD","FRAX",
    "LUSD","EURC","USDE","SUSD","USD0","USDD","EURS"
}

WRAPPED_HINTS = (
    "wrapped ", "staked ", "bridged ", "wormhole", "binance-peg",
    "lido staked", "wrapped bitcoin", "wrapped ether"
)

POSITIVE_WORDS = {
    "approval","approved","partnership","partners","integration","integrates",
    "launch","launches","released","release","upgrade","upgrades","adoption",
    "adopts","contract","award","wins","growth","surge","breakthrough",
    "successful","success","phase 3","phase iii","fda","etf","listing",
    "listed","mainnet","testnet","burn","buyback","acquisition","acquires",
    "merger","collaboration","roadmap","milestone","expansion","expands",
    "institutional","funding","investment","grant","government contract"
}

NEGATIVE_WORDS = {
    "hack","hacked","exploit","breach","lawsuit","investigation","probe",
    "fraud","scam","outage","delay","delayed","delist","delisting",
    "bankruptcy","default","rejected","rejection","ban","banned","charges",
    "liquidation","selloff","plunge","crash","downgrade","dilution",
    "offering","unlock","token unlock","rug","shutdown","halt"
}

FUTURE_CATALYST_WORDS = {
    "upcoming","soon","next","will","plans","planned","scheduled","expected",
    "expects","launch","release","upgrade","roadmap","conference","vote",
    "decision","trial","earnings","mainnet","listing","burn","airdrop",
    "deadline","approval","migration","hard fork","testnet","partnership"
}

# ---------------------- HELPERS ----------------------

def sf(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

def safe_div(a, b, default=0.0):
    return a / b if b else default

def pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0

def mean(vals, default=0.0):
    vals = [sf(v) for v in vals if v is not None]
    return statistics.mean(vals) if vals else default

def req_json(url, params=None, timeout=18, retries=3, extra_headers=None):
    headers = dict(session.headers)
    if extra_headers:
        headers.update(extra_headers)

    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout, headers=headers)
            if r.status_code == 429:
                last_err = RuntimeError(f"HTTP 429 rate limit from {url}")
                time.sleep(1.25 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
    raise last_err

def log_scale(value, floor=1):
    return math.log10(max(sf(value), floor))

def robust_rank_score(values):
    """Return percentile-style 0..100 scores, robust to outliers."""
    if not values:
        return []
    indexed = sorted((sf(v), i) for i, v in enumerate(values))
    out = [50.0] * len(values)
    n = max(len(indexed) - 1, 1)
    for rank, (_, idx) in enumerate(indexed):
        out[idx] = 100.0 * rank / n
    return out

def parse_rss(url, limit=15):
    try:
        r = session.get(url, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub = item.findtext("pubDate") or ""
            title = html.unescape(re.sub(r"<[^>]+>", " ", title)).strip()
            if title:
                items.append({"title": title, "link": link, "published": pub})
        return items
    except Exception:
        return []

def google_news(query, days, limit=10):
    q = quote_plus(f'{query} when:{days}d')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return parse_rss(url, limit)


coinbase_cache={"ts":0.0,"symbols":{},"ok":False}
coinbase_lock=threading.Lock()
source_feed_cache={"ts":0.0,"items":[]}
source_feed_lock=threading.Lock()

def parse_any_feed(url,limit=20):
    try:
        r=session.get(url,timeout=9);r.raise_for_status();root=ET.fromstring(r.content);out=[]
        for node in root.iter():
            if node.tag.split("}")[-1].lower() not in ("item","entry"): continue
            title="";link="";published=""
            for child in list(node):
                t=child.tag.split("}")[-1].lower()
                if t=="title" and child.text:title=child.text
                elif t=="link":link=child.attrib.get("href") or (child.text or "")
                elif t in ("pubdate","published","updated") and child.text:published=child.text
            title=html.unescape(re.sub(r"<[^>]+>"," ",title or "")).strip()
            if title: out.append({"title":title,"link":link,"published":published})
            if len(out)>=limit:break
        return out
    except Exception:return []

def get_coinbase_markets():
    now=time.time()
    with coinbase_lock:
        if now-coinbase_cache["ts"]<600 and coinbase_cache["symbols"]:return coinbase_cache["symbols"],coinbase_cache["ok"]
    symbols={};ok=False
    try:
        r=session.get("https://api.exchange.coinbase.com/products",timeout=10);r.raise_for_status();data=r.json()
        if isinstance(data,list):
            for p in data:
                b=str(p.get("base_currency") or "").upper();q=str(p.get("quote_currency") or "").upper();status=str(p.get("status") or "online").lower()
                if b and q in ("USD","USDC","USDT") and status=="online" and not bool(p.get("trading_disabled",False)):
                    symbols.setdefault(b,[]).append(str(p.get("id") or f"{b}-{q}"))
            ok=True
    except Exception:pass
    with coinbase_lock:coinbase_cache.update({"ts":now,"symbols":symbols,"ok":ok})
    return symbols,ok

def crypto_industry_feed():
    now=time.time()
    with source_feed_lock:
        if now-source_feed_cache["ts"]<300 and source_feed_cache["items"]:return list(source_feed_cache["items"])
    items=[]
    for source,url in [("CoinDesk","https://www.coindesk.com/arc/outboundfeeds/rss/"),("Cointelegraph","https://cointelegraph.com/rss"),("Decrypt","https://decrypt.co/feed")]:
        for x in parse_any_feed(url,20):x=dict(x);x["source"]=source;items.append(x)
    with source_feed_lock:source_feed_cache.update({"ts":now,"items":items})
    return items

def relevant_industry_news(asset,limit=6):
    name=str(asset.get("name") or "").lower();symbol=str(asset.get("symbol") or "").lower();out=[]
    for x in crypto_industry_feed():
        t=x["title"].lower();hit=(name and name in t) or (len(symbol)>=3 and re.search(rf"(?<![a-z0-9]){re.escape(symbol)}(?![a-z0-9])",t))
        if hit:out.append(x)
        if len(out)>=limit:break
    return out

def reddit_context(asset,limit=7):
    name=str(asset.get("name") or "");symbol=str(asset.get("symbol") or "").upper();q=quote_plus(f'"{name}" {symbol} crypto')
    posts=parse_any_feed(f"https://www.reddit.com/search.rss?q={q}&sort=new&t=day",limit);pos=neg=0
    for p in posts:
        ss,_=headline_signal(p["title"])
        if ss>0:pos+=ss
        elif ss<0:neg+=abs(ss)
    if not posts:return {"available":False,"score":50.0,"mentions":0,"sentiment":"unavailable","posts":[]}
    score=clamp(50+pos*4-neg*5+min(len(posts),8)*1.2);sentiment="bullish" if score>=58 else ("bearish" if score<=42 else "mixed")
    return {"available":True,"score":round(score,1),"mentions":len(posts),"sentiment":sentiment,"posts":posts[:5]}


def headline_signal(title):
    t = title.lower()
    pos = sum(1 for w in POSITIVE_WORDS if w in t)
    neg = sum(1 for w in NEGATIVE_WORDS if w in t)
    future = sum(1 for w in FUTURE_CATALYST_WORDS if w in t)
    return pos - neg, future

# ---------------------- BROAD MARKET DATA ----------------------

def get_coingecko_universe():
    rows = []
    cg_headers = {}
    if COINGECKO_API_KEY:
        cg_headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    pages = max(1, math.ceil(MAX_CRYPTO_UNIVERSE / 250))
    for page in range(1, pages + 1):
        j = req_json(
            f"{COINGECKO}/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 250,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d,30d",
            },
            extra_headers=cg_headers,
            retries=3,
        )
        if not isinstance(j, list):
            raise RuntimeError("CoinGecko returned unexpected data")
        rows.extend(j)
        if len(rows) >= MAX_CRYPTO_UNIVERSE:
            break
    return rows[:MAX_CRYPTO_UNIVERSE]

def get_coinpaprika_universe():
    j = req_json(f"{COINPAPRIKA}/tickers", timeout=25, retries=3)
    if not isinstance(j, list):
        raise RuntimeError("CoinPaprika returned unexpected data")

    rows = []
    for x in j:
        q = (x.get("quotes") or {}).get("USD") or {}
        rows.append({
            "id": x.get("id"),
            "symbol": x.get("symbol", ""),
            "name": x.get("name", ""),
            "current_price": sf(q.get("price")),
            "market_cap": sf(q.get("market_cap")),
            "total_volume": sf(q.get("volume_24h")),
            "price_change_percentage_1h_in_currency": sf(q.get("percent_change_1h")),
            "price_change_percentage_24h_in_currency": sf(q.get("percent_change_24h")),
            "price_change_percentage_7d_in_currency": sf(q.get("percent_change_7d")),
            "price_change_percentage_30d_in_currency": sf(q.get("percent_change_30d")),
        })
    rows.sort(key=lambda z: sf(z.get("market_cap")), reverse=True)
    return rows[:MAX_CRYPTO_UNIVERSE]

def get_crypto_universe():
    try:
        rows = get_coingecko_universe()
        if rows:
            return rows, "CoinGecko", None
    except Exception as e:
        cg_err = str(e)
    else:
        cg_err = "no rows"

    try:
        rows = get_coinpaprika_universe()
        if rows:
            return rows, "CoinPaprika", f"CoinGecko unavailable: {cg_err}"
    except Exception as e:
        return [], "Unavailable", f"CoinGecko failed ({cg_err}); CoinPaprika failed ({e})"

    return [], "Unavailable", f"CoinGecko failed ({cg_err}); fallback returned no rows"

# ---------------------- MARKET REGIME ----------------------

def fear_greed():
    try:
        d = req_json(ALTERNATIVE_FNG, timeout=10, retries=2)["data"][0]
        return int(d["value"]), d["value_classification"]
    except Exception:
        return 50, "Unavailable"

def market_regime(rows):
    if not rows:
        return {"score":50,"label":"UNKNOWN","breadth":50}

    top = [x for x in rows[:100] if sf(x.get("market_cap")) > 0]
    if not top:
        return {"score":50,"label":"UNKNOWN","breadth":50}

    positive_24 = sum(1 for x in top if sf(x.get("price_change_percentage_24h_in_currency")) > 0)
    positive_7 = sum(1 for x in top if sf(x.get("price_change_percentage_7d_in_currency")) > 0)
    breadth = (positive_24 / len(top) * 50) + (positive_7 / len(top) * 50)

    btc = next((x for x in rows if str(x.get("symbol","")).lower() == "btc"), None)
    btc24 = sf(btc.get("price_change_percentage_24h_in_currency")) if btc else 0
    btc7 = sf(btc.get("price_change_percentage_7d_in_currency")) if btc else 0

    score = clamp(50 + (breadth - 50) * 0.65 + btc24 * 1.4 + btc7 * 0.45)
    if score >= 63:
        label = "RISK-ON"
    elif score <= 37:
        label = "RISK-OFF"
    else:
        label = "MIXED"

    return {"score":round(score,1),"label":label,"breadth":round(breadth,1)}

# ---------------------- FILTERS / PRELIMINARY SCORE ----------------------

def is_bad_duplicate_or_stable(name, symbol):
    n = (name or "").lower().strip()
    s = (symbol or "").upper().strip()

    if s in STABLE_SYMBOLS:
        return True

    if any(h in n for h in WRAPPED_HINTS):
        return True

    # Skip obvious LP tokens / receipt tokens / stable naming.
    if any(k in n for k in ("liquidity pool", "lp token", "usd stablecoin", "stablecoin")):
        return True

    return False

def horizon_momentum(asset, horizon):
    ch1 = asset["change_1h"]
    ch24 = asset["change_24h"]
    ch7 = asset["change_7d"]
    ch30 = asset["change_30d"]

    if horizon == "24h":
        return ch1 * 0.45 + ch24 * 0.55
    if horizon == "3d":
        return ch24 * 0.60 + ch7 * 0.40
    if horizon == "1w":
        return ch24 * 0.20 + ch7 * 0.80
    if horizon == "1m":
        return ch7 * 0.35 + ch30 * 0.65
    if horizon == "3m":
        return ch30 * 0.85 + ch7 * 0.15
    return ch30 * 0.35 + ch7 * 0.15

def preprocess_crypto(rows, horizon, risk):
    cfg = RISK_MODES[risk]
    clean = []

    for x in rows:
        symbol = str(x.get("symbol","")).upper()
        name = str(x.get("name",""))

        if is_bad_duplicate_or_stable(name, symbol):
            continue

        mcap = sf(x.get("market_cap"))
        vol = sf(x.get("total_volume"))
        price = sf(x.get("current_price"))

        if price <= 0 or mcap <= 0 or vol <= 0:
            continue
        if mcap < cfg["min_mcap"]:
            continue
        if vol < cfg["min_volume"]:
            continue

        liq_ratio = vol / mcap
        if liq_ratio < cfg["min_liq_ratio"]:
            continue

        clean.append({
            "asset_type":"crypto",
            "id":x.get("id"),
            "symbol":symbol,
            "name":name,
            "price":price,
            "market_cap":mcap,
            "volume":vol,
            "liq_ratio":liq_ratio,
            "change_1h":sf(x.get("price_change_percentage_1h_in_currency")),
            "change_24h":sf(x.get("price_change_percentage_24h_in_currency")),
            "change_7d":sf(x.get("price_change_percentage_7d_in_currency")),
            "change_30d":sf(x.get("price_change_percentage_30d_in_currency")),
        })

    if not clean:
        return []

    mcap_scores = robust_rank_score([log_scale(a["market_cap"]) for a in clean])
    volume_scores = robust_rank_score([log_scale(a["volume"]) for a in clean])
    liq_scores = robust_rank_score([a["liq_ratio"] for a in clean])

    for i, a in enumerate(clean):
        momentum_raw = horizon_momentum(a, horizon)
        momentum_score = clamp(50 + momentum_raw * 1.75)

        # Healthy liquidity is good. Extreme turnover can be pump-like.
        turnover = a["liq_ratio"]
        liquidity_score = liq_scores[i]
        if turnover > 2.0:
            liquidity_score -= 12
        elif turnover > 1.0:
            liquidity_score -= 6
        liquidity_score = clamp(liquidity_score)

        # "Fundamental" proxy for crypto. Not true fundamentals:
        # persistence/size/liquidity quality without blindly rewarding only mega-caps.
        quality = (
            mcap_scores[i] * 0.45 +
            volume_scores[i] * 0.30 +
            liquidity_score * 0.25
        )

        # Risk score: high = dangerous.
        recent_volatility = (
            abs(a["change_1h"]) * 2.0 +
            abs(a["change_24h"]) * 0.75 +
            abs(a["change_7d"]) * 0.22
        )
        concentration_risk = max(0, 60 - mcap_scores[i]) * 0.35
        risk_score = clamp(
            20 +
            recent_volatility * 1.25 +
            concentration_risk +
            max(0, 35 - liquidity_score) * 0.8
        )

        # Small cap upside is only a modest bonus. It never overrides weak liquidity.
        smallcap_upside = (100 - mcap_scores[i]) * cfg["smallcap_bonus"]
        volatility_fit = min(recent_volatility, 35) * cfg["volatility_preference"]

        prelim = (
            momentum_score * 0.42 +
            liquidity_score * 0.23 +
            quality * 0.23 +
            clamp(50 + smallcap_upside * 0.35 + volatility_fit, 0, 100) * 0.12
            - risk_score * 0.08 * cfg["risk_penalty"]
        )

        a.update({
            "momentum_score":round(momentum_score,1),
            "liquidity_score":round(liquidity_score,1),
            "fundamental_score":round(quality,1),
            "risk_score":round(risk_score,1),
            "pre_score":round(clamp(prelim),1),
            "mcap_percentile":round(mcap_scores[i],1),
            "volume_percentile":round(volume_scores[i],1),
        })

    clean.sort(key=lambda z:z["pre_score"], reverse=True)
    return clean


# ---------------------- ADVANCED MULTI-TIMEFRAME PRICE ACTION ----------------------

def true_range_series(highs, lows, closes):
    out=[]
    for i in range(len(closes)):
        if i == 0:
            out.append(highs[i]-lows[i])
        else:
            out.append(max(
                highs[i]-lows[i],
                abs(highs[i]-closes[i-1]),
                abs(lows[i]-closes[i-1])
            ))
    return out

def atr_value(highs,lows,closes,period=14):
    tr=true_range_series(highs,lows,closes)
    return mean(tr[-period:]) if tr else 0

def rolling_vwap(highs,lows,closes,quote_volumes,period=30):
    if not closes:
        return 0
    start=max(0,len(closes)-period)
    num=0.0
    den=0.0
    for i in range(start,len(closes)):
        typical=(highs[i]+lows[i]+closes[i])/3
        qv=quote_volumes[i]
        num += typical*qv
        den += qv
    return safe_div(num,den,closes[-1])

def swing_points(highs,lows,left=3,right=3):
    swing_highs=[]
    swing_lows=[]
    n=len(highs)
    for i in range(left,n-right):
        h=highs[i]
        l=lows[i]
        if all(h>highs[j] for j in range(i-left,i)) and all(h>=highs[j] for j in range(i+1,i+right+1)):
            swing_highs.append((i,h))
        if all(l<lows[j] for j in range(i-left,i)) and all(l<=lows[j] for j in range(i+1,i+right+1)):
            swing_lows.append((i,l))
    return swing_highs,swing_lows

def market_structure_score(highs,lows,closes):
    sh,sl=swing_points(highs,lows,3,3)
    score=50
    label="mixed"
    if len(sh)>=2 and len(sl)>=2:
        hh=sh[-1][1]>sh[-2][1]
        hl=sl[-1][1]>sl[-2][1]
        lh=sh[-1][1]<sh[-2][1]
        ll=sl[-1][1]<sl[-2][1]
        if hh and hl:
            score=84; label="higher highs / higher lows"
        elif lh and ll:
            score=18; label="lower highs / lower lows"
        elif hh or hl:
            score=64; label="improving structure"
        elif lh or ll:
            score=36; label="weakening structure"
    return score,label,sh,sl

def support_resistance_zones(highs,lows,closes,atr):
    sh,sl=swing_points(highs,lows,3,3)
    width=max(atr*0.45,closes[-1]*0.0015)
    points=[("R",p) for _,p in sh[-16:]]+[("S",p) for _,p in sl[-16:]]
    zones=[]
    for kind,p in sorted(points,key=lambda z:z[1]):
        match=None
        for z in zones:
            if abs(z["center"]-p)<=width:
                match=z; break
        if match:
            match["prices"].append(p)
            match["touches"]+=1
            match["center"]=mean(match["prices"])
            match["types"].add(kind)
        else:
            zones.append({"center":p,"prices":[p],"touches":1,"types":{kind}})
    for z in zones:
        z["low"]=z["center"]-width
        z["high"]=z["center"]+width
        z["type"]="support" if "S" in z["types"] and "R" not in z["types"] else "resistance" if "R" in z["types"] and "S" not in z["types"] else "pivot"
    zones.sort(key=lambda z:(z["touches"],-abs(z["center"]-closes[-1])),reverse=True)
    return zones[:10]

def nearest_zones(price,zones):
    below=[z for z in zones if z["center"]<price]
    above=[z for z in zones if z["center"]>price]
    support=max(below,key=lambda z:z["center"]) if below else None
    resistance=min(above,key=lambda z:z["center"]) if above else None
    return support,resistance

def candle_rejection_score(opens,highs,lows,closes,atr):
    if len(closes)<3 or atr<=0:
        return 50,"none"
    score=0
    label="none"
    for i in range(-3,0):
        body=abs(closes[i]-opens[i])
        upper=highs[i]-max(opens[i],closes[i])
        lower=min(opens[i],closes[i])-lows[i]
        base=max(body,atr*0.08)
        if lower>base*2.2 and lower>upper*1.5:
            score+=18; label="bullish lower-wick rejection"
        if upper>base*2.2 and upper>lower*1.5:
            score-=18; label="bearish upper-wick rejection"
    return clamp(50+score),label

def liquidity_sweep_signal(highs,lows,closes,atr):
    if len(closes)<30 or atr<=0:
        return 50,"none"
    prior_high=max(highs[-25:-2])
    prior_low=min(lows[-25:-2])
    h,l,c=highs[-1],lows[-1],closes[-1]
    if h>prior_high+atr*0.05 and c<prior_high:
        return 34,"bearish liquidity sweep above recent highs"
    if l<prior_low-atr*0.05 and c>prior_low:
        return 74,"bullish liquidity sweep below recent lows"
    return 50,"none"

def breakout_retest_signal(highs,lows,closes,volumes,atr):
    if len(closes)<35 or atr<=0:
        return 50,"none"
    prior_high=max(highs[-35:-5])
    prior_low=min(lows[-35:-5])
    p=closes[-1]
    recent_vol=mean(volumes[-3:])
    base_vol=mean(volumes[-30:-5],1)
    vr=safe_div(recent_vol,base_vol,1)
    if p>prior_high and lows[-1]<=prior_high+atr*0.35:
        return clamp(70+(vr-1)*8,50,92),"bullish breakout / retest"
    if p<prior_low and highs[-1]>=prior_low-atr*0.35:
        return clamp(30-(vr-1)*6,8,50),"bearish breakdown / retest"
    if p>prior_high+atr*0.7:
        return 61,"breakout extended above resistance"
    return 50,"none"

def compression_score(highs,lows,closes):
    if len(closes)<45:
        return 50
    recent=max(highs[-12:])-min(lows[-12:])
    older=max(highs[-40:-12])-min(lows[-40:-12])
    ratio=safe_div(recent,older,1)
    if ratio<0.35:return 84
    if ratio<0.55:return 70
    if ratio>1.30:return 36
    return 50

def location_quality(price,support,resistance):
    score=55
    label="neutral location"
    if support:
        ds=(price-support["center"])/price*100
        if 0<=ds<=1.5:
            score+=14; label="near support"
        elif ds>8:
            score-=6
    if resistance:
        dr=(resistance["center"]-price)/price*100
        if 0<=dr<=1.0:
            score-=16; label="directly below resistance"
        elif 2<=dr<=8:
            score+=7
    return clamp(score),label

def aggregate_10m(raw5m):
    """Binance has no native 10m candles, so combine pairs of 5m candles."""
    out=[]
    for i in range(0,len(raw5m)-1,2):
        a,b=raw5m[i],raw5m[i+1]
        out.append([
            a[0],
            a[1],
            max(sf(a[2]),sf(b[2])),
            min(sf(a[3]),sf(b[3])),
            b[4],
            sf(a[5])+sf(b[5]),
            b[6],
            sf(a[7])+sf(b[7]),
            int(sf(a[8]))+int(sf(b[8])),
            sf(a[9])+sf(b[9]),
            sf(a[10])+sf(b[10]),
            "0"
        ])
    return out

def analyze_timeframe(raw):
    opens=[sf(r[1]) for r in raw]
    highs=[sf(r[2]) for r in raw]
    lows=[sf(r[3]) for r in raw]
    closes=[sf(r[4]) for r in raw]
    base_vol=[sf(r[5]) for r in raw]
    quote_vol=[sf(r[7]) for r in raw]
    if len(closes)<45:
        raise RuntimeError("insufficient candles")

    p=closes[-1]
    atr=atr_value(highs,lows,closes,14)
    atr_pct=safe_div(atr,p,0)*100
    e20=ema(closes,20)[-1]
    e50=ema(closes,50)[-1]
    e200=ema(closes,200)[-1] if len(closes)>=200 else None
    rr=rsi(closes,14)
    vw=rolling_vwap(highs,lows,closes,quote_vol,30)

    structure_score,structure_label,_,_=market_structure_score(highs,lows,closes)
    zones=support_resistance_zones(highs,lows,closes,atr)
    support,resistance=nearest_zones(p,zones)
    rejection_score,rejection_label=candle_rejection_score(opens,highs,lows,closes,atr)
    sweep_score,sweep_label=liquidity_sweep_signal(highs,lows,closes,atr)
    breakout_score,breakout_label=breakout_retest_signal(highs,lows,closes,base_vol,atr)
    squeeze=compression_score(highs,lows,closes)
    location_score,location_label=location_quality(p,support,resistance)

    trend_score=50
    trend_label="mixed"
    if p>e20>e50:
        trend_score=76; trend_label="bullish"
        if e200 and e50>e200:
            trend_score=85; trend_label="strong bullish"
    elif p<e20<e50:
        trend_score=24; trend_label="bearish"
        if e200 and e50<e200:
            trend_score=15; trend_label="strong bearish"
    elif p>e20:
        trend_score=62; trend_label="mild bullish"

    vwap_score=66 if p>vw else 38
    rsi_score=72 if 52<=rr<=68 else 25 if rr>=80 else 45 if rr<=28 else 50

    composite=clamp(
        trend_score*0.22 +
        structure_score*0.22 +
        breakout_score*0.14 +
        sweep_score*0.10 +
        rejection_score*0.08 +
        location_score*0.10 +
        vwap_score*0.08 +
        rsi_score*0.06
    )

    profile=volume_profile_zones(highs,lows,closes,quote_vol,24)
    rsi_vals=rsi_series(closes,14)
    divergence_score,divergence_label=divergence_signal(closes,rsi_vals)

    return {
        "price":p,
        "atr_pct":atr_pct,
        "rsi":rr,
        "trend_score":trend_score,
        "trend_label":trend_label,
        "structure_score":structure_score,
        "structure_label":structure_label,
        "breakout_score":breakout_score,
        "breakout_label":breakout_label,
        "sweep_score":sweep_score,
        "sweep_label":sweep_label,
        "rejection_score":rejection_score,
        "rejection_label":rejection_label,
        "squeeze_score":squeeze,
        "location_score":location_score,
        "location_label":location_label,
        "support":support,
        "resistance":resistance,
        "score":composite,
        "volume_profile":profile,
        "divergence_score":divergence_score,
        "divergence_label":divergence_label
    }

def timeframe_weights_for_horizon(horizon):
    if horizon=="1h": return {"1m":0.18,"5m":0.28,"10m":0.18,"15m":0.18,"1h":0.12,"4h":0.05,"1d":0.01,"1w":0.00,"1M":0.00,"1y":0.00}
    if horizon=="4h": return {"1m":0.08,"5m":0.22,"10m":0.16,"15m":0.22,"1h":0.20,"4h":0.09,"1d":0.03,"1w":0.00,"1M":0.00,"1y":0.00}
    if horizon=="12h": return {"1m":0.04,"5m":0.14,"10m":0.12,"15m":0.18,"1h":0.25,"4h":0.18,"1d":0.07,"1w":0.02,"1M":0.00,"1y":0.00}
    if horizon=="24h": return {"1m":0.03,"5m":0.10,"10m":0.10,"15m":0.15,"1h":0.25,"4h":0.20,"1d":0.12,"1w":0.04,"1M":0.01,"1y":0.00}
    if horizon=="3d": return {"1m":0.01,"5m":0.05,"10m":0.06,"15m":0.10,"1h":0.24,"4h":0.24,"1d":0.18,"1w":0.08,"1M":0.03,"1y":0.01}
    if horizon=="1w": return {"1m":0.00,"5m":0.02,"10m":0.03,"15m":0.06,"1h":0.18,"4h":0.24,"1d":0.25,"1w":0.13,"1M":0.06,"1y":0.03}
    if horizon=="1m": return {"1m":0.00,"5m":0.01,"10m":0.01,"15m":0.02,"1h":0.08,"4h":0.16,"1d":0.30,"1w":0.23,"1M":0.13,"1y":0.06}
    if horizon=="3m": return {"1m":0.00,"5m":0.00,"10m":0.00,"15m":0.01,"1h":0.03,"4h":0.09,"1d":0.27,"1w":0.29,"1M":0.19,"1y":0.12}
    return {"1m":0.00,"5m":0.00,"10m":0.00,"15m":0.00,"1h":0.01,"4h":0.04,"1d":0.17,"1w":0.27,"1M":0.26,"1y":0.25}


def model_probability_estimate(score,confidence,risk_score,technical_score,agreement):
    """
    Heuristic estimate only. NOT historically calibrated.
    Kept deliberately bounded to avoid fake certainty.
    """
    x=(
        (score-50)*0.42 +
        (technical_score-50)*0.20 +
        (confidence-50)*0.10 +
        (agreement-50)*0.15 -
        max(risk_score-50,0)*0.10
    )
    prob=50 + x*0.55
    return round(clamp(prob,18,82),1)



# ---------------------- PRE-BREAKOUT / DON'T-CHASE ENGINE ----------------------

def early_acceleration_score(frames):
    """
    Detects a quiet-to-active transition across 1m/5m/10m/15m/1h charts.
    The goal is to reward acceleration before a large 24h candle makes the coin obvious.
    """
    needed=("1m","5m","10m","15m","1h")
    if not frames or any(k not in frames for k in needed):
        return 50.0, ["insufficient intraday data"]
    f1,f5,f10,f15,f60=(frames[k] for k in needed)
    score=50.0; reasons=[]

    short_strength = f1.get("score",50)*0.15 + f5.get("score",50)*0.35 + f10.get("score",50)*0.20 + f15.get("score",50)*0.30
    base = f60.get("score",50)
    delta = short_strength-base
    if delta>=12:
        score+=14; reasons.append("short timeframes accelerating ahead of 1h")
    elif delta>=6:
        score+=8; reasons.append("early intraday acceleration")
    elif delta<=-10:
        score-=10

    bullish_short=sum(1 for f in (f1,f5,f10,f15) if sf(f.get("trend_score"),50)>=62)
    if bullish_short>=3 and sf(f60.get("trend_score"),50)>=50:
        score+=10; reasons.append("broad short-timeframe trend alignment")

    squeeze=max(sf(f15.get("squeeze_score"),50),sf(f60.get("squeeze_score"),50))
    breakout=max(sf(f5.get("breakout_score"),50),sf(f15.get("breakout_score"),50))
    if squeeze>=68 and 55<=breakout<=78:
        score+=12; reasons.append("compression beginning to release")
    elif squeeze>=72 and breakout<55:
        score+=7; reasons.append("tight compression before trigger")

    r5=sf(f5.get("rsi"),50); r15=sf(f15.get("rsi"),50)
    if 50<=r5<=68 and 48<=r15<=66:
        score+=7; reasons.append("momentum strengthening without RSI exhaustion")
    if r5>=80 or r15>=78:
        score-=14; reasons.append("intraday momentum already overextended")

    if sf(f5.get("sweep_score"),50)>=66 or sf(f15.get("sweep_score"),50)>=66:
        score+=6; reasons.append("recent liquidity sweep supports reversal/expansion")

    return round(clamp(score),1), reasons[:6]


def prebreakout_setup_score(tf, change24=0.0, news_score=50.0, catalyst_score=50.0):
    """
    Scores whether price looks like it is BEFORE an upside expansion rather than
    already late in one. This is heuristic research logic, not a calibrated probability.
    """
    if not tf or not tf.get("available", True):
        return 50.0, 50.0, ["Chart data unavailable"]

    score=50.0
    entry=58.0
    reasons=[]

    squeeze=sf(tf.get("squeeze_score"),50)
    breakout=sf(tf.get("breakout_score"),50)
    sweep=sf(tf.get("sweep_score"),50)
    location=sf(tf.get("location_score"),50)
    agreement=sf(tf.get("agreement_score"),50)
    r=sf(tf.get("rsi"),50)
    atrp=sf(tf.get("atr_pct"),0)
    support=tf.get("support")
    resistance=tf.get("resistance")
    price=sf(tf.get("price"),0)

    # Compression before expansion.
    if squeeze>=70:
        score+=12
        reasons.append("volatility compression / squeeze")
    elif squeeze<=40:
        score-=4

    # Constructive market structure across timeframes.
    if agreement>=68:
        score+=10
        reasons.append("bullish multi-timeframe agreement")
    elif agreement<=38:
        score-=10

    # Liquidity sweep below lows can precede reversal/expansion.
    if sweep>=68:
        score+=9
        reasons.append("bullish liquidity sweep")
    elif sweep<=38:
        score-=8

    # Location near support or below nearby resistance is preferable to chasing.
    if location>=64:
        score+=9
        entry+=10
        reasons.append("constructive entry location")
    elif location<=40:
        score-=8
        entry-=15

    # A resistance level not too far overhead can represent a breakout trigger.
    if price>0 and resistance:
        dist=(resistance-price)/price*100
        if 0.35 <= dist <= 4.0:
            score+=12
            reasons.append(f"coiling {dist:.1f}% below resistance")
        elif dist < 0:
            # Already through resistance: useful only if this is a clean retest, not extension.
            if breakout>=68:
                score+=4
            else:
                entry-=8

    if price>0 and support:
        ds=(price-support)/price*100
        if 0 <= ds <= 3.0:
            score+=6
            entry+=7
            reasons.append("near established support")

    # RSI sweet spot: enough strength without obvious short-term exhaustion.
    if 48 <= r <= 66:
        score+=8
        reasons.append("RSI in pre-breakout strength zone")
    elif r >= 78:
        score-=16
        entry-=24
        reasons.append("RSI extended")
    elif r >= 72:
        score-=8
        entry-=12

    # News/catalyst should help *before* price fully reacts.
    if catalyst_score>=65:
        score+=min(14,(catalyst_score-60)*0.35)
        reasons.append("positive/upcoming catalyst")
    if news_score>=62:
        score+=min(8,(news_score-58)*0.20)

    # Avoid assets that already made the move.
    c=sf(change24)
    if c>=30:
        score-=30; entry-=38; reasons.append("already up 30%+ today — don't chase")
    elif c>=20:
        score-=23; entry-=30; reasons.append("already up 20%+ today — extended")
    elif c>=12:
        score-=14; entry-=20; reasons.append("already up 12%+ today")
    elif c>=8:
        score-=7; entry-=10
    elif -3 <= c <= 6:
        score+=5  # quiet/early movement is desirable

    # Very high ATR can mean the move is already disorderly.
    if atrp>=10:
        entry-=12
    elif atrp>=6:
        entry-=5

    return round(clamp(score,0,100),1), round(clamp(entry,0,100),1), reasons[:8]

def chase_penalty(change24, rsi_value, breakout_score=50, location_score=50):
    p=0.0
    c=sf(change24)
    r=sf(rsi_value,50)
    if c>=30:p+=28
    elif c>=20:p+=21
    elif c>=12:p+=13
    elif c>=8:p+=6
    if r>=82:p+=18
    elif r>=76:p+=10
    elif r>=70:p+=4
    if sf(location_score,50)<=35:p+=8
    # Clean breakout/retest can partially offset extension.
    if sf(breakout_score,50)>=72:p=max(0,p-5)
    return round(clamp(p,0,45),1)



# ---------------------- ADVANCED TRADING-STATE ENGINE ----------------------

def normalize_score(x, center=0, scale=1):
    return clamp(50 + safe_div(x-center, scale, 0)*10)

def rsi_series(values, period=14):
    if len(values) < period+1:
        return [50.0]*len(values)
    out=[50.0]*len(values)
    gains=[]; losses=[]
    for i in range(1,len(values)):
        d=values[i]-values[i-1]
        gains.append(max(d,0))
        losses.append(max(-d,0))
        if i>=period:
            ag=mean(gains[i-period:i],0)
            al=mean(losses[i-period:i],0)
            if al==0:
                out[i]=70.0 if ag>0 else 50.0
            else:
                rs=ag/al
                out[i]=100-100/(1+rs)
    return out

def rolling_return(values, n):
    if len(values)<=n:
        return 0.0
    return pct(values[-1], values[-1-n])

def volume_profile_zones(highs,lows,closes,quote_volumes,bins=24):
    if not closes:
        return {"poc":None,"hvn":[],"lvn":[]}
    lo=min(lows); hi=max(highs)
    if hi<=lo:
        return {"poc":closes[-1],"hvn":[closes[-1]],"lvn":[]}
    step=(hi-lo)/bins
    vols=[0.0]*bins
    for h,l,c,qv in zip(highs,lows,closes,quote_volumes):
        typical=(h+l+c)/3
        idx=int((typical-lo)/step)
        idx=max(0,min(bins-1,idx))
        vols[idx]+=qv
    centers=[lo+(i+0.5)*step for i in range(bins)]
    total=sum(vols) or 1
    poc_idx=max(range(bins),key=lambda i:vols[i])
    ranked=sorted(range(bins),key=lambda i:vols[i],reverse=True)
    hvn=[centers[i] for i in ranked[:3]]
    # Low-volume nodes between populated areas can accelerate price.
    med=statistics.median(vols) if vols else 0
    lvn=[centers[i] for i,v in enumerate(vols) if v<med*0.35 and v>0]
    return {
        "poc":centers[poc_idx],
        "hvn":hvn,
        "lvn":lvn[:6],
        "profile_concentration":max(vols)/total*100
    }

def anchored_vwap_from(raw, start_index):
    if not raw:
        return None
    start_index=max(0,min(start_index,len(raw)-1))
    num=0.0; den=0.0
    for r in raw[start_index:]:
        h,l,c=sf(r[2]),sf(r[3]),sf(r[4])
        qv=sf(r[7])
        typical=(h+l+c)/3
        num+=typical*qv; den+=qv
    return safe_div(num,den,sf(raw[-1][4]))

def anchored_vwap_pack(raw):
    if len(raw)<30:
        return {}
    highs=[sf(r[2]) for r in raw]
    lows=[sf(r[3]) for r in raw]
    closes=[sf(r[4]) for r in raw]
    lookback=min(80,len(raw))
    recent_high_idx=max(range(len(raw)-lookback,len(raw)),key=lambda i:highs[i])
    recent_low_idx=min(range(len(raw)-lookback,len(raw)),key=lambda i:lows[i])
    # breakout anchor: strongest positive candle with above-median volume in last 40
    vols=[sf(r[7]) for r in raw]
    medv=statistics.median(vols[-40:]) if len(vols)>=40 else statistics.median(vols)
    candidates=[]
    for i in range(max(1,len(raw)-40),len(raw)):
        o,c=sf(raw[i][1]),sf(raw[i][4])
        ret=pct(c,o) if o else 0
        if ret>0 and vols[i]>=medv*1.4:
            candidates.append((ret*vols[i],i))
    breakout_idx=max(candidates)[1] if candidates else max(0,len(raw)-20)
    return {
        "from_swing_low":anchored_vwap_from(raw,recent_low_idx),
        "from_swing_high":anchored_vwap_from(raw,recent_high_idx),
        "from_breakout":anchored_vwap_from(raw,breakout_idx),
        "breakout_anchor_index":breakout_idx
    }

def cvd_proxy(raw, lookback=40):
    """
    Kline proxy from taker-buy quote volume versus total quote volume.
    Not true exchange-wide CVD, but useful for aggressive-buying pressure.
    """
    if not raw:
        return {"score":50,"delta":0,"trend":"unavailable"}
    rows=raw[-lookback:]
    total=0.0; delta=0.0
    deltas=[]
    for r in rows:
        qv=sf(r[7]); taker_buy_q=sf(r[10])
        d=(taker_buy_q-(qv-taker_buy_q))
        total+=qv; delta+=d; deltas.append(d)
    ratio=safe_div(delta,total,0)
    score=clamp(50+ratio*160)
    first=sum(deltas[:len(deltas)//2]); second=sum(deltas[len(deltas)//2:])
    trend="improving" if second>first else "weakening"
    return {"score":round(score,1),"delta":round(ratio,4),"trend":trend}

def divergence_signal(closes, rsi_vals):
    if len(closes)<35 or len(rsi_vals)<35:
        return 50,"none"
    # Compare recent local extremes with previous block.
    prev_high=max(closes[-35:-15]); recent_high=max(closes[-15:])
    prev_low=min(closes[-35:-15]); recent_low=min(closes[-15:])
    prev_rsi_high=max(rsi_vals[-35:-15]); recent_rsi_high=max(rsi_vals[-15:])
    prev_rsi_low=min(rsi_vals[-35:-15]); recent_rsi_low=min(rsi_vals[-15:])
    if recent_high>prev_high and recent_rsi_high<prev_rsi_high-3:
        return 30,"bearish RSI divergence"
    if recent_low<prev_low and recent_rsi_low>prev_rsi_low+3:
        return 72,"bullish RSI divergence"
    return 50,"none"

def fakeout_score(tf):
    score=50; reasons=[]
    if not tf:
        return score,reasons
    if tf.get("breakout_label")=="breakout extended above resistance":
        score-=12; reasons.append("breakout is extended")
    if sf(tf.get("rejection_score"),50)<40:
        score-=12; reasons.append("bearish rejection wick")
    if sf(tf.get("sweep_score"),50)<40:
        score-=10; reasons.append("liquidity sweep above highs")
    if sf(tf.get("location_score"),50)<40:
        score-=8; reasons.append("poor location")
    if sf(tf.get("rsi"),50)>=78:
        score-=12; reasons.append("overbought RSI")
    if sf(tf.get("breakout_score"),50)>=70 and sf(tf.get("location_score"),50)>=55:
        score+=12; reasons.append("clean breakout / retest")
    return round(clamp(score),1),reasons

def classify_market_phase(tf, change24, cvd, divergence_score):
    squeeze=sf(tf.get("squeeze_score"),50)
    breakout=sf(tf.get("breakout_score"),50)
    location=sf(tf.get("location_score"),50)
    structure=sf(tf.get("structure_score"),50)
    r=sf(tf.get("rsi"),50)
    ch=sf(change24)
    if squeeze>=70 and breakout<60 and -4<=ch<=8:
        return "PRE-BREAKOUT"
    if breakout>=70 and location>=50 and ch<18:
        return "BREAKOUT / RETEST"
    if breakout>=60 and structure>=65 and cvd.get("score",50)>=55 and ch<25:
        return "CONTINUATION"
    if r>=78 or ch>=25 or divergence_score<40:
        return "EXHAUSTION RISK"
    if structure<=35 and cvd.get("score",50)<45:
        return "DISTRIBUTION / WEAKNESS"
    return "TREND / RANGE"

def post_breakout_continuation_score(tf, cvd, divergence_score, avwap_pack, price, change24):
    score=50.0; reasons=[]
    breakout=sf(tf.get("breakout_score"),50)
    location=sf(tf.get("location_score"),50)
    structure=sf(tf.get("structure_score"),50)
    agreement=sf(tf.get("agreement_score"),50)
    r=sf(tf.get("rsi"),50)

    if breakout>=68:
        score+=14; reasons.append("breakout/retest quality is strong")
    if structure>=68:
        score+=10; reasons.append("higher-timeframe structure supports continuation")
    if agreement>=66:
        score+=8; reasons.append("timeframes broadly agree")
    if cvd.get("score",50)>=60:
        score+=10; reasons.append("aggressive buyers remain active")
    elif cvd.get("score",50)<=40:
        score-=10; reasons.append("buying pressure is fading")
    if divergence_score<=35:
        score-=14; reasons.append("bearish divergence")
    elif divergence_score>=68:
        score+=7; reasons.append("bullish divergence")
    if location>=60:
        score+=7
    elif location<=38:
        score-=10
    # Anchored VWAP holding.
    bav=avwap_pack.get("from_breakout") if avwap_pack else None
    if bav and price>0:
        dist=(price-bav)/price*100
        if -0.5<=dist<=3.5:
            score+=10; reasons.append("holding breakout anchored VWAP")
        elif dist>8:
            score-=9; reasons.append("too far above breakout VWAP")
        elif dist<-1:
            score-=12; reasons.append("lost breakout VWAP")
    if r>=80:
        score-=12
    if change24>=20:
        score-=14; reasons.append("late-stage daily extension")
    return round(clamp(score),1),reasons[:8]

def relative_strength_score(asset, reference):
    """
    Compare asset return to BTC/ETH/market median across 24h and 7d.
    """
    a24=sf(asset.get("change_24h")); a7=sf(asset.get("change_7d"))
    b24=sf(reference.get("btc24")); b7=sf(reference.get("btc7"))
    e24=sf(reference.get("eth24")); e7=sf(reference.get("eth7"))
    m24=sf(reference.get("median24")); m7=sf(reference.get("median7"))
    edge=(a24-b24)*0.25 + (a24-e24)*0.20 + (a24-m24)*0.20 + (a7-b7)*0.15 + (a7-e7)*0.10 + (a7-m7)*0.10
    return round(clamp(50+edge*1.8),1)

def historical_analog_estimate(raw, horizon_bars=12):
    """
    Lightweight self-analogue backtest:
    Find historical windows with similar momentum/RSI/volatility/volume conditions,
    then observe how often price was higher after horizon_bars.
    This is still not a robust out-of-sample model, but is more grounded than a fixed formula.
    """
    if len(raw)<140:
        return {"samples":0,"up_rate":None,"avg_return":None,"score":50}
    closes=[sf(r[4]) for r in raw]
    highs=[sf(r[2]) for r in raw]
    lows=[sf(r[3]) for r in raw]
    vols=[sf(r[7]) for r in raw]
    rsis=rsi_series(closes,14)
    atrs=true_range_series(highs,lows,closes)
    target_i=len(raw)-1
    cur={
        "mom5":pct(closes[target_i],closes[target_i-5]) if target_i>=5 else 0,
        "mom20":pct(closes[target_i],closes[target_i-20]) if target_i>=20 else 0,
        "rsi":rsis[target_i],
        "atr":safe_div(mean(atrs[target_i-13:target_i+1]),closes[target_i],0)*100,
        "vr":safe_div(vols[target_i],mean(vols[target_i-20:target_i],1),1)
    }
    matches=[]
    for i in range(40,len(raw)-horizon_bars-1):
        feat={
            "mom5":pct(closes[i],closes[i-5]),
            "mom20":pct(closes[i],closes[i-20]),
            "rsi":rsis[i],
            "atr":safe_div(mean(atrs[i-13:i+1]),closes[i],0)*100,
            "vr":safe_div(vols[i],mean(vols[i-20:i],1),1)
        }
        dist=(
            abs(feat["mom5"]-cur["mom5"])*1.0 +
            abs(feat["mom20"]-cur["mom20"])*0.55 +
            abs(feat["rsi"]-cur["rsi"])*0.12 +
            abs(feat["atr"]-cur["atr"])*0.9 +
            abs(feat["vr"]-cur["vr"])*3.0
        )
        future=pct(closes[i+horizon_bars],closes[i])
        matches.append((dist,future))
    matches.sort(key=lambda x:x[0])
    chosen=matches[:20]
    if len(chosen)<8:
        return {"samples":len(chosen),"up_rate":None,"avg_return":None,"score":50}
    up=sum(1 for _,ret in chosen if ret>0)/len(chosen)*100
    avg=mean([ret for _,ret in chosen])
    score=clamp(35+up*0.45+avg*1.4)
    return {"samples":len(chosen),"up_rate":round(up,1),"avg_return":round(avg,2),"score":round(score,1)}


# ---------------------- BINANCE.US TECHNICAL / ORDER BOOK ----------------------

def ema(values, span):
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(alpha * x + (1 - alpha) * out[-1])
    return out

def rsi(values, period=14):
    if len(values) < period + 2:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = mean(gains[-period:])
    avg_loss = mean(losses[-period:])
    if avg_loss == 0:
        return 70.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def get_binance_us_symbols():
    try:
        j = req_json(f"{BINANCE_US}/api/v3/exchangeInfo", timeout=12, retries=2)
        return {
            x["symbol"] for x in j.get("symbols", [])
            if x.get("status") == "TRADING"
        }
    except Exception:
        return set()

def technical_for_asset(asset, available_symbols, horizon="4h"):
    pair=f"{asset['symbol']}USDT"
    if pair not in available_symbols:
        return {
            "available":False,"technical_score":50.0,"spread_bps":None,
            "book_depth":None,"book_imbalance":None,"rsi":None,
            "trend":"unavailable","structure":"unavailable","support":None,
            "resistance":None,"chart_signals":[],"timeframes":{},
            "agreement_score":50.0,
            "market_phase":"unavailable","post_breakout_score":50.0,"fakeout_score":50.0,
            "cvd_score":50.0,"cvd_delta":0.0,"cvd_trend":"unavailable",
            "divergence_score":50.0,"divergence_label":"none",
            "anchored_vwap":{},"volume_profile":{},"historical_analog":{"samples":0,"up_rate":None,"avg_return":None,"score":50},
            "early_acceleration_score":50.0,"early_acceleration_reasons":[]
        }

    try:
        frames={}
        raw1m=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"1m","limit":220},timeout=12,retries=2)
        raw5m=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"5m","limit":220},timeout=12,retries=2)
        raw15m=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"15m","limit":220},timeout=12,retries=2)
        raw1h=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"1h","limit":220},timeout=12,retries=2)
        raw4h=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"4h","limit":220},timeout=12,retries=2)
        raw1d=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"1d","limit":365},timeout=12,retries=2)
        raw1w=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"1w","limit":180},timeout=12,retries=2)
        raw1M=req_json(f"{BINANCE_US}/api/v3/klines",{"symbol":pair,"interval":"1M","limit":60},timeout=12,retries=2)

        frames["1m"]=analyze_timeframe(raw1m)
        frames["5m"]=analyze_timeframe(raw5m)
        frames["10m"]=analyze_timeframe(aggregate_10m(raw5m))
        frames["15m"]=analyze_timeframe(raw15m)
        frames["1h"]=analyze_timeframe(raw1h)
        frames["4h"]=analyze_timeframe(raw4h)
        frames["1d"]=analyze_timeframe(raw1d)
        frames["1w"]=analyze_timeframe(raw1w)
        frames["1M"]=analyze_timeframe(raw1M)
        # "1y" is a one-year structural read derived from daily candles.
        frames["1y"]=analyze_timeframe(raw1d[-365:])
        early_accel, early_accel_reasons = early_acceleration_score(frames)

        # Deeper trading-state inputs.
        active_raw = raw5m if horizon=="1h" else raw15m if horizon in ("4h","12h","24h") else raw1h if horizon in ("3d","1w") else raw4h if horizon=="1m" else raw1d
        cvd = cvd_proxy(active_raw,40)
        avwap = anchored_vwap_pack(active_raw)
        analog_horizon = 12 if horizon=="1h" else 16 if horizon=="4h" else 20 if horizon=="12h" else 8 if horizon=="24h" else 12 if horizon=="3d" else 24 if horizon=="1w" else 20
        analog = historical_analog_estimate(active_raw, analog_horizon)

        weights=timeframe_weights_for_horizon(horizon)
        weighted=sum(frames[k]["score"]*w for k,w in weights.items() if k in frames)
        total=sum(w for k,w in weights.items() if k in frames) or 1
        multi=weighted/total

        bullish=sum(1 for f in frames.values() if f["trend_score"]>=62)
        bearish=sum(1 for f in frames.values() if f["trend_score"]<=35)
        agreement=clamp(50+(bullish-bearish)*6)
        if bullish>=7:
            multi+=7
        if bearish>=7:
            multi-=12
        if bullish>=4 and bearish>=4:
            multi-=8

        active=frames["5m"] if horizon=="1h" else frames["15m"] if horizon in ("4h","12h","24h") else frames["1h"] if horizon in ("3d","1w") else frames["1d"]
        structural=frames["15m"] if horizon=="1h" else frames["1h"] if horizon in ("4h","12h","24h") else frames["4h"] if horizon in ("3d","1w") else frames["1d"] if horizon=="1m" else frames["1w"]

        fakeout, fakeout_reasons = fakeout_score(active)
        post_breakout, post_breakout_reasons = post_breakout_continuation_score(
            active, cvd, active.get("divergence_score",50), avwap, active["price"], asset.get("change_24h",0)
        )
        phase=classify_market_phase(active,asset.get("change_24h",0),cvd,active.get("divergence_score",50))

        book=req_json(f"{BINANCE_US}/api/v3/depth",{"symbol":pair,"limit":100},timeout=10,retries=2)
        bids=[(sf(x[0]),sf(x[1])) for x in book.get("bids",[])]
        asks=[(sf(x[0]),sf(x[1])) for x in book.get("asks",[])]

        spread_bps=None; depth=None; imbalance=None; orderbook_score=50
        if bids and asks:
            best_bid=bids[0][0]; best_ask=asks[0][0]
            mid=(best_bid+best_ask)/2
            spread_bps=safe_div(best_ask-best_bid,mid,0)*10000
            lo=mid*0.995; hi=mid*1.005
            bd=sum(px*q for px,q in bids if px>=lo)
            ad=sum(px*q for px,q in asks if px<=hi)
            depth=bd+ad
            imbalance=safe_div(bd-ad,depth,0)
            if spread_bps<=3:orderbook_score+=10
            elif spread_bps>15:orderbook_score-=15
            if depth>=1_000_000:orderbook_score+=8
            elif depth<100_000:orderbook_score-=10
            if imbalance>=0.12:orderbook_score+=8
            elif imbalance<=-0.18:orderbook_score-=8

        # Order book matters most on short horizons.
        ob_weight=0.20 if horizon=="1h" else 0.18 if horizon=="4h" else 0.16 if horizon=="12h" else 0.14 if horizon=="24h" else 0.10 if horizon=="3d" else 0.06 if horizon=="1w" else 0.02
        multi=clamp(multi*(1-ob_weight)+clamp(orderbook_score)*ob_weight)

        # Blend continuation/fakeout/CVD/analog lightly; these are higher-order signals.
        multi=clamp(
            multi*0.68 +
            post_breakout*0.12 +
            fakeout*0.06 +
            cvd.get("score",50)*0.06 +
            active.get("divergence_score",50)*0.04 +
            analog.get("score",50)*0.04
        )

        signals=[]
        priority=("1m","5m","10m","15m","1h","4h","1d","1w","1M","1y")
        for tf in priority:
            f=frames[tf]
            if f["sweep_label"]!="none":
                signals.append(f"{tf}: {f['sweep_label']}")
            if f["breakout_label"]!="none":
                signals.append(f"{tf}: {f['breakout_label']}")
            if f["rejection_label"]!="none":
                signals.append(f"{tf}: {f['rejection_label']}")
        signals.append(f"{structural['structure_label']} on structural timeframe")
        if active.get("divergence_label")!="none": signals.append(active.get("divergence_label"))
        signals.extend(post_breakout_reasons[:4])
        signals.extend(fakeout_reasons[:3])
        if active["location_label"]!="neutral location":
            signals.append(f"{active['location_label']} on active timeframe")

        support=active["support"]["center"] if active["support"] else None
        resistance=active["resistance"]["center"] if active["resistance"] else None

        return {
            "available":True,
            "price":round(active["price"],8),
            "technical_score":round(clamp(multi),1),
            "spread_bps":round(spread_bps,2) if spread_bps is not None else None,
            "book_depth":round(depth,2) if depth is not None else None,
            "book_imbalance":round(imbalance,3) if imbalance is not None else None,
            "rsi":round(active["rsi"],1),
            "trend":structural["trend_label"],
            "structure":structural["structure_label"],
            "support":round(support,8) if support else None,
            "resistance":round(resistance,8) if resistance else None,
            "atr_pct":round(active["atr_pct"],2),
            "location_score":round(active["location_score"],1),
            "breakout_score":round(active["breakout_score"],1),
            "sweep_score":round(active["sweep_score"],1),
            "squeeze_score":round(active["squeeze_score"],1),
            "agreement_score":round(agreement,1),
            "chart_signals":signals[:16],
            "market_phase":phase,
            "post_breakout_score":post_breakout,
            "fakeout_score":fakeout,
            "cvd_score":cvd.get("score",50),
            "cvd_delta":cvd.get("delta",0),
            "cvd_trend":cvd.get("trend","unavailable"),
            "divergence_score":active.get("divergence_score",50),
            "divergence_label":active.get("divergence_label","none"),
            "anchored_vwap":avwap,
            "volume_profile":active.get("volume_profile",{}),
            "historical_analog":analog,
            "early_acceleration_score":early_accel,
            "early_acceleration_reasons":early_accel_reasons,
            "timeframes":{
                tf:{
                    "score":round(frames[tf]["score"],1),
                    "trend":frames[tf]["trend_label"],
                    "structure":frames[tf]["structure_label"]
                } for tf in priority
            }
        }
    except Exception:
        return {
            "available":False,"technical_score":50.0,"spread_bps":None,
            "book_depth":None,"book_imbalance":None,"rsi":None,
            "trend":"error","structure":"error","support":None,
            "resistance":None,"chart_signals":[],"timeframes":{},
            "agreement_score":50.0,
            "market_phase":"unavailable","post_breakout_score":50.0,"fakeout_score":50.0,
            "cvd_score":50.0,"cvd_delta":0.0,"cvd_trend":"unavailable",
            "divergence_score":50.0,"divergence_label":"none",
            "anchored_vwap":{},"volume_profile":{},"historical_analog":{"samples":0,"up_rate":None,"avg_return":None,"score":50},
            "early_acceleration_score":50.0,"early_acceleration_reasons":[]
        }


# ---------------------- NEWS / CATALYST ----------------------

def _catalyst_for_asset_uncached(asset,horizon):
    days={"1h":1,"4h":1,"12h":2,"24h":3,"3d":7,"1w":14,"1m":45,"3m":120,"1y":365}[horizon]
    google=google_news(f'"{asset["name"]}" {asset["symbol"]}',days,limit=8);industry=relevant_industry_news(asset,6);seen=set();headlines=[]
    for x in google+industry:
        k=re.sub(r"\W+"," ",x.get("title","").lower()).strip()
        if k and k not in seen:seen.add(k);headlines.append(x)
    reddit=reddit_context(asset,7);pos=neg=future=0;catalyst_titles=[]
    for h in headlines:
        sig,f=headline_signal(h["title"])
        if sig>0:pos+=sig
        elif sig<0:neg+=abs(sig)
        future+=f
        if sig!=0 or f>0:catalyst_titles.append(h["title"])
    news_score=clamp(50+pos*6-neg*8);catalyst_score=clamp(40+future*7+pos*2-neg*3);recent_run=max(abs(asset["change_24h"]),abs(asset["change_7d"])/2);priced_in=0
    if catalyst_score>=60:
        if recent_run>30:priced_in=14
        elif recent_run>18:priced_in=9
        elif recent_run>10:priced_in=5
    return {"news_score":round(news_score,1),"catalyst_score":round(catalyst_score,1),"priced_in_penalty":priced_in,"headlines":headlines[:7],"catalyst_titles":catalyst_titles[:5],"news_mentions":len(headlines),"social_score":reddit["score"],"reddit_mentions":reddit["mentions"],"reddit_sentiment":reddit["sentiment"],"reddit_posts":reddit["posts"]}


def catalyst_for_asset(asset, horizon):
    key=f"{asset.get('id')}:{horizon}"
    now=time.time()
    with news_cache_lock:
        cached=news_cache.get(key)
        if cached and now-cached["ts"]<NEWS_CACHE_SECONDS:
            return cached["data"]
    data=_catalyst_for_asset_uncached(asset,horizon)
    with news_cache_lock:
        news_cache[key]={"ts":now,"data":data}
    return data


# ---------------------- V12 TRADE-PLAN / VALIDATION ENGINE ----------------------

PREDICTION_DB = os.environ.get("PREDICTION_DB_PATH", "/tmp/crypto_radar_predictions.sqlite3")
prediction_db_lock = threading.Lock()

def init_prediction_db():
    try:
        with prediction_db_lock:
            con = sqlite3.connect(PREDICTION_DB, timeout=5)
            con.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bucket INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    setup_type TEXT,
                    entry REAL,
                    stop REAL,
                    target1 REAL,
                    target2 REAL,
                    score REAL,
                    do_not_trade REAL,
                    max_seen REAL,
                    min_seen REAL,
                    status TEXT DEFAULT 'OPEN',
                    outcome TEXT,
                    UNIQUE(bucket, symbol, horizon)
                )
            """)
            con.commit()
            con.close()
    except Exception:
        pass

init_prediction_db()

def setup_classifier(technical, prebreakout_score, entry_quality):
    phase = str(technical.get("market_phase") or "")
    squeeze = sf(technical.get("squeeze_score"),50)
    sweep = sf(technical.get("sweep_score"),50)
    fakeout = sf(technical.get("fakeout_score"),50)
    breakout = sf(technical.get("breakout_score"),50)
    structure = str(technical.get("structure") or "").lower()
    trend = str(technical.get("trend") or "").lower()

    if fakeout <= 35:
        return "Fakeout risk"
    if phase == "BREAKOUT / RETEST":
        return "Breakout / retest"
    if sweep >= 68 and entry_quality >= 55:
        return "Liquidity sweep / reclaim"
    if prebreakout_score >= 72 and squeeze >= 65:
        return "Pre-breakout compression"
    if breakout >= 68 and entry_quality >= 55:
        return "Breakout continuation"
    if ("higher" in structure or "bull" in trend) and entry_quality >= 60:
        return "Trend pullback"
    return "Watch / no clear setup"

def market_alignment(regime, rs_ref, relative_strength):
    btc24 = sf(rs_ref.get("btc24"))
    eth24 = sf(rs_ref.get("eth24"))
    score = sf(regime.get("score"),50) * 0.45 + relative_strength * 0.35
    score += clamp(50 + (btc24 + eth24) * 3, 0, 100) * 0.20
    if score >= 63:
        label = "supportive"
    elif score <= 40:
        label = "hostile"
    else:
        label = "mixed"
    return round(clamp(score),1), label

def trade_plan(asset, technical, regime, rs_ref, relative_strength, prebreakout_score, entry_quality, chase):
    price = sf(technical.get("price"), sf(asset.get("price"),0))
    atr_pct = max(0.25, sf(technical.get("atr_pct"),1.2))
    support = sf(technical.get("support"),0)
    resistance = sf(technical.get("resistance"),0)
    spread = technical.get("spread_bps")
    fakeout = sf(technical.get("fakeout_score"),50)
    agreement = sf(technical.get("agreement_score"),50)
    rsi = sf(technical.get("rsi"),50)
    liq = sf(asset.get("liquidity_score"),50)
    risk_score = sf(asset.get("risk_score"),50)

    setup_type = setup_classifier(technical, prebreakout_score, entry_quality)
    align_score, align_label = market_alignment(regime, rs_ref, relative_strength)

    if price <= 0:
        return {
            "setup_type":setup_type,"market_alignment":align_label,"market_alignment_score":align_score,
            "entry_low":None,"entry_high":None,"stop":None,"target1":None,"target2":None,
            "risk_reward":None,"trigger":"Wait for reliable live price data.",
            "do_not_trade_score":100,"do_not_trade_reasons":["reliable price data unavailable"],
            "action":"AVOID"
        }

    atr = price * atr_pct / 100.0
    entry_low = max(0, price - atr * 0.18)
    entry_high = price + atr * 0.12

    # Put invalidation below structural support when that support is nearby;
    # otherwise use an ATR-derived technical invalidation.
    atr_stop = price - atr * 1.15
    if support > 0 and support < price and (price-support)/price < 0.08:
        stop = min(price-atr*0.55, support-atr*0.20)
    else:
        stop = atr_stop
    stop = max(0.00000001, stop)

    risk_per_unit = max(price-stop, price*0.0025)
    natural_t1 = resistance if resistance > price and resistance < price + risk_per_unit*3.0 else 0
    target1 = max(price + risk_per_unit*1.35, natural_t1)
    target2 = price + risk_per_unit*2.35
    rr = (target2-price) / risk_per_unit if risk_per_unit > 0 else 0

    dnt = 0.0
    reasons = []
    if not technical.get("available"):
        dnt += 45; reasons.append("deep chart data unavailable")
    if spread is not None:
        if spread > 25: dnt += 25; reasons.append("very wide spread")
        elif spread > 12: dnt += 12; reasons.append("wide spread")
    if liq < 35:
        dnt += 18; reasons.append("thin liquidity")
    if chase >= 15:
        dnt += min(30,chase); reasons.append("price looks extended / chase risk")
    if entry_quality < 45:
        dnt += 15; reasons.append("weak entry location")
    if fakeout < 40:
        dnt += 18; reasons.append("fakeout risk is elevated")
    if agreement < 40:
        dnt += 14; reasons.append("timeframes conflict")
    if rsi >= 78:
        dnt += 12; reasons.append("short-term RSI is overheated")
    if align_label == "hostile":
        dnt += 18; reasons.append("BTC / ETH / broad market backdrop is hostile")
    elif align_label == "mixed":
        dnt += 5
    if risk_score > 75:
        dnt += 10; reasons.append("asset risk is very high")
    if rr < 1.6:
        dnt += 16; reasons.append("risk/reward is weak")
    dnt = round(clamp(dnt),1)

    if resistance > price:
        trigger = f"Prefer a 15m close above {resistance:.8g} with stronger-than-normal volume, or a clean retest that holds."
    elif support > 0:
        trigger = f"Prefer a hold/reclaim above support near {support:.8g} with 5m/15m momentum turning up."
    else:
        trigger = "Wait for 5m/15m confirmation: higher low, improving volume, and no immediate rejection."

    if dnt >= 65:
        action = "AVOID"
    elif dnt >= 42:
        action = "WAIT"
    elif setup_type in ("Pre-breakout compression","Liquidity sweep / reclaim","Breakout / retest","Trend pullback"):
        action = "SETUP READY"
    else:
        action = "WATCH"

    return {
        "setup_type":setup_type,
        "market_alignment":align_label,
        "market_alignment_score":align_score,
        "entry_low":round(entry_low,8),
        "entry_high":round(entry_high,8),
        "stop":round(stop,8),
        "target1":round(target1,8),
        "target2":round(target2,8),
        "risk_reward":round(rr,2),
        "trigger":trigger,
        "do_not_trade_score":dnt,
        "do_not_trade_reasons":reasons[:6],
        "action":action,
    }

def news_intelligence(catalyst):
    headlines = catalyst.get("headlines") or []
    titles = [str(x.get("title") or "") for x in headlines if x.get("title")]
    sources = {str(x.get("source") or "Google News") for x in headlines}
    joined = " ".join(titles).lower()

    event = "general news"
    if any(k in joined for k in ("listing","lists on","listed on","exchange listing")):
        event = "exchange listing"
    elif any(k in joined for k in ("upgrade","mainnet","launch","release","hard fork")):
        event = "protocol / product event"
    elif any(k in joined for k in ("etf","sec","regulation","lawsuit","court")):
        event = "regulatory event"
    elif any(k in joined for k in ("hack","exploit","breach","drain")):
        event = "security event"
    elif any(k in joined for k in ("partnership","integrat","collaborat")):
        event = "partnership / integration"

    credibility = clamp(45 + min(len(sources),4)*10 + min(len(titles),6)*3)
    novelty = clamp(40 + min(len(titles),6)*7 - max(len(titles)-len(sources)*2,0)*4)
    return {
        "event_type":event,
        "source_count":len(sources),
        "credibility_score":round(credibility,1),
        "novelty_score":round(novelty,1),
    }

def update_prediction_outcomes(symbol_prices):
    """Update open paper predictions using prices observed on subsequent scans."""
    if not symbol_prices:
        return
    try:
        with prediction_db_lock:
            con=sqlite3.connect(PREDICTION_DB,timeout=5)
            now=int(time.time())
            for symbol,price in symbol_prices.items():
                price=sf(price)
                if price<=0: continue
                rows=con.execute(
                    "SELECT id,entry,stop,target1,target2,max_seen,min_seen,status FROM predictions WHERE symbol=? AND status='OPEN'",
                    (symbol.upper(),)
                ).fetchall()
                for rid,entry,stop,t1,t2,mx,mn,status in rows:
                    mx=max(sf(mx,entry),price); mn=min(sf(mn,entry),price)
                    new_status="OPEN"; outcome=None
                    # Conservative ordering if both levels could have occurred between scans:
                    # mark STOP first rather than pretending target was hit first.
                    if stop and price <= stop:
                        new_status="CLOSED"; outcome="STOP"
                    elif t2 and price >= t2:
                        new_status="CLOSED"; outcome="TARGET2"
                    elif t1 and price >= t1:
                        new_status="CLOSED"; outcome="TARGET1"
                    con.execute(
                        "UPDATE predictions SET updated_at=?,max_seen=?,min_seen=?,status=?,outcome=? WHERE id=?",
                        (now,mx,mn,new_status,outcome,rid)
                    )
            con.commit(); con.close()
    except Exception:
        pass

def record_predictions(rows, horizon):
    """Paper-log strongest actionable setups. Default Render storage is ephemeral."""
    try:
        bucket=int(time.time()//900)
        now=int(time.time())
        with prediction_db_lock:
            con=sqlite3.connect(PREDICTION_DB,timeout=5)
            for r in rows[:10]:
                if r.get("action") not in ("SETUP READY","WATCH"):
                    continue
                con.execute("""
                    INSERT OR IGNORE INTO predictions
                    (bucket,created_at,updated_at,symbol,horizon,setup_type,entry,stop,target1,target2,score,do_not_trade,max_seen,min_seen,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')
                """,(
                    bucket,now,now,str(r.get("symbol","")).upper(),horizon,r.get("setup_type"),
                    sf(r.get("price")),sf(r.get("stop")),sf(r.get("target1")),sf(r.get("target2")),
                    sf(r.get("score")),sf(r.get("do_not_trade_score")),sf(r.get("price")),sf(r.get("price"))
                ))
            con.commit();con.close()
    except Exception:
        pass

def performance_summary(horizon=None, setup_type=None):
    try:
        with prediction_db_lock:
            con=sqlite3.connect(PREDICTION_DB,timeout=5)
            q="SELECT outcome,COUNT(*) FROM predictions WHERE status='CLOSED'"
            args=[]
            if horizon:
                q+=" AND horizon=?";args.append(horizon)
            if setup_type:
                q+=" AND setup_type=?";args.append(setup_type)
            q+=" GROUP BY outcome"
            counts=dict(con.execute(q,args).fetchall())
            samples=sum(counts.values())
            wins=counts.get("TARGET1",0)+counts.get("TARGET2",0)
            con.close()
        return {
            "samples":samples,
            "wins":wins,
            "losses":counts.get("STOP",0),
            "win_rate":round(wins/samples*100,1) if samples else None
        }
    except Exception:
        return {"samples":0,"wins":0,"losses":0,"win_rate":None}



# ---------------------- V13 DECISION / EXECUTION QUALITY LAYER ----------------------

def confirmation_quality(row):
    """Separate 'good coin' from 'good entry right now'."""
    tech = sf(row.get("technical_score"),50)
    entry = sf(row.get("entry_quality"),50)
    pre = sf(row.get("prebreakout_score"),50)
    agree = sf(row.get("agreement_score"),50)
    social = sf(row.get("social_score"),50)
    news = sf(row.get("news_score"),50)
    dnt = sf(row.get("do_not_trade_score"),50)
    chase = sf(row.get("chase_penalty"),0)
    align = sf(row.get("market_alignment_score"),50)
    q = tech*.18 + entry*.23 + pre*.15 + agree*.14 + align*.15 + news*.08 + social*.03 + (100-dnt)*.04
    q -= min(chase,30)*.45
    return round(clamp(q),1)

def signal_disagreement(row):
    """High disagreement means the ingredients do not tell the same story."""
    vals = [
        sf(row.get("momentum_score"),50), sf(row.get("technical_score"),50),
        sf(row.get("entry_quality"),50), sf(row.get("prebreakout_score"),50),
        sf(row.get("news_score"),50), sf(row.get("market_alignment_score"),50),
    ]
    mean = sum(vals)/len(vals)
    variance = sum((v-mean)**2 for v in vals)/len(vals)
    # Map dispersion into a 0-100 caution score.
    return round(clamp((variance**0.5)*3.2),1)

def position_risk_hint(row):
    """
    Not an account-specific order size. Gives a conservative risk-budget band
    based on setup quality so the UI never implies 'bet X dollars'.
    """
    cq=sf(row.get("confirmation_quality"),50)
    dnt=sf(row.get("do_not_trade_score"),50)
    disagree=sf(row.get("signal_disagreement"),50)
    rr=sf(row.get("risk_reward"),0)
    if dnt>=65 or cq<45 or rr<1.4:
        return "0% — skip / paper trade"
    if dnt>=42 or disagree>=60 or cq<60:
        return "0.25% account risk max"
    if cq>=75 and dnt<25 and disagree<40 and rr>=2:
        return "0.5% account risk max"
    return "0.25–0.5% account risk max"

def decision_summary(row):
    positives=[]; negatives=[]
    candidates=[
        ("entry location",sf(row.get("entry_quality"),50)),
        ("technical structure",sf(row.get("technical_score"),50)),
        ("pre-breakout setup",sf(row.get("prebreakout_score"),50)),
        ("market alignment",sf(row.get("market_alignment_score"),50)),
        ("news",sf(row.get("news_score"),50)),
        ("relative strength",sf(row.get("relative_strength"),50)),
    ]
    for name,val in sorted(candidates,key=lambda x:x[1],reverse=True):
        if val>=62 and len(positives)<3: positives.append(f"{name} {val:.0f}/100")
    for name,val in sorted(candidates,key=lambda x:x[1]):
        if val<=42 and len(negatives)<3: negatives.append(f"{name} {val:.0f}/100")
    negatives += [x for x in (row.get("do_not_trade_reasons") or []) if x not in negatives]
    return {
        "best_reasons":positives[:3],
        "main_risks":negatives[:3],
        "verdict": "Strong confirmation" if sf(row.get("confirmation_quality"),50)>=72 and sf(row.get("do_not_trade_score"),50)<35
                   else "Wait for confirmation" if sf(row.get("do_not_trade_score"),50)<65
                   else "Skip this setup"
    }

def enrich_decision_rows(rows):
    for r in rows:
        r["confirmation_quality"]=confirmation_quality(r)
        r["signal_disagreement"]=signal_disagreement(r)
        r["risk_budget_hint"]=position_risk_hint(r)
        r["decision_summary"]=decision_summary(r)
        # Never allow an attractive headline score to hide a poor execution state.
        if r["do_not_trade_score"]>=65:
            r["action"]="AVOID"
        elif r["confirmation_quality"]<52 or r["signal_disagreement"]>=70:
            r["action"]="WAIT"
    return rows

# ---------------------- FINAL SCORE ----------------------

def moonshot_profile(asset, risk):
    mcap = asset["market_cap"]
    risk_score = asset["risk_score"]

    if risk != "extreme":
        return None

    if mcap < 25_000_000:
        label = "Micro-cap moonshot"
        upside = "A 5–10× move is conceivable in an exceptional catalyst cycle"
    elif mcap < 100_000_000:
        label = "Small-cap moonshot"
        upside = "A 3–8× move is conceivable in a strong catalyst cycle"
    elif mcap < 500_000_000:
        label = "High-beta small cap"
        upside = "A 2–5× move is conceivable in a strong cycle"
    else:
        label = "High-volatility large cap"
        upside = "Large upside is possible, but 10× is structurally harder"

    return {
        "label":label,
        "upside":upside,
        "warning":f"Risk score {risk_score:.0f}/100; severe drawdown or near-total loss is possible"
    }

def final_score(asset, technical, catalyst, regime, fng_value, horizon, risk):
    hw = HORIZONS[horizon]
    cfg = RISK_MODES[risk]

    technical_score = technical["technical_score"]
    news_score = catalyst["news_score"]
    catalyst_score = catalyst["catalyst_score"]
    regime_score = regime["score"]

    # Fear/greed is contextual, not a direct signal.
    sentiment_context = 50
    if 35 <= fng_value <= 70:
        sentiment_context = 58
    elif fng_value >= 85:
        sentiment_context = 42
    elif fng_value <= 15:
        sentiment_context = 44

    weighted = (
        asset["momentum_score"] * hw["momentum"] +
        technical_score * hw["technical"] +
        asset["liquidity_score"] * hw["liquidity"] +
        news_score * hw["news"] +
        catalyst_score * hw["catalyst"] +
        asset["fundamental_score"] * hw["fundamental"] +
        regime_score * hw["regime"] +
        sentiment_context * 0.35
    )

    denom = (
        hw["momentum"] + hw["technical"] + hw["liquidity"] +
        hw["news"] + hw["catalyst"] + hw["fundamental"] +
        hw["regime"] + 0.35
    )

    score = weighted / denom

    # Penalize risk differently by mode.
    score -= asset["risk_score"] * 0.10 * cfg["risk_penalty"]

    # Penalize "already pumped on known news".
    score -= catalyst["priced_in_penalty"]

    # Extreme mode can reward small-cap asymmetry, but only if liquidity is not terrible.
    if risk == "extreme" and asset["liquidity_score"] >= 35:
        if asset["market_cap"] < 25_000_000:
            score += 8
        elif asset["market_cap"] < 100_000_000:
            score += 5
        elif asset["market_cap"] < 500_000_000:
            score += 2

    # Conservative mode strongly rejects micro/small caps.
    if risk == "conservative" and asset["market_cap"] < 5_000_000_000:
        score -= 8

    # Data quality/confidence is NOT win probability.
    data_points = 3
    if technical["available"]:
        data_points += 3
    if catalyst["news_mentions"] > 0:
        data_points += 2
    if asset["volume"] > 10_000_000:
        data_points += 1
    if asset["market_cap"] > 100_000_000:
        data_points += 1

    confidence = clamp(35 + data_points * 5.5)

    return round(clamp(score),1), round(confidence,1)

def scenario_label(score, risk_score):
    if score >= 82 and risk_score <= 55:
        return "Strong setup"
    if score >= 82:
        return "Strong but high-risk setup"
    if score >= 72:
        return "Watch closely"
    if score >= 62:
        return "Speculative watch"
    return "Low priority"

# ---------------------- OPTIONAL STOCK SUPPORT ----------------------

def finnhub_enabled():
    return bool(FINNHUB_API_KEY)

# Stock support remains optional in this file. Crypto is the primary engine.
# The UI clearly labels stocks disabled unless FINNHUB_API_KEY is present.
# This avoids pretending to scan stocks when no reliable stock feed is configured.

# ---------------------- SCAN PIPELINE ----------------------

def scan_crypto(horizon, risk):
    rows, provider, warning = get_crypto_universe()
    if not rows:
        raise RuntimeError(warning or "No crypto market data returned")

    regime = market_regime(rows)
    fng_value, fng_label = fear_greed()

    # Broad-market reference for relative strength.
    btc=next((x for x in rows if str(x.get("symbol","")).lower()=="btc"),{})
    eth=next((x for x in rows if str(x.get("symbol","")).lower()=="eth"),{})
    valid24=[sf(x.get("price_change_percentage_24h_in_currency")) for x in rows[:300]]
    valid7=[sf(x.get("price_change_percentage_7d_in_currency")) for x in rows[:300]]
    rs_ref={
        "btc24":sf(btc.get("price_change_percentage_24h_in_currency")),
        "btc7":sf(btc.get("price_change_percentage_7d_in_currency")),
        "eth24":sf(eth.get("price_change_percentage_24h_in_currency")),
        "eth7":sf(eth.get("price_change_percentage_7d_in_currency")),
        "median24":statistics.median(valid24) if valid24 else 0,
        "median7":statistics.median(valid7) if valid7 else 0
    }

    prelim = preprocess_crypto(rows, horizon, risk)
    if not prelim:
        return {
            "updated":int(time.time()),
            "provider":provider,
            "warning":warning,
            "regime":regime,
            "fear_greed":{"value":fng_value,"label":fng_label},
            "results":[],
            "diagnostics":{
                "raw_universe":len(rows),
                "prelim_candidates":0,
                "message":"No coins passed the selected risk/liquidity filters."
            }
        }

    prelim = prelim[:PRELIM_LIMIT]
    available_symbols = get_binance_us_symbols()

    # Deep technical analysis in parallel.
    technical_map = {}
    tech_assets = prelim[:TECH_LIMIT]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            ex.submit(technical_for_asset, a, available_symbols, horizon): a["id"]
            for a in tech_assets
        }
        for f in as_completed(futs):
            technical_map[futs[f]] = f.result()

    # News/catalyst only for strongest candidates. This is the expensive layer.
    catalyst_map = {}
    news_assets = prelim[:NEWS_LIMIT]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            ex.submit(catalyst_for_asset, a, horizon): a["id"]
            for a in news_assets
        }
        for f in as_completed(futs):
            catalyst_map[futs[f]] = f.result()

    coinbase_markets,coinbase_ok=get_coinbase_markets()

    final = []
    for a in prelim:
        technical = technical_map.get(a["id"], {
            "available":False,
            "technical_score":50.0,
            "spread_bps":None,
            "book_depth":None,
            "book_imbalance":None,
            "rsi":None,
            "trend":"not deeply scanned",
        })

        catalyst = catalyst_map.get(a["id"], {
            "news_score":50.0,
            "catalyst_score":40.0,
            "priced_in_penalty":0,
            "headlines":[],
            "catalyst_titles":[],
            "news_mentions":0,
            "social_score":50.0,"reddit_mentions":0,"reddit_sentiment":"unavailable","reddit_posts":[],
        })

        relative_strength = relative_strength_score(a, rs_ref)

        score, confidence = final_score(
            a, technical, catalyst, regime, fng_value, horizon, risk
        )

        # Relative strength is important for finding leaders before/through breakouts.
        rs_weight={"1h":0.12,"4h":0.12,"12h":0.11,"24h":0.10,"3d":0.10,"1w":0.09,"1m":0.07,"3m":0.05,"1y":0.03}.get(horizon,0.07)
        score=clamp(score*(1-rs_weight)+relative_strength*rs_weight)

        prebreakout_score, entry_quality, prebreakout_reasons = prebreakout_setup_score(
            technical,
            a.get("change_24h",0),
            catalyst.get("news_score",50),
            catalyst.get("catalyst_score",50)
        )
        chase = chase_penalty(
            a.get("change_24h",0),
            technical.get("rsi"),
            technical.get("breakout_score",50),
            technical.get("location_score",50)
        )
        early_accel = sf(technical.get("early_acceleration_score"),50)
        social_context=sf(catalyst.get("social_score"),50)
        social_w={"1h":0.03,"4h":0.04,"12h":0.04,"24h":0.04,"3d":0.03}.get(horizon,0.02)

        # For short horizons, explicitly reward setups that appear to be BEFORE the move.
        # For longer horizons this matters less than quality/fundamentals/catalysts.
        pre_w = {"1h":0.38,"4h":0.36,"12h":0.33,"24h":0.30,"3d":0.25,"1w":0.18,"1m":0.10,"3m":0.06,"1y":0.03}.get(horizon,0.15)
        score = clamp(score*(1-pre_w) + prebreakout_score*pre_w - chase)
        # Microstructure acceleration gets meaningful weight only where minute/hour timing matters.
        accel_w={"1h":0.24,"4h":0.22,"12h":0.19,"24h":0.16,"3d":0.11,"1w":0.06,"1m":0.02,"3m":0.00,"1y":0.00}.get(horizon,0.05)
        score=clamp(score*(1-accel_w)+early_accel*accel_w)
        score=clamp(score*(1-social_w)+social_context*social_w)

        # Post-breakout continuation matters when phase has already transitioned.
        pb=sf(technical.get("post_breakout_score"),50)
        analog=sf((technical.get("historical_analog") or {}).get("score"),50)
        phase=technical.get("market_phase","")
        if phase in ("BREAKOUT / RETEST","CONTINUATION"):
            score=clamp(score*0.82 + pb*0.12 + analog*0.06)
        elif phase=="EXHAUSTION RISK":
            score=clamp(score-8)

        score = round(score,1)

        plan = trade_plan(
            a, technical, regime, rs_ref, relative_strength,
            prebreakout_score, entry_quality, chase
        )
        nintel = news_intelligence(catalyst)

        probability_estimate = model_probability_estimate(
            score,
            confidence,
            a["risk_score"],
            technical.get("technical_score",50),
            technical.get("agreement_score",50)
        )

        perf = performance_summary(horizon, plan.get("setup_type"))
        calibrated_probability = None
        if perf.get("samples",0) >= 20 and perf.get("win_rate") is not None:
            calibrated_probability = round(perf["win_rate"],1)

        moonshot = moonshot_profile(a, risk)

        row = {
            **a,
            "score":score,
            "confidence":confidence,
            "model_probability_estimate":probability_estimate,
            "calibrated_probability":calibrated_probability,
            "historical_setup_samples":perf.get("samples",0),
            "setup_type":plan.get("setup_type"),
            "action":plan.get("action"),
            "market_alignment":plan.get("market_alignment"),
            "market_alignment_score":plan.get("market_alignment_score"),
            "entry_low":plan.get("entry_low"),
            "entry_high":plan.get("entry_high"),
            "stop":plan.get("stop"),
            "target1":plan.get("target1"),
            "target2":plan.get("target2"),
            "risk_reward":plan.get("risk_reward"),
            "trigger":plan.get("trigger"),
            "do_not_trade_score":plan.get("do_not_trade_score"),
            "do_not_trade_reasons":plan.get("do_not_trade_reasons",[]),
            "news_intelligence":nintel,
            "prebreakout_score":prebreakout_score,
            "entry_quality":entry_quality,
            "chase_penalty":chase,
            "prebreakout_reasons":prebreakout_reasons,
            "relative_strength_score":relative_strength,
            "early_acceleration_score":early_accel,
            "early_acceleration_reasons":technical.get("early_acceleration_reasons",[]),
            "market_phase":technical.get("market_phase","unavailable"),
            "post_breakout_score":technical.get("post_breakout_score",50),
            "fakeout_score":technical.get("fakeout_score",50),
            "cvd_score":technical.get("cvd_score",50),
            "cvd_delta":technical.get("cvd_delta",0),
            "cvd_trend":technical.get("cvd_trend","unavailable"),
            "divergence_score":technical.get("divergence_score",50),
            "divergence_label":technical.get("divergence_label","none"),
            "anchored_vwap":technical.get("anchored_vwap",{}),
            "volume_profile":technical.get("volume_profile",{}),
            "historical_analog":technical.get("historical_analog",{}),
            "scenario":scenario_label(score,a["risk_score"]),
            "technical_score":technical["technical_score"],
            "technical_available":technical["available"],
            "live_pair":f"{a['symbol']}USDT" if technical["available"] else None,
            "trend":technical["trend"],
            "rsi":technical["rsi"],
            "spread_bps":technical["spread_bps"],
            "book_depth":technical["book_depth"],
            "book_imbalance":technical["book_imbalance"],
            "structure":technical.get("structure"),
            "support":technical.get("support"),
            "resistance":technical.get("resistance"),
            "atr_pct":technical.get("atr_pct"),
            "location_score":technical.get("location_score"),
            "breakout_score":technical.get("breakout_score"),
            "sweep_score":technical.get("sweep_score"),
            "squeeze_score":technical.get("squeeze_score"),
            "agreement_score":technical.get("agreement_score",50),
            "chart_signals":technical.get("chart_signals",[]),
            "timeframes":technical.get("timeframes",{}),
            "news_score":catalyst["news_score"],
            "catalyst_score":catalyst["catalyst_score"],
            "priced_in_penalty":catalyst["priced_in_penalty"],
            "headlines":catalyst["headlines"],
            "catalyst_titles":catalyst["catalyst_titles"],
            "moonshot":moonshot,
            "coinbase_available":(a["symbol"].upper() in coinbase_markets) if coinbase_ok else None,
            "coinbase_pairs":coinbase_markets.get(a["symbol"].upper(),[]) if coinbase_ok else [],
            "social_score":catalyst.get("social_score",50.0),"reddit_mentions":catalyst.get("reddit_mentions",0),"reddit_sentiment":catalyst.get("reddit_sentiment","unavailable"),"reddit_posts":catalyst.get("reddit_posts",[]),
            "why_now":[
                f"Momentum {a['momentum_score']:.0f}/100",
                f"Technical {technical['technical_score']:.0f}/100",
                f"Liquidity {a['liquidity_score']:.0f}/100",
                f"Catalyst {catalyst['catalyst_score']:.0f}/100",
                f"News {catalyst['news_score']:.0f}/100",
                f"Quality {a['fundamental_score']:.0f}/100",
                f"Risk {a['risk_score']:.0f}/100",
            ]
        }
        final.append(row)

    final.sort(key=lambda z:(z["score"],z["confidence"]), reverse=True)
    final = enrich_decision_rows(final)

    # Paper-validation loop: subsequent scans update old predictions, then this scan logs new ones.
    update_prediction_outcomes({str(r.get("symbol","")).upper():sf(r.get("price")) for r in final if r.get("price")})
    record_predictions(final, horizon)

    return {
        "updated":int(time.time()),
        "provider":provider,
        "warning":warning,
        "regime":regime,
        "fear_greed":{"value":fng_value,"label":fng_label},
        "results":final[:20],
        "diagnostics":{
            "raw_universe":len(rows),
            "prelim_candidates":len(prelim),
            "technical_scanned":len(tech_assets),
            "news_scanned":len(news_assets),
        }
    }

def cache_key(asset,horizon,risk):
    return f"{asset}:{horizon}:{risk}"

def get_scan(asset,horizon,risk,force=False):
    key = cache_key(asset,horizon,risk)

    with cache_lock:
        cached = cache.get(key)
        if cached and not force and time.time() - cached["updated"] < CACHE_SECONDS:
            return cached

    if asset == "stocks":
        if not finnhub_enabled():
            return {
                "updated":int(time.time()),
                "error":"Stock scanning is not enabled. Add FINNHUB_API_KEY in Render.",
                "results":[]
            }
        return {
            "updated":int(time.time()),
            "error":"Stock engine is intentionally disabled in v3 until a full stock-universe implementation is configured.",
            "results":[]
        }

    # "all" currently means crypto plus a clear stock status.
    # Serialize expensive cold scans so multiple phone refreshes cannot stampede APIs.
    with scan_compute_lock:
        with cache_lock:
            cached = cache.get(key)
            if cached and not force and time.time() - cached["updated"] < CACHE_SECONDS:
                return cached
        data = scan_crypto(horizon,risk)
    data["asset_type"] = asset
    data["horizon"] = horizon
    data["horizon_label"] = HORIZONS[horizon]["label"]
    data["risk"] = risk
    data["risk_label"] = RISK_MODES[risk]["label"]
    data["stocks_enabled"] = finnhub_enabled()

    with cache_lock:
        cache[key] = data

    return data

# ---------------------- API ----------------------

@app.route("/api/scan")
def api_scan():
    asset = request.args.get("asset","crypto")
    horizon = request.args.get("horizon","4h")
    risk = request.args.get("risk","aggressive")
    force = request.args.get("force","0") == "1"

    if asset not in ("crypto","stocks","all"):
        asset = "crypto"
    if horizon not in HORIZONS:
        horizon = "1w"
    if risk not in RISK_MODES:
        risk = "aggressive"

    try:
        data = get_scan(asset,horizon,risk,force)
        if data.get("error"):
            return jsonify(data), 200
        return jsonify(data)
    except Exception as e:
        return jsonify({
            "updated":int(time.time()),
            "error":str(e),
            "results":[]
        }), 500

@app.route("/health")
def health():
    return {
        "ok":True,
        "version":"13.0-trader-workbench",
        "stocks_enabled":finnhub_enabled()
    }

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name":"Market Opportunity Engine",
        "short_name":"Market Engine",
        "start_url":"/",
        "display":"standalone",
        "background_color":"#090c11",
        "theme_color":"#090c11",
        "icons":[]
    })

# ---------------------- UI ----------------------

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#071018">
<title>Crypto Radar Pro V13</title>
<style>
:root{color-scheme:dark;--bg:#071018;--panel:#0d1722;--line:#22344a;--text:#eef6ff;--muted:#8ea1b8;--green:#6ce6aa;--yellow:#ffd978;--red:#ff8b99}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% -5%,rgba(103,215,255,.14),transparent 28%),radial-gradient(circle at 85% 5%,rgba(154,133,255,.13),transparent 28%),var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,Inter,Arial,sans-serif}
.wrap{max-width:980px;margin:auto;padding:calc(env(safe-area-inset-top) + 16px) 14px 80px}
.brand{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.chip,.tag{font-size:10px;font-weight:850;padding:6px 9px;border-radius:999px;border:1px solid var(--line)}.v11{background:linear-gradient(135deg,#6a5cff,#9f78ff);border:0}.live{background:#0e241a;color:var(--green)}.locked{background:#0e1b2a;color:#a9d6ff}
h1{font-size:30px;letter-spacing:-.045em;margin:8px 0 5px}.subtitle{color:var(--muted);font-size:13px;line-height:1.45}.notice{margin:14px 0;padding:12px 13px;border-radius:15px;background:#0d1824;border:1px solid var(--line);font-size:12px;color:#b6c4d6;line-height:1.5}.notice b{color:white}
.controls{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:13px 0}select,button{width:100%;border-radius:13px;border:1px solid #2a3a50;background:#101a27;color:var(--text);padding:12px;font-size:14px}button{font-weight:900}#scan{grid-column:1/-1;background:linear-gradient(135deg,#eef7ff,#cde9ff);color:#08121d;border:0}
.meta{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 13px}.m{font-size:10px;color:#98a9bd;padding:7px 9px;border:1px solid var(--line);background:#0c1620;border-radius:999px}.warn{border:1px solid #65383e;background:#2b171b;color:#ffc2ca;border-radius:13px;padding:11px;margin:9px 0;font-size:12px}.diag{color:#6f829a;font-size:10px;margin:8px 2px 14px}
.section{font-size:10px;color:#72849a;text-transform:uppercase;letter-spacing:.14em;margin:18px 2px 8px}.focusWrap{display:none}.focusWrap.show{display:block}.focus,.card{background:linear-gradient(180deg,#101c29,#0b151f);border:1px solid #1e3044;border-radius:18px;padding:14px}.focus{border-color:#405a7b}.cards{display:grid;gap:11px}
.head{display:flex;justify-content:space-between;gap:12px}.name{font-size:21px;font-weight:950}.sym,.rank{font-size:10px;color:#8192a8;text-transform:uppercase}.score{font-size:24px;font-weight:950;text-align:right}.score small{display:block;font-size:8px;color:#708197}.price{font-size:18px;font-weight:900;margin:9px 0}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.tag{background:#121e2b;color:#afbdd0}.good{background:#10261c;border-color:#275d44;color:#82eab5}.bad{background:#2a171b;border-color:#60343a;color:#ffb1bb}.mid{background:#2c2618;border-color:#62552e;color:#ffe39a}.cb{font-weight:950}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:10px 0}.metric{background:#0d1722;border:1px solid #1d2d40;border-radius:11px;padding:9px;color:#8192a8;font-size:10px}.metric b{display:block;color:#f2f7fd;font-size:14px;margin-top:3px}
.callout,.reason{margin-top:10px;padding:10px 11px;border-radius:12px;font-size:11px;line-height:1.5}.callout.good{background:#0f241a;border:1px solid #265b43;color:#a5efc8}.callout.bad{background:#2a171b;border:1px solid #60343a;color:#ffc0c8}.callout.mid{background:#2b2517;border:1px solid #61542d;color:#ffe4a6}.reason{background:#09121b;border:1px solid #1b2a3b;color:#b7c5d6}
.actions{display:flex;gap:7px;margin-top:10px}.actions button{padding:9px;background:#172537}.actions .focusBtn{background:#dceeff;color:#07111c;border:0}details{margin-top:11px;padding-top:9px;border-top:1px solid #1e2d40}summary{cursor:pointer;color:#9cb0c8;font-size:11px;font-weight:900}.news{margin-top:10px}.news div{font-size:11px;color:#b4c1d2;line-height:1.45;margin:4px 0}.empty{text-align:center;color:#8293a8;padding:40px 10px}.footer{margin-top:22px;border-top:1px solid #182737;padding-top:12px;color:#607389;font-size:10px;line-height:1.5}.live-state{font-size:9px;color:var(--green);margin-left:6px}.dot{display:inline-block;width:6px;height:6px;background:var(--green);border-radius:50%;margin-right:4px}
@media(max-width:700px){.controls{grid-template-columns:1fr 1fr}.controls select:first-child{grid-column:1/-1}.grid{grid-template-columns:repeat(2,1fr)}}

.plan{display:grid;grid-template-columns:repeat(2,1fr);gap:7px;margin:10px 0}
.planbox{background:#0a141e;border:1px solid #203248;border-radius:11px;padding:9px;font-size:10px;color:#8092a8}
.planbox b{display:block;color:#f0f6fd;font-size:13px;margin-top:3px}
.action{font-size:11px;font-weight:950;letter-spacing:.05em}
.action.ready{color:#78e8ad}.action.wait{color:#ffe094}.action.avoid{color:#ff9daa}

</style>
</head>
<body>
<div class="wrap">
<div class="brand"><span class="chip v11">V13</span><span class="chip live">● LIVE PRICES</span><span class="chip locked">🔒 MANUAL RANKINGS</span></div>
<h1>Crypto Radar Pro</h1>
<div class="subtitle">Day-trading radar with stable cards, live prices, setup classification, entry/stop/targets, Coinbase availability, news and social context.</div>
<div class="notice"><b>No automatic reshuffling in V13.</b> Prices stay live, but the ranking only changes when you press <b>Refresh Rankings</b>. Tap <b>Focus</b> and the coin stays open while you read.</div>

<div class="controls">
<select id="asset"><option value="crypto" selected>Crypto</option><option value="all">Crypto + Stock Status</option><option value="stocks">Stocks</option></select>
<select id="horizon"><option value="1h">Next 1 Hour · Scalping</option><option value="4h" selected>Next 4 Hours · Day Trade</option><option value="12h">Next 12 Hours</option><option value="24h">Next 24 Hours</option><option value="3d">Next 3 Days</option><option value="1w">Next Week</option></select>
<select id="risk"><option value="conservative">Conservative</option><option value="moderate">Moderate</option><option value="aggressive" selected>Aggressive</option><option value="extreme">Extreme / Moonshot</option></select>
<button id="scan">REFRESH RANKINGS</button>
</div>

<div class="meta"><div class="m" id="updated">Updated —</div><div class="m" id="provider">Data —</div><div class="m" id="regime">Market —</div><div class="m" id="fng">Fear & Greed —</div></div>
<div id="warning"></div><div id="diagnostics"></div>

<div id="focusWrap" class="focusWrap"><div class="section">Focus Mode · stays open</div><div id="focusCard" class="focus"></div></div>
<div class="section">Radar Rankings</div><div id="results" class="cards"><div class="empty">Analyzing market…</div></div>
<div class="footer">Research tool only. Rankings are not guaranteed returns or calibrated probabilities. Coinbase status is based on active Coinbase Exchange USD/USDC/USDT products and may differ by jurisdiction/account. V13 paper-logs setups and begins showing historical setup win rate after at least 20 resolved samples. On Render's default ephemeral filesystem that history resets when the instance is replaced unless PREDICTION_DB_PATH points to persistent storage.</div>
</div>

<script>
const money=x=>{if(x===null||x===undefined||!Number.isFinite(Number(x)))return '—';x=Number(x);if(x>=1e12)return '$'+(x/1e12).toFixed(2)+'T';if(x>=1e9)return '$'+(x/1e9).toFixed(2)+'B';if(x>=1e6)return '$'+(x/1e6).toFixed(2)+'M';if(x>=1000)return '$'+x.toLocaleString(undefined,{maximumFractionDigits:2});if(x>=1)return '$'+x.toFixed(4);if(x>=.01)return '$'+x.toFixed(5);return '$'+x.toFixed(8)}
const pct=x=>(Number(x)>=0?'+':'')+Number(x||0).toFixed(2)+'%';
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
const state=c=>{if((c.entry_quality??50)<40||(c.chase_penalty??0)>=15)return['🔴 CHASE RISK','bad'];if((c.prebreakout_score??50)>=70&&(c.entry_quality??50)>=58)return['🟢 EARLY SETUP','good'];if((c.post_breakout_score??50)>=68)return['🟢 BREAKOUT / RETEST','good'];return['🟡 WAIT / CONFIRM','mid']}
const coinbase=c=>c.coinbase_available===true?'<span class="tag good cb">Coinbase ✓ Available</span>':c.coinbase_available===false?'<span class="tag bad cb">Coinbase ✕ Not detected</span>':'<span class="tag mid cb">Coinbase ? Check unavailable</span>';
let data=null,focused=null,focusSnapshot=null,socket=null,timer=null,map={};
function stopLive(){if(timer){clearTimeout(timer);timer=null}if(socket){try{socket.onclose=null;socket.close()}catch(_){}socket=null}map={}}
function startLive(results){stopLive();const list=(results||[]).map((c,i)=>({c,i})).filter(x=>x.c.live_pair).slice(0,20);if(!list.length)return;const streams=[];list.forEach(({c,i})=>{const p=c.live_pair.toUpperCase();map[p]={c,i};streams.push(p.toLowerCase()+'@ticker');streams.push(p.toLowerCase()+'@bookTicker')});try{socket=new WebSocket('wss://stream.binance.us:9443/stream?streams='+streams.join('/'))}catch(_){return}socket.onopen=()=>list.forEach(({i})=>{const e=document.getElementById('live-'+i);if(e)e.innerHTML='<span class="dot"></span>LIVE'});socket.onmessage=ev=>{let m;try{m=JSON.parse(ev.data)}catch(_){return};const d=m.data||m,x=map[d.s];if(!x)return;if(d.c!==undefined){const p=Number(d.c),e=document.getElementById('price-'+x.i);if(p>0&&e)e.textContent=money(p);if(focused===String(x.c.symbol).toUpperCase()){const f=document.getElementById('focusPrice');if(f&&p>0)f.textContent=money(p)}}if(d.b!==undefined&&d.a!==undefined){const b=Number(d.b),a=Number(d.a),e=document.getElementById('spread-'+x.i);if(b>0&&a>0&&e)e.textContent='Spread '+((a-b)/((a+b)/2)*10000).toFixed(2)+' bp'}};socket.onclose=()=>{timer=setTimeout(()=>startLive(data?.results||[]),5000)}}
function renderFocus(c){if(!c)return;const[s,cls]=state(c),tf=c.timeframes||{},tfText=['1m','5m','10m','15m','1h','4h'].filter(k=>tf[k]).map(k=>`${k}: ${tf[k].score}/100 · ${tf[k].trend}`).join('<br>');const actionClass=c.action==='SETUP READY'?'ready':c.action==='AVOID'?'avoid':'wait';document.getElementById('focusCard').innerHTML=`<div class="head"><div><div class="name">${esc(c.name)}</div><div class="sym">${esc(c.symbol)} · ${esc(c.setup_type||c.market_phase||'—')}</div></div><button style="width:auto" onclick="closeFocus()">Close</button></div><div id="focusPrice" class="price" style="font-size:28px">${money(c.price)}</div><div class="tags"><span class="tag ${cls}">${s}</span>${coinbase(c)}<span class="tag">BTC/ETH ${esc(c.market_alignment||'—')}</span><span class="tag">Do-not-trade ${c.do_not_trade_score??'—'}</span></div><div class="action ${actionClass}">${esc(c.action||'WATCH')}</div><div class="plan"><div class="planbox">Entry zone<b>${money(c.entry_low)} – ${money(c.entry_high)}</b></div><div class="planbox">Invalidation / stop<b>${money(c.stop)}</b></div><div class="planbox">Target 1<b>${money(c.target1)}</b></div><div class="planbox">Target 2<b>${money(c.target2)}</b></div><div class="planbox">Risk / reward<b>${c.risk_reward??'—'} : 1</b></div><div class="planbox">Setup history<b>${c.calibrated_probability!==null&&c.calibrated_probability!==undefined?c.calibrated_probability+'% wins ('+c.historical_setup_samples+')':'Collecting data'}</b></div></div><div class="reason"><b>Wait for this</b><br>${esc(c.trigger||'Wait for confirmation.')}</div><div class="grid"><div class="metric">Radar<b>${c.score}</b></div><div class="metric">Entry<b>${c.entry_quality}</b></div><div class="metric">Pre-breakout<b>${c.prebreakout_score}</b></div><div class="metric">Technical<b>${c.technical_score}</b></div><div class="metric">News<b>${c.news_score}</b></div><div class="metric">Social<b>${c.social_score??'—'}</b></div><div class="metric">Confirmation<b>${c.confirmation_quality??'—'}</b></div><div class="metric">Disagreement<b>${c.signal_disagreement??'—'}</b></div></div><div class="reason"><b>Risk budget</b><br>${esc(c.risk_budget_hint||'—')}</div>${(c.do_not_trade_reasons||[]).length?`<div class="reason"><b>Why you might skip it</b><br>${c.do_not_trade_reasons.map(x=>'• '+esc(x)).join('<br>')}</div>`:''}<div class="reason"><b>What the radar sees</b><br>${(c.prebreakout_reasons||c.why_now||[]).slice(0,6).map(x=>'• '+esc(x)).join('<br>')||'No strong explanation available.'}</div>${tfText?`<div class="reason"><b>Short-timeframe alignment</b><br>${tfText}</div>`:''}`}\nfunction focusIndex(i){const c=data?.results?.[i];if(!c)return;focused=String(c.symbol).toUpperCase();focusSnapshot=JSON.parse(JSON.stringify(c));renderFocus(c);document.getElementById('focusWrap').classList.add('show');document.getElementById('focusWrap').scrollIntoView({behavior:'smooth',block:'start'})}
function closeFocus(){focused=null;focusSnapshot=null;document.getElementById('focusWrap').classList.remove('show')}
function card(c,i){const[s,cls]=state(c),reasons=(c.prebreakout_reasons||c.why_now||[]).slice(0,3),actionClass=c.action==='SETUP READY'?'ready':c.action==='AVOID'?'avoid':'wait';return `<div class="card"><div class="head"><div><div class="rank">#${i+1} · ${esc(c.setup_type||c.scenario)}</div><div class="name">${esc(c.name)}</div><div class="sym">${esc(c.symbol)}</div></div><div class="score">${c.score}<small>RADAR SCORE</small></div></div><div class="price"><span id="price-${i}">${money(c.price)}</span> <span style="font-size:11px;color:${Number(c.change_24h)>=0?'#6ce6aa':'#ff8b99'}">${pct(c.change_24h)} 24h</span>${c.live_pair?`<span id="live-${i}" class="live-state"><span class="dot"></span>connecting</span>`:''}</div><div class="tags"><span class="tag ${cls}">${s}</span>${coinbase(c)}<span class="tag">BTC/ETH ${esc(c.market_alignment||'—')}</span><span class="tag">No-trade ${c.do_not_trade_score??'—'}/100</span>${c.live_pair?`<span id="spread-${i}" class="tag">Spread —</span>`:''}</div><div class="action ${actionClass}">${esc(c.action||'WATCH')}</div><div class="plan"><div class="planbox">Entry<b>${money(c.entry_low)} – ${money(c.entry_high)}</b></div><div class="planbox">Stop / invalidation<b>${money(c.stop)}</b></div><div class="planbox">T1 / T2<b>${money(c.target1)} / ${money(c.target2)}</b></div><div class="planbox">Risk / reward<b>${c.risk_reward??'—'} : 1</b></div></div><div class="callout ${cls}"><b>Trigger:</b> ${esc(c.trigger||'Wait for confirmation.')}</div><div class="grid"><div class="metric">Entry quality<b>${c.entry_quality}</b></div><div class="metric">Pre-breakout<b>${c.prebreakout_score}</b></div><div class="metric">Momentum<b>${c.momentum_score}</b></div><div class="metric">Technical<b>${c.technical_score}</b></div><div class="metric">News<b>${c.news_score}</b></div><div class="metric">Chase<b>${c.chase_penalty}</b></div><div class="metric">Confirm<b>${c.confirmation_quality??'—'}</b></div><div class="metric">Conflict<b>${c.signal_disagreement??'—'}</b></div></div><div class="callout"><b>${esc(c.decision_summary?.verdict||'Decision')}</b><br>${(c.decision_summary?.best_reasons||[]).map(x=>'✓ '+esc(x)).join(' · ')||'No strong confirmations yet.'}<br>${(c.decision_summary?.main_risks||[]).map(x=>'⚠ '+esc(x)).join(' · ')}</div><div class="reason"><b>Risk budget:</b> ${esc(c.risk_budget_hint||'—')}</div><div class="actions"><button class="focusBtn" onclick="focusIndex(${i})">◎ FOCUS</button></div><details><summary>Advanced Analysis</summary><div class="reason"><b>News intelligence</b><br>Event: ${esc(c.news_intelligence?.event_type||'—')} · credibility ${c.news_intelligence?.credibility_score??'—'}/100 · novelty ${c.news_intelligence?.novelty_score??'—'}/100 · ${c.news_intelligence?.source_count??0} source(s)</div>${(c.do_not_trade_reasons||[]).length?`<div class="reason"><b>Do-not-trade flags</b><br>${c.do_not_trade_reasons.map(x=>'• '+esc(x)).join('<br>')}</div>`:''}<div class="reason"><b>Structure</b><br>${esc(c.structure||'—')}<br>Support ${money(c.support)} · Resistance ${money(c.resistance)}<br>${(c.chart_signals||[]).slice(0,8).map(x=>'• '+esc(x)).join('<br>')}</div>${(c.headlines||[]).length?`<div class="news">${(c.headlines||[]).slice(0,5).map(h=>`<div>• ${esc(h.title)}</div>`).join('')}</div>`:''}${(c.reddit_posts||[]).length?`<div class="news">${(c.reddit_posts||[]).slice(0,4).map(h=>`<div>• ${esc(h.title)}</div>`).join('')}</div>`:''}</details></div>`}\nfunction render(d){data=d;document.getElementById('updated').textContent='Updated '+new Date(d.updated*1000).toLocaleTimeString();document.getElementById('provider').textContent='Data '+(d.provider||'—');document.getElementById('regime').textContent='Market '+(d.regime?.label||'—');document.getElementById('fng').textContent='Fear & Greed '+(d.fear_greed?.value??'—')+' · '+(d.fear_greed?.label||'—');let w=[];if(d.warning)w.push(d.warning);document.getElementById('warning').innerHTML=w.map(x=>'<div class="warn">'+esc(x)+'</div>').join('');if(d.diagnostics)document.getElementById('diagnostics').innerHTML=`<div class="diag">Universe ${d.diagnostics.raw_universe||0} · candidates ${d.diagnostics.prelim_candidates||0} · technical ${d.diagnostics.technical_scanned||0} · news/social ${d.diagnostics.news_scanned||0}</div>`;const r=d.results||[];document.getElementById('results').innerHTML=r.length?r.map(card).join(''):'<div class="empty">No coins passed these filters.</div>';startLive(r);if(focused){const fresh=r.find(x=>String(x.symbol).toUpperCase()===focused);renderFocus(fresh||focusSnapshot)}}
async function scan(force=true){const b=document.getElementById('scan');b.disabled=true;b.textContent='SCANNING…';if(!data)document.getElementById('results').innerHTML='<div class="empty">Analyzing market…</div>';try{const a=document.getElementById('asset').value,h=document.getElementById('horizon').value,r=document.getElementById('risk').value,res=await fetch(`/api/scan?asset=${a}&horizon=${h}&risk=${r}&force=${force?1:0}`),d=await res.json();if(d.error)throw new Error(d.error);render(d)}catch(e){document.getElementById('warning').innerHTML='<div class="warn">'+esc(e.message)+'</div>'}b.disabled=false;b.textContent='REFRESH RANKINGS'}
document.getElementById('scan').onclick=()=>scan(true);['asset','horizon','risk'].forEach(id=>document.getElementById(id).onchange=()=>scan(true));document.addEventListener('visibilitychange',()=>{if(document.hidden)stopLive();else if(data)startLive(data.results||[])});scan(false);
// V13 intentionally has NO setInterval scan. Rankings never change unless you request it.
</script>
</body>
</html>"""


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok":True,
        "version":"13.0-trader-workbench",
        "auto_ranking_refresh":False,
        "paper_validation":True,
        "features":["coinbase-status","focus-mode","trade-plan","do-not-trade","setup-validation","decision-quality"]
    })

@app.route("/api/performance")
def api_performance():
    horizon=request.args.get("horizon")
    setup=request.args.get("setup_type")
    return jsonify(performance_summary(horizon or None, setup or None))

@app.route("/version")
def version():
    return {"version":"13.0-trader-workbench","auto_ranking_refresh":False}

@app.route("/")
def index():
    return render_template_string(PAGE)

if __name__ == "__main__":
    port = env_int("PORT", 10000, 1, 65535)
    app.run(host="0.0.0.0", port=port)
