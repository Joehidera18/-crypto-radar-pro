# Crypto Radar Pro

A read-only crypto day-trading research scanner.

## What it analyzes

- 1m, 5m, 15m and 1h price structure
- EMA 20/50/200
- RSI
- MACD momentum
- rolling VWAP
- ATR / volatility
- spot volume and taker-buy share
- order-book bid/ask spread
- ±0.5% order-book depth
- bid/ask imbalance
- simulated $1,000 market-order slippage
- perpetual-futures funding
- open-interest change
- futures taker buy/sell ratio
- BTC risk-on / risk-off regime
- Crypto Fear & Greed
- recent crypto RSS headlines
- simple per-coin news sentiment
- hard liquidity, spread, volatility and overextension filters
- research entry/invalidation/target levels
- signal history CSV
- paper-trading tracker

## Safety design

This program **does not have code to place orders**. A score is a heuristic ranking,
not the probability a trade will win. Run it in paper mode long enough to measure
its real results after fees/slippage before considering live trading.

## Install

Python 3.10+ recommended.

```bash
pip install -r requirements.txt
```

## Run one scan

```bash
python crypto_radar_pro.py --once
```

## Run continuously

```bash
python crypto_radar_pro.py
```

The default refresh is every 90 seconds. You can change it:

```bash
python crypto_radar_pro.py --seconds 120
```

A local `dashboard.html` is regenerated every scan. Open that file in a browser.
It refreshes itself while the Python scanner is running.

## Files created while it runs

- `dashboard.html` — phone/computer-friendly dashboard
- `data/signal_history.csv` — every ranked scan
- `data/paper_trades.json` — illustrative paper positions/results

## Important limitations

No scanner can literally take "everything" into account. This one intentionally
focuses on measurable inputs that can be obtained from public endpoints.

Headline sentiment is deliberately simple. It does not understand sarcasm,
rumors, fake news, regulatory nuance, token unlocks, wallet flows, SEC filings,
or every exchange. Derivatives data may be unavailable for some spot symbols.

Public endpoints can change, throttle, or be regionally unavailable. If the
configured market-data provider blocks your region, the program will tell you.

## Before trusting it

A useful validation standard is not "it found a winner once." Evaluate at least:

- 100+ completed paper trades
- win rate
- average win vs. average loss
- profit factor
- maximum drawdown
- results after realistic fees/slippage
- performance in trending AND sideways markets

If those numbers are poor, modify the strategy; do not connect money.
