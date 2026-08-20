from flask import Flask, jsonify, render_template_string, request
import requests
import math
import time
import os
import re
import html
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

app = Flask(__name__)

# ============================================================
# MARKET OPPORTUNITY ENGINE v3
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

CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "600"))
MAX_CRYPTO_UNIVERSE = int(os.environ.get("MAX_CRYPTO_UNIVERSE", "1000"))
PRELIM_LIMIT = int(os.environ.get("PRELIM_LIMIT", "80"))
TECH_LIMIT = int(os.environ.get("TECH_LIMIT", "40"))
NEWS_LIMIT = int(os.environ.get("NEWS_LIMIT", "25"))

session = requests.Session()
session.headers.update({
    "User-Agent": "MarketOpportunityEngine/3.0 research-only",
    "Accept": "application/json,text/plain,*/*"
})

cache = {}
cache_lock = threading.Lock()

# ---------------------- CONFIG ----------------------

HORIZONS = {
    "24h": {
        "label": "Next 24 Hours",
        "momentum": 1.45,
        "technical": 1.35,
        "liquidity": 1.00,
        "news": 1.35,
        "catalyst": 0.85,
        "fundamental": 0.30,
        "regime": 1.00,
    },
    "3d": {
        "label": "Next 3 Days",
        "momentum": 1.35,
        "technical": 1.25,
        "liquidity": 0.95,
        "news": 1.30,
        "catalyst": 1.05,
        "fundamental": 0.40,
        "regime": 0.95,
    },
    "1w": {
        "label": "Next Week",
        "momentum": 1.15,
        "technical": 1.10,
        "liquidity": 0.90,
        "news": 1.15,
        "catalyst": 1.25,
        "fundamental": 0.55,
        "regime": 0.85,
    },
    "1m": {
        "label": "Next Month",
        "momentum": 0.90,
        "technical": 0.85,
        "liquidity": 0.80,
        "news": 1.00,
        "catalyst": 1.45,
        "fundamental": 0.80,
        "regime": 0.70,
    },
    "3m": {
        "label": "Next 3 Months",
        "momentum": 0.65,
        "technical": 0.65,
        "liquidity": 0.70,
        "news": 0.75,
        "catalyst": 1.25,
        "fundamental": 1.15,
        "regime": 0.55,
    },
    "1y": {
        "label": "1 Year+",
        "momentum": 0.35,
        "technical": 0.35,
        "liquidity": 0.65,
        "news": 0.45,
        "catalyst": 0.75,
        "fundamental": 1.60,
        "regime": 0.35,
    },
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

def technical_for_asset(asset, available_symbols):
    pair = f"{asset['symbol']}USDT"
    if pair not in available_symbols:
        return {
            "available":False,
            "technical_score":50.0,
            "spread_bps":None,
            "book_depth":None,
            "book_imbalance":None,
            "rsi":None,
            "trend":"unavailable",
        }

    try:
        raw = req_json(
            f"{BINANCE_US}/api/v3/klines",
            {"symbol":pair,"interval":"15m","limit":120},
            timeout=12,
            retries=2
        )
        closes = [sf(r[4]) for r in raw]
        quote_vol = [sf(r[7]) for r in raw]
        if len(closes) < 55:
            raise RuntimeError("insufficient candles")

        e20 = ema(closes,20)[-1]
        e50 = ema(closes,50)[-1]
        rr = rsi(closes,14)
        p = closes[-1]
        vol_now = quote_vol[-1]
        vol_avg = mean(quote_vol[-21:-1],1)
        vol_ratio = safe_div(vol_now,vol_avg,1)

        trend_score = 50
        trend = "mixed"
        if p > e20 > e50:
            trend_score = 78
            trend = "bullish"
        elif p < e20 < e50:
            trend_score = 25
            trend = "bearish"
        elif p > e20:
            trend_score = 62
            trend = "mild bullish"

        rsi_score = 50
        if 52 <= rr <= 68:
            rsi_score = 72
        elif rr > 80:
            rsi_score = 28
        elif rr < 30:
            rsi_score = 42

        volume_score = clamp(45 + (vol_ratio - 1) * 25)

        book = req_json(
            f"{BINANCE_US}/api/v3/depth",
            {"symbol":pair,"limit":100},
            timeout=10,
            retries=2
        )
        bids = [(sf(x[0]),sf(x[1])) for x in book.get("bids",[])]
        asks = [(sf(x[0]),sf(x[1])) for x in book.get("asks",[])]

        spread_bps = None
        depth = None
        imbalance = None
        book_score = 50

        if bids and asks:
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2
            spread_bps = safe_div(best_ask-best_bid,mid,0) * 10000

            lo = mid * 0.995
            hi = mid * 1.005
            bd = sum(px*q for px,q in bids if px >= lo)
            ad = sum(px*q for px,q in asks if px <= hi)
            depth = bd + ad
            imbalance = safe_div(bd-ad,depth,0)

            book_score = 55
            if spread_bps <= 3:
                book_score += 10
            elif spread_bps > 15:
                book_score -= 18

            if depth >= 1_000_000:
                book_score += 8
            elif depth < 100_000:
                book_score -= 10

            if imbalance >= 0.12:
                book_score += 7
            elif imbalance <= -0.18:
                book_score -= 7

        technical_score = clamp(
            trend_score*0.42 +
            rsi_score*0.20 +
            volume_score*0.18 +
            book_score*0.20
        )

        return {
            "available":True,
            "technical_score":round(technical_score,1),
            "spread_bps":round(spread_bps,2) if spread_bps is not None else None,
            "book_depth":round(depth,2) if depth is not None else None,
            "book_imbalance":round(imbalance,3) if imbalance is not None else None,
            "rsi":round(rr,1),
            "trend":trend,
        }
    except Exception:
        return {
            "available":False,
            "technical_score":50.0,
            "spread_bps":None,
            "book_depth":None,
            "book_imbalance":None,
            "rsi":None,
            "trend":"error",
        }

# ---------------------- NEWS / CATALYST ----------------------

def catalyst_for_asset(asset, horizon):
    days = {
        "24h":3, "3d":7, "1w":14, "1m":45, "3m":120, "1y":365
    }[horizon]

    # Name + symbol reduces ticker false positives.
    headlines = google_news(f'"{asset["name"]}" {asset["symbol"]}', days, limit=10)

    pos = 0
    neg = 0
    future = 0
    catalyst_titles = []

    for h in headlines:
        s, f = headline_signal(h["title"])
        if s > 0:
            pos += s
        elif s < 0:
            neg += abs(s)
        future += f
        if s != 0 or f > 0:
            catalyst_titles.append(h["title"])

    news_score = clamp(50 + pos*6 - neg*8)
    catalyst_score = clamp(40 + future*7 + pos*2 - neg*3)

    # "Priced in" heuristic:
    # large recent run + positive catalyst news = reduced incremental upside.
    recent_run = max(abs(asset["change_24h"]), abs(asset["change_7d"]) / 2)
    priced_in = 0
    if catalyst_score >= 60:
        if recent_run > 30:
            priced_in = 14
        elif recent_run > 18:
            priced_in = 9
        elif recent_run > 10:
            priced_in = 5

    return {
        "news_score":round(news_score,1),
        "catalyst_score":round(catalyst_score,1),
        "priced_in_penalty":priced_in,
        "headlines":headlines[:5],
        "catalyst_titles":catalyst_titles[:4],
        "news_mentions":len(headlines),
    }

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
            ex.submit(technical_for_asset, a, available_symbols): a["id"]
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
        })

        score, confidence = final_score(
            a, technical, catalyst, regime, fng_value, horizon, risk
        )

        moonshot = moonshot_profile(a, risk)

        row = {
            **a,
            "score":score,
            "confidence":confidence,
            "scenario":scenario_label(score,a["risk_score"]),
            "technical_score":technical["technical_score"],
            "technical_available":technical["available"],
            "trend":technical["trend"],
            "rsi":technical["rsi"],
            "spread_bps":technical["spread_bps"],
            "book_depth":technical["book_depth"],
            "book_imbalance":technical["book_imbalance"],
            "news_score":catalyst["news_score"],
            "catalyst_score":catalyst["catalyst_score"],
            "priced_in_penalty":catalyst["priced_in_penalty"],
            "headlines":catalyst["headlines"],
            "catalyst_titles":catalyst["catalyst_titles"],
            "moonshot":moonshot,
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
    horizon = request.args.get("horizon","1w")
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
        "version":"3.0",
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
<meta name="theme-color" content="#090c11">
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Market Opportunity Engine</title>

<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{
 margin:0;background:#090c11;color:#eef2f7;
 font-family:-apple-system,BlinkMacSystemFont,Inter,Arial,sans-serif
}
.wrap{
 max-width:1100px;margin:auto;
 padding:calc(env(safe-area-inset-top) + 18px) 14px 50px
}
h1{font-size:27px;margin:0 0 4px}
.sub{font-size:13px;color:#8893a4;line-height:1.4}
.controls{
 display:grid;grid-template-columns:repeat(3,1fr);
 gap:8px;margin:16px 0
}
select,button{
 width:100%;border:1px solid #273042;background:#141923;
 color:#eef2f7;border-radius:12px;padding:12px;font-size:14px
}
button{
 grid-column:1/-1;background:#eef2f7;color:#101216;
 font-weight:800;border:none
}
.meta{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0 14px}
.pill{
 background:#141923;border:1px solid #242c3a;
 border-radius:10px;padding:8px 10px;font-size:11px;color:#a7b0bf
}
.card{
 background:#11161f;border:1px solid #222a39;
 border-radius:17px;padding:14px;margin:10px 0
}
.topcard{border-width:2px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.rank{font-size:12px;color:#8691a2}
.name{font-size:22px;font-weight:850}
.sym{font-size:13px;color:#9aa5b6}
.score{font-size:22px;font-weight:850;text-align:right}
.conf{font-size:11px;color:#8f99aa;text-align:right}
.price{font-size:18px;font-weight:700;margin:9px 0}
.grid{
 display:grid;grid-template-columns:repeat(4,1fr);
 gap:7px;margin:10px 0
}
.metric{
 background:#181e29;border-radius:10px;padding:8px;
 font-size:10px;color:#8f99aa
}
.metric b{display:block;color:#eef2f7;font-size:13px;margin-top:3px}
.why{font-size:12px;line-height:1.5;color:#c4cbd5}
.badge{
 display:inline-block;background:#1c2330;border-radius:8px;
 padding:5px 7px;font-size:11px;margin:4px 4px 0 0
}
.upside{
 background:#0d1117;border-radius:10px;padding:9px;
 font-size:12px;line-height:1.45;margin-top:9px
}
.headlines{font-size:11px;color:#97a1b0;line-height:1.45;margin-top:8px}
.headlines div{margin-top:4px}
.note{font-size:11px;color:#778294;line-height:1.45;margin-top:16px}
.warn{
 background:#261b1c;border:1px solid #493033;
 padding:10px;border-radius:12px;margin-bottom:12px;font-size:12px
}
.empty{text-align:center;padding:35px;color:#8f99aa}
.diag{font-size:11px;color:#758092;margin:10px 0}

@media(max-width:560px){
 .controls{grid-template-columns:1fr}
 button{grid-column:auto}
 .grid{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>

<body>
<div class="wrap">
<h1>Market Opportunity Engine</h1>
<div class="sub">
Top 20 crypto opportunities ranked using market action, liquidity,
technical structure, catalysts, news, risk and the broader crypto regime.
</div>

<div class="controls">
<select id="asset">
<option value="crypto" selected>Crypto</option>
<option value="all">Crypto + Stock Status</option>
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
<div class="pill" id="provider">Provider —</div>
<div class="pill" id="regime">Regime —</div>
<div class="pill" id="fng">Fear & Greed —</div>
</div>

<div id="warning"></div>
<div id="diagnostics"></div>
<div id="results"><div class="empty">Loading market…</div></div>

<div class="note">
Scores are research rankings, not guaranteed returns and not calibrated probabilities.
Confidence measures data completeness, not chance of profit.
Extreme / Moonshot mode intentionally accepts much higher failure and drawdown risk.
A coin described as having conceivable 5–10× upside can also lose nearly all of its value.
</div>
</div>

<script>
const money=x=>{
 if(x===null || x===undefined)return '—';
 if(x>=1e12)return '$'+(x/1e12).toFixed(2)+'T';
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

 btn.disabled=true;
 btn.textContent='SCANNING…';
 document.getElementById('results').innerHTML='<div class="empty">Analyzing market…</div>';

 try{
   const r=await fetch(`/api/scan?asset=${asset}&horizon=${horizon}&risk=${risk}&force=${force?1:0}`);
   const text=await r.text();

   let d;
   try{d=JSON.parse(text)}
   catch(_){throw new Error('Server returned invalid JSON')}

   document.getElementById('updated').textContent='Updated '+new Date(d.updated*1000).toLocaleTimeString();

   if(d.error){
     document.getElementById('results').innerHTML='<div class="warn">'+d.error+'</div>';
     document.getElementById('warning').innerHTML='';
     return;
   }

   document.getElementById('provider').textContent='Data: '+(d.provider||'—');
   document.getElementById('regime').textContent='Market: '+(d.regime?.label||'—');
   document.getElementById('fng').textContent='Fear & Greed: '+(d.fear_greed?.value??'—')+' · '+(d.fear_greed?.label||'—');

   let warnings=[];
   if(d.warning)warnings.push(d.warning);
   if((asset==='stocks'||asset==='all') && !d.stocks_enabled){
     warnings.push('Stocks are not enabled yet. Add FINNHUB_API_KEY in Render when you want the stock engine added.');
   }
   document.getElementById('warning').innerHTML=warnings.map(x=>'<div class="warn">'+x+'</div>').join('');

   if(d.diagnostics){
     document.getElementById('diagnostics').innerHTML=
       `<div class="diag">Universe ${d.diagnostics.raw_universe||0} · preliminary ${d.diagnostics.prelim_candidates||0} · technical ${d.diagnostics.technical_scanned||0} · news ${d.diagnostics.news_scanned||0}</div>`;
   }

   if(!d.results || !d.results.length){
     document.getElementById('results').innerHTML='<div class="empty">No coins passed these filters. Try a different risk mode or horizon.</div>';
   }else{
     document.getElementById('results').innerHTML=d.results.map((c,i)=>`
       <div class="card ${i===0?'topcard':''}">
         <div class="row">
           <div>
             <div class="rank">#${i+1} · ${c.scenario}</div>
             <div class="name">${c.name}</div>
             <div class="sym">${c.symbol}</div>
           </div>
           <div>
             <div class="score">${c.score}/100</div>
             <div class="conf">data confidence ${c.confidence}</div>
           </div>
         </div>

         <div class="price">${money(c.price)} · ${pct(c.change_24h)} 24h</div>

         <div class="grid">
           <div class="metric">Momentum<b>${c.momentum_score}</b></div>
           <div class="metric">Technical<b>${c.technical_score}</b></div>
           <div class="metric">Liquidity<b>${c.liquidity_score}</b></div>
           <div class="metric">Catalyst<b>${c.catalyst_score}</b></div>
           <div class="metric">News<b>${c.news_score}</b></div>
           <div class="metric">Quality<b>${c.fundamental_score}</b></div>
           <div class="metric">Risk<b>${c.risk_score}</b></div>
           <div class="metric">Market cap<b>${money(c.market_cap)}</b></div>
         </div>

         <div>
           <span class="badge">Trend: ${c.trend}</span>
           ${c.rsi!==null ? `<span class="badge">RSI ${c.rsi}</span>`:''}
           ${c.spread_bps!==null ? `<span class="badge">Spread ${c.spread_bps} bp</span>`:''}
           ${c.book_imbalance!==null ? `<span class="badge">Book ${c.book_imbalance>=0?'+':''}${c.book_imbalance}</span>`:''}
         </div>

         ${c.moonshot ? `
           <div class="upside"><b>${c.moonshot.label}:</b> ${c.moonshot.upside}<br>
           <b>Warning:</b> ${c.moonshot.warning}</div>
         `:''}

         <div class="why"><b>Why now:</b> ${c.why_now.join(' · ')}</div>

         ${c.catalyst_titles && c.catalyst_titles.length ? `
           <div class="headlines"><b>Potential catalyst headlines:</b>
           ${c.catalyst_titles.map(h=>`<div>• ${h}</div>`).join('')}
           </div>
         `:''}
       </div>
     `).join('');
   }

 }catch(e){
   document.getElementById('results').innerHTML='<div class="warn">Scan failed: '+e.message+'</div>';
 }

 btn.disabled=false;
 btn.textContent='SCAN / REFRESH';
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
