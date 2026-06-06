# Research: Public Data Sources for Crypto Futures Screener

**Date:** 2026-04-24  
**Author:** CEO agent (AVEA-13)  
**Scope:** Free/public APIs — Bybit, Binance, CoinGlass, CoinGecko

---

## 1. Bybit Public API (already integrated)

**Base URL:** `https://api.bybit.com/v5`  
**Auth required:** No (public endpoints used)  
**Rate limits:** ~600 req/min per IP

### What's already used
| Endpoint | Data | Update Freq |
|----------|------|------------|
| `/market/tickers` | All perp tickers (price, OI, funding, turnover24h) | Real-time |
| `/market/kline` | OHLCV candles (1H, 4H, D) | Per candle |
| `/market/open-interest` | OI history (1H) | 1H |
| `/market/funding/history` | Funding rate history (8H periods) | Per settlement |
| `/market/account-ratio` | Long/Short ratio top traders | 1H |
| `/market/recent-trade` | Last 500 trades (for trade CVD) | Real-time |
| `/market/orderbook` | Bid/ask depth 50 levels | Real-time |

### What's available but NOT yet used
| Endpoint | Data | Signal Potential |
|----------|------|-----------------|
| `/market/instruments-info?category=linear` | Contract specs, max leverage, launch date | Already partially used (listing_age_days), but tick size / min order could filter illiquid pairs better |
| `/market/risk-limit` | Risk tiers per symbol | Low — operational only |
| `/market/delivery-price` | Settlement price history | Low for perps |
| `/market/insurance` | Insurance fund balance | **Medium** — large insurance fund drawdown signals high recent liquidation events even without DB |
| `/market/long-short-ratio` (global) | Market-wide L/S ratio | **Medium** — global sentiment crosscheck |

### Improvement potential (Bybit)
- **Insurance fund trend:** `GET /v5/market/insurance` → track day-over-day change. A falling insurance fund confirms recent cascade liquidations (reinforces Squeeze setup).
- **Global Long/Short ratio:** Different from per-symbol L/S. A market-wide extreme (>70% long globally) is a contrarian squeeze signal. Free, 1H resolution.

---

## 2. Binance Futures Public API (partially integrated via binance_bridge.py)

**Base URL:** `https://fapi.binance.com`  
**Auth required:** No (public market data)  
**Rate limits:** 2400 requests/min

### What's already used (via binance_bridge.py)
| Endpoint | Data | Usage |
|----------|------|-------|
| `/fapi/v1/premiumIndex` | Funding + mark/index price for all symbols | Cross-exchange funding confirmation (+5/+8 to score) |
| `/fapi/v1/openInterest` | Per-symbol current OI | Cross-exchange OI trend confirmation (+8 to score) |
| `/futures/data/openInterestHist` | Historical OI (1H) | 24H OI change on Binance side |

### What's available but NOT yet used
| Endpoint | Data | Update Freq | Signal Potential |
|----------|------|------------|-----------------|
| `/fapi/v1/ticker/24hr` | 24H volume, price change for ALL symbols | Real-time | **High** — Binance USDT volume >> Bybit volume for most alts; better volume spike detection |
| `/fapi/v1/klines` | OHLCV candles (any TF) | Per candle | **High** — cross-exchange volume confirmation; if vol spike on BOTH exchanges it's genuine demand, not wash trading |
| `/futures/data/globalLongShortAccountRatio` | Global L/S account ratio | 5m / 15m / 1H | **High** — Binance has far more retail → global sentiment signal independent from Bybit's pro traders |
| `/futures/data/topLongShortAccountRatio` | Top-trader L/S account ratio | Same | **Medium** — similar to Bybit L/S but larger sample |
| `/futures/data/topLongShortPositionRatio` | Top-trader position ratio | Same | **Medium** |
| `/futures/data/takerlongshortRatio` | Taker buy/sell volume ratio | 5m–1H | **High** — direct CVD proxy, much simpler to fetch than raw trades; can confirm Bybit CVD signal |
| `/fapi/v1/aggTrades` | Aggregated trades (faster than raw trades) | Real-time | **Medium** — faster CVD computation, less bandwidth than full trade stream |
| `/fapi/v1/depth` | Order book depth | Real-time | **Low** — already have Bybit DOM; cross-exchange DOM rarely adds alpha in real-time |

### Key Binance signals NOT available on Bybit
- **Taker L/S ratio** (5m granularity) — shows real-time aggressive buying/selling pressure. Bybit only provides account-ratio (who holds positions), not who is the taker. This is a distinct metric.
- **Global account L/S** — Binance has ~3x Bybit's retail user base, making this signal statistically stronger for detecting retail sentiment extremes.
- **Cross-exchange volume confirmation** — if volume spike appears on Bybit but NOT on Binance for the same pair, it's likely wash trading or thin order book manipulation. Dual-exchange volume validation would reduce false breakout signals.

---

## 3. CoinGlass Public Endpoints

**Base URL:** `https://open-api.coinglass.com/public/v2`  
**Auth required:** Free tier requires API key (free registration), but several endpoints return public data  
**Note:** Most valuable endpoints (heatmap, detailed liquidation data) require API key. The key is free.

### Available without API key
| Endpoint | Data | Signal Potential |
|----------|------|-----------------|
| `https://fapi.coinglass.com/api/futures/fundingRate/latest` | Cross-exchange funding rates aggregated | **Medium** — already approximated via Bybit+Binance bridge |
| `https://fapi.coinglass.com/api/futures/openInterest/chart` | OI aggregated across exchanges | **Medium** — existing per-exchange data covers most of this |

