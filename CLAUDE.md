# Crypto Futures Trading System — Project Instructions

## Context

This is a Bybit perpetual futures screener that identifies trade setups via funding rate, OI divergence, FVG/Order Blocks, liquidity sweeps, and CVD. The user trades leveraged crypto futures and adds knowledge incrementally.

---

## AUTO-SKILL RULES (no slash command required)

### 1. Chart Images → Technical Analysis (auto)

**Trigger**: User shares ANY image file (PNG, JPG, screenshot) that looks like a price chart.

**Action**: Immediately apply the `technical-analyst` skill framework WITHOUT waiting for `/technical-analyst`. Deliver:
- Trend direction (HTF + current TF)
- Key support / resistance levels with prices
- Active patterns (structure, candlestick, volume)
- **2–4 scenarios** with probability estimates (e.g. "60% bull continuation, 40% retest first")
- Suggested entry zone, invalidation level

Do NOT ask "do you want analysis?" — just do it.

### 2. Strategy Description → Backtest Framework (auto)

**Trigger**: User describes a trading strategy, entry/exit rules, or asks "does this work / is this profitable / how to test this".

**Action**: Immediately apply the `backtest-expert` skill framework. Cover:
- Hypothesis clarity check
- Stress test plan (parameter sensitivity, slippage 1.5–2×, year-by-year)
- Sample size requirements
- Walk-forward validation structure
- Deploy / Refine / Abandon verdict criteria

Do NOT wait for `/backtest-expert` command.

---

## Screener Integration

When the screener outputs top candidates:
- Mention which candidates would benefit from chart review (high score but no clear pattern)
- For any coin with `grade=A+` or `score > 120`: suggest checking weekly/daily chart before entry
- Stop-loss guidance: ATR×0.65 buffer minimum, ATR×1.8 fallback for volatile alts (liquidity sweep protection)

## Stack

- Python screener: `screener.py`, `telegram_bot.py`, `liquidation_tracker.py`, `channel_reader.py`
- Bybit V5 Public API (no keys needed for market data)
- Telegram alerts via `telegram_alerts.py`
- Outcome tracking: `outcomes/pending.json`, `outcomes/resolved.csv`

## Key Decisions / History

- Stop losses must be wide enough to survive liquidity sweeps (user was getting swept on tight SLs)
- `detect_sweep` uses 3-candle lookback
- `short_dist` setup threshold = 5
- Stacked order walls flagged with ⚠ when R:R < 2 (from PDF "Механика раскачки депозита")
- OI changes interpreted position-aware (rising OI in uptrend = new longs, not shorts)
