# Market Opportunity Engine

A mobile-friendly market scanner for finding and ranking opportunities across crypto and stocks.

## Features

### Markets
- Crypto
- Stocks
- Stocks + Crypto

### Time Horizons
- Next 24 Hours
- Next 3 Days
- Next Week
- Next Month
- Next 3 Months
- 1 Year+

### Risk Modes
- Conservative
- Moderate
- Aggressive
- Extreme / Moonshot

## What the Scanner Analyzes

The engine ranks up to 20 opportunities using signals including:

- Price momentum
- Trading volume
- Market capitalization
- Liquidity
- Recent price movement
- News
- Upcoming catalysts
- Product releases
- Partnerships
- Network upgrades
- Regulatory developments
- Listings
- Token events
- Market sentiment
- Fundamental strength
- Whether recent news may already be priced into the asset

The importance of each signal changes depending on the selected investment time horizon.

Short-term scans emphasize momentum, news, catalysts, and market activity.

Long-term scans place more emphasis on fundamentals, market quality, adoption, and longer-term catalysts.

## Crypto Coverage

Crypto market discovery uses CoinGecko.

The scanner performs a broad initial market scan and then performs deeper analysis on the strongest candidates.

This allows smaller cryptocurrencies to appear in Aggressive and Extreme / Moonshot mode instead of only ranking the largest cryptocurrencies.

## Stock Coverage

Stock scanning uses Finnhub.

To enable stock scanning, add a Finnhub API key to Render as an environment variable.

Variable name:

FINNHUB_API_KEY

Never put private API keys directly into this GitHub repository.

## Render Deployment

Build command:

pip install -r requirements.txt

Start command:

gunicorn app:app --workers 1 --threads 4 --timeout 240

## Important

This application is a research and market-ranking tool.

Scores are model-generated opportunity scores and are not guarantees of future returns.

The Extreme / Moonshot category searches for unusually high-upside opportunities, including small-cap cryptocurrencies that could potentially experience very large price increases.

A possible 5x or 10x outcome does NOT mean the model predicts that return will occur.

Assets capable of extremely large gains can also lose most or all of their value.

Always verify important information and manage position size appropriately.