### Available with free API key
| Endpoint | Data | Update Freq | Signal Potential |
|----------|------|------------|-----------------|
| `/public/v2/funding` | Per-exchange funding rates, all pairs | 1H | **Medium** — adds FTX/OKX/Deribit cross-reference |
| `/public/v2/open-interest` | Aggregated OI across 10+ exchanges | 1H | **High** — total market OI is a better signal than single-exchange OI; spike in aggregate OI = real new money |
| `/public/v2/liquidation` | Hourly liquidation volumes by pair, by side | 1H | **High** — this is the key missing data; CoinGlass aggregates liquidations from ALL major exchanges, not just what liquidation_tracker.py captures from Bybit websocket |
| `/public/v2/long-short-ratio` | Global L/S by exchange | 15m | **Medium** |
| `/public/v2/grayscale` | Grayscale trust premium/discount | Daily | **Low** — BTC/ETH specific, slow-moving |
| Liquidation heatmap (requires Pro) | Predicted liquidation clusters by price level | Dynamic | **Very High** — shows WHERE liquidations will cascade; critical for Squeeze and Sweep setups |

### Key insight: CoinGlass liquidation data
The current `liquidation_tracker.py` captures Bybit websocket liquidation events in real-time and stores in SQLite. CoinGlass aggregates across Bybit + Binance + OKX + Deribit + Huobi + Kraken. For a $1M liquidation event:
- Bybit share is typically 25–40% of total
- Missing 60–75% of the liquidation data in signals
- CoinGlass free API returns hourly aggregates; good enough for the screener's 1H timeframe

**Recommendation:** Register free API key, integrate `/public/v2/liquidation` endpoint. Will significantly improve Squeeze and Range Sweep signal quality.

---

## 4. CoinGecko Free API

**Base URL:** `https://api.coingecko.com/api/v3`  
**Auth required:** No (rate-limited to ~30 calls/min without key; free key gives 500 calls/min)  
**What's already used:**
- `GET /search/trending` → trending coins + categories (TTL 30min) ✅
- `GET /global` → BTC dominance % (TTL 15min) ✅

### What's available but NOT yet used
| Endpoint | Data | Update Freq | Signal Potential |
|----------|------|------------|-----------------|
| `/coins/{id}/market_chart` | Price + volume + market cap history | Daily / Hourly | **Medium** — redundant with Bybit klines for perp pairs; more useful for spot-only assets |
| `/coins/markets` | Price, volume, MCap, rank for all coins | 1–5min | **High** — provides **spot volume** for all coins; combined with Bybit perp volume gives the perp/spot volume ratio currently computed only for BTC |
| `/coins/{id}/ticker` | Trading volume by exchange | As-is | **Medium** — shows which exchanges are driving volume |
| `/global/decentralized_finance_defi` | DeFi global metrics | Daily | **Low** — too macro |
| `/coins/categories` | Category market caps + 1H/24H changes | 1–5min | **High** — already fetching trending categories; adding category market cap changes identifies sector rotation in real-time |
| `/coins/{id}` | Full coin detail: sector, liquidity, FDV, token unlocks schedule | Cached | **Medium** — token unlock schedule is key for Pre-Pump signals: avoid entering if large unlock is imminent |
| `/search/trending` (already used) | Trending coins 7 days | 30min | ✅ Used |
| `/global` (already used) | BTC.d, total MCap, ETH.d | 15min | ✅ Used |

### Key insight: Perp/Spot ratio for altcoins
Currently `perp_spot_ratio` is only calculated for BTC (comparing Bybit perp OI to CoinGecko spot MCap). This ratio is available for BTC only because the global market cap endpoint breaks down by coin.

To get perp/spot ratio for altcoins (critical for Pre-Pump and Squeeze detection):
1. Fetch Bybit OI in USDT for the symbol (already done)
2. Fetch CoinGecko `/coins/markets?ids=XXX` for the same coin's market cap
3. perp/spot = OI_USDT / spot_volume_24h_USDT

This would allow per-symbol perp/spot ratio scoring, not just BTC-level. **This is the highest-value CoinGecko integration.**

---

## Summary: Data Source Priority Matrix

| Source | Integration Status | Priority | Effort | Value |
|--------|--------------------|----------|--------|-------|
| Bybit insurance fund | Not used | Low | 1h | Medium |
| Bybit global L/S ratio | Not used | Low | 30min | Low |
| Binance taker L/S ratio | Not used | **High** | 2h | High |
| Binance global account L/S | Not used | **High** | 2h | High |
| Binance cross-volume validation | Not used | **High** | 3h | High |
| CoinGlass liquidation aggregated | Not used | **High** | 4h | Very High |
| CoinGlass aggregate OI | Not used | Medium | 2h | Medium |
| CoinGecko coins/markets (spot vol) | Not used | **High** | 3h | High |
| CoinGecko categories (sector rotation) | Not used | Medium | 2h | Medium |

**Top 3 highest-impact integrations:**
1. **CoinGlass aggregate liquidations** — fixes the biggest gap in Squeeze/Sweep setups (currently capturing only 25-40% of real liquidations)
2. **Binance taker L/S ratio** — true buy/sell aggression proxy, distinct from Bybit's position-based L/S
3. **CoinGecko spot volume for alts** — enables per-symbol perp/spot ratio which is already a scoring factor but only approximated globally
