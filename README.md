# Wheel Strategy Agent

Autonomous wheel-strategy trading agent for the Alpaca Trading API.

Sells cash-secured puts on a watchlist of liquid names; if assigned, switches to
selling covered calls on the resulting shares. Enforces explicit risk gates
before every trade and logs every decision (including rejected trades) to
`trade_log.csv`.

## Risk Gates

- **Per-position collateral cap** — max 50% of account per cash-secured put
- **Total deployment cap** — never deploy more than 65% of account at once
- **Daily loss halt** — halt new entries if down more than 3% on the day
- **Minimum open interest** — skip illiquid contracts
- **Earnings blackout** — skip names reporting within N days

## Requirements

- `alpaca` CLI authenticated against a paper account
  (`alpaca profile login` or `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` env vars)

## Run

```bash
python3 wheel_agent.py
```

Every cycle appends to `trade_log.csv`: trades, skips, and errors with reasons.
