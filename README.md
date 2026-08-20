# Crypto Radar Pro — iPhone Web App

This is the combined mobile version with three modes:

- Conservative
- Aggressive
- Very Aggressive

The app gives one primary answer:
- **BEST TRADE RIGHT NOW**, or
- **NO TRADE**

For a qualifying setup it shows:
- exact pair
- suggested dollar amount based on account size
- entry zone
- stop
- two profit targets
- model score
- confidence score
- spread
- order-book depth
- order-book imbalance
- funding
- open-interest change
- reasons and risks

## Deploy with Render

1. Create a GitHub repository.
2. Upload these four files:
   - app.py
   - requirements.txt
   - render.yaml
   - README.md
3. Create a Render account.
4. Choose **New > Blueprint** and connect the GitHub repo.
5. Deploy.
6. Open the HTTPS address Render gives you.

## Put it on your iPhone Home Screen

1. Open the deployed address in Safari.
2. Tap the Share button.
3. Tap **Add to Home Screen**.
4. Name it `Crypto Radar`.
5. Tap Add.

Now it launches like an app.

## Risk modes

### Conservative
- 0.50% max planned account risk per trade
- 20% max position size
- $100M minimum 24-hour liquidity
- tighter spread/volatility requirements

### Aggressive
- 1.25% max planned account risk per trade
- 20% max position size
- $8M minimum 24-hour liquidity
- wider search across faster-moving coins

### Very Aggressive
- 2.00% max planned account risk per trade
- 15% max position size
- $3M minimum 24-hour liquidity
- widest volatility/spread tolerance

The smaller max position in Very Aggressive mode is deliberate because these coins can move violently.

## Important

This program does not execute trades and contains no exchange account credentials.
It is a rules-based research tool, not a guarantee of profit.
