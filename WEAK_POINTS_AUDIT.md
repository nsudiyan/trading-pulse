# WEAK_POINTS_AUDIT.md — Code Audit Report

**Date:** 2026-05-27  
**Task:** AVEC-54  
**Scope:** pump_detector.py (in-depth) + full project — screener.py, telegram_bot.py, all dependency modules  
**Method:** Full static analysis; cross-module contradiction checks  
**Files audited:** pump_detector.py, screener.py, free_data.py, telegram_alerts.py, telegram_bot.py, outcome_tracker.py, liquidation_tracker.py, sector_heat.py, orderbook_imbalance.py, pattern_engine.py, rca_engine.py, chart_analyzer.py, sweep_watcher.py, boost_watcher.py, rug_detector.py, channel_reader.py

---

## 1. Summary: Top-5 Critical Problems

### #1 — orderbook_imbalance.py:183 — All OB metrics corrupt after first WebSocket delta (HIGH)
`_apply_delta()` sorts all price levels `reverse=True` (descending). This is correct for bids but is also applied unconditionally to asks. After any WS delta update, the asks list is stored highest-price-first, so `best_ask = asks[0].price` returns the **worst** ask (furthest from mid), not the best. Every downstream calculation — spread, wall detection, imbalance depth — is inverted and wrong after the first delta event.

### #2 — rca_engine.py:450–466 — rca_results.json has no file lock; concurrent writes corrupt the RCA database (HIGH)
`store_rca()` does read → append → write_text with no locking. `outcome_tracker.check_and_resolve()` calls this inside a resolution loop. When screener cron and a Telegram `/run` command overlap, both read the same file, each append, and the last writer overwrites the other's additions. The entire RCA analytics database can be silently corrupted. Contrast with `pending.json`, which correctly uses `atomic_json_update`.

### #3 — outcome_tracker.py:363 vs screener.py:5339 — TP1 multiplier mismatch inflates win-rate statistics (HIGH)
`outcome_tracker._plan_from_result()` sets TP1 at ATR×2.7. `screener.py build_trade_plan()` sets TP1 at ATR×3.6 (a 33% discrepancy). The comment in screener.py also falsely says "ATR×2.7". The tracker checks a closer TP1 level than what the user's actual plan shows, so `hit_tp1_4h` fires earlier than the real trade would. Win-rate statistics for TP1 are systematically inflated. `resolved.csv` trains the calibration model on wrong labels.

### #4 — telegram_alerts.py:581 — short_dist signals silently dropped from watchlist (HIGH)
`format_watchlist()` for SHORT includes only `bos_fvg + range_sweep`. `short_dist` is never included in either the LONG or SHORT watchlist. All S5 signals — historically the **best-performing setup at 54.9% WR** per backtest findings — are silently suppressed from every Telegram watchlist message. Users never see them.

### #5 — sector_heat.py:177 / outcome_tracker.py:361 — Direction language mismatch corrupts the full analytics chain (HIGH)
`sector_heat.py` emits candidates with `"direction": "LONG"` (English). `outcome_tracker.py` compares against `"ЛОНГ"` (Russian) throughout (line 361, 443, 516). `rca_engine._assess_signal_validity()` uses `is_long = direction == "ЛОНГ"` — inverted for sector_heat signals. `pattern_engine._direction_norm()` won't match "LONG" and bins all sector_heat data as `dir_wait`, excluding it from pattern hypotheses. The entire analytics chain runs wrong direction logic for every sector_heat-sourced signal.

---

## 2. General Project Audit

### 2.1 Scoring Contradictions

**F-01 | screener.py:2991–3003, 3131–3143, 3319–3327 | RS_BTC double-scoring in S1, S2, S4 | HIGH**
- **What:** `rs_btc > 2.0 → +12` and `rs_btc > 1.3 → +6` are both standalone `if` blocks, not `elif`. A coin at rs_btc=2.1 satisfies both and can accumulate both bonuses depending on setup.
- **Why weak:** Score inflation from non-exclusive scoring conditions.
- **Severity:** HIGH
- **Fix:** `if rs_btc > 2.0: +12 elif rs_btc > 1.3: +6 elif rs_btc < 0.8: -8`

**F-02 | screener.py:81 | HARD_BLOCK_HOURS missing hour 17 despite comment saying WR=30.2% catastrophic | HIGH**
- **What:** Comment at line 81 says "hour 17 WR=30.2% catastrophic" but `HARD_BLOCK_HOURS = {18, 19}`. Hour 17 absent from both `HARD_BLOCK_HOURS` and `BAD_SIGNAL_HOURS`. Also: hour 14 is documented as moved to HARD_BLOCK (comment at line 74) but `{18, 19}` still doesn't contain it. `pump_detector._PUMP_BAD_HOURS = {1, 13, 14, 20, 23, 0}` includes hour 14 — same hour, opposite treatment.
- **Severity:** HIGH
- **Fix:** Add 17 (and 14 if intended) to `HARD_BLOCK_HOURS`. Remove misleading comment or reconcile with pump_detector.

**F-03 | screener.py:3026–3027 vs 3144–3148 | CHoCH treatment inverted between setups | MED**
- **What:** S1 (squeeze): `bull_choch → s1 -= 15` (penalty). S2 (bos_fvg): `bull_choch or bear_choch → s2 = int(s2 * 1.2)` (bonus). Same signal, opposite treatment.
- **Severity:** MED
- **Fix:** Document why (S1: CHoCH = reversal risk in squeeze; S2: CHoCH = structure confirmation) or align treatment.

**F-04 | screener.py:4847, 4859, 4962 | Three grade functions with different A+ thresholds | MED**
- **What:** `score_grade()` → A+ at ≥100. `composite_grade()` → A+ at ≥90 + MTF≥2. `calc_mtf_grade()` → A+ at ≥90 + MTF≥2 + no overheat + long direction. Same score → different grade depending on caller.
- **Severity:** MED
- **Fix:** Deprecate `score_grade()` and `composite_grade()`; route all callers through `calc_mtf_grade()`.

**F-05 | screener.py (POC scoring) | POC distance scored differently in S1 vs S2 | MED**
- **What:** S1: distance < 1.5% → +8, < 3% → +4. S2: distance < 2% → +10, < 4% → +5. A coin 1.8% from POC gets +4 in S1 but +10 in S2.
- **Severity:** MED
- **Fix:** Unify into a shared POC scoring function or document the rationale explicitly.

**F-06 | screener.py:2289, 2321 | Regime classifiers have no hysteresis | MED**
- **What:** `classify_funding_regime()` and `classify_oi_regime()` recompute fresh every scan. Near boundaries (e.g., funding oscillating ±0.01%), the regime label flips every cycle, causing score oscillation ±5–10 points per scan.
- **Severity:** MED
- **Fix:** Cache last regime per symbol; require N consecutive samples past threshold before flipping.

**F-07 | rca_engine.py:90, 103 | OI "favorable" logic identical for longs and shorts | MED**
- **What:** Both long and short branches set `"oi_trend": "favorable" if oi_24h < -2`. Correct short-favorable OI is rising OI during downtrend (`oi_24h > 5`).
- **Severity:** MED
- **Fix:** For shorts: `"favorable" if oi_24h > 5 else ("extreme" if oi_24h > 20 else "neutral")`.

**F-08 | outcome_tracker.py:363, 367 vs screener.py:5339, 5457 | TP1 multiplier mismatch | HIGH**
- (See Top-5 #3 above)

**F-09 | pump_detector.py:895–898 | price_chg_4h covers 5h10m, not 4h | MED**
- **What:** With `limit=62` 5-minute candles, `opens[0]` is 310 minutes = 5h10m ago. Variable named `price_chg_4h` everywhere — in alert text ("за 4ч"), return dict, and CSV. `oi_chg_4h` window is separately 50×5m = 250 min = 4h17m. Two different windows compared as if the same.
- **Severity:** MED
- **Fix:** Use `limit=48` for exact 4h, or rename to `price_chg_5h` everywhere.

**F-10 | pump_detector.py:905 | oi_chg_30m / price_chg_30m use index -7 = 35 minutes | LOW**
- **What:** `closes[-7]` = 7 × 5min = 35 minutes. Variables labeled `_30m`.
- **Severity:** LOW
- **Fix:** Use `-6` for 30min or rename variables to `_35m`.

---

### 2.2 Dead / Unreachable Code

**F-11 | screener.py:3157–3235 | Setup S3 (range_sweep) fully computed then zeroed | MED**
- **What:** Lines 3157–3232 compute a complete S3 score. Lines 3233–3235 unconditionally `s3 = 0`, `setup_dir_s3 = None`. The entire scoring block is dead.
- **Severity:** MED
- **Fix:** Remove the zeroing block (re-enable S3) or delete the dead scoring block entirely.

**F-12 | screener.py:2676–2689 | Funding streak dict branch unreachable | LOW**
- **What:** `fetch_funding_history()` always returns `list[float]`. The `isinstance(item, dict)` branch never executes.
- **Severity:** LOW
- **Fix:** Remove the dict branch.

**F-13 | pump_detector.py:1251, 1275 | CryptoPanic dead code — defined but never called | MED**
- **What:** `fetch_crypto_panic_sentiment()` (lines 482–528) and constants `PANIC_MIN_SCORE`, `PANIC_BEARISH_PEN` (lines 256–257) are fully defined. `cp_sentiment = None` is hardcoded. The function is never called in `_analyze_symbol` or `scan_once`. Always `None`.
- **Severity:** MED
- **Fix:** Either wire it up (call when `score >= PANIC_MIN_SCORE`) or delete as dead code.

**F-14 | pump_detector.py:99–101 | TLDB_FUND_EXTREME_NEG, TLDB_OI_SURGE_SOLO never used | MED**
- **What:** Both constants defined, never referenced in any scoring logic.
- **Severity:** MED
- **Fix:** Apply or delete.

**F-15 | pump_detector.py:47–49 | FUND_RECOVERY_FROM/TO, FUND_STEALTH_SHORT orphaned | MED**
- **What:** Three named threshold constants defined but never used. Pattern 1 uses hard-coded `-0.03` instead of `FUND_RECOVERY_FROM`.
- **Severity:** MED
- **Fix:** Use named constants in pattern thresholds or remove them.

**F-16 | pump_detector.py:103 | REAL_CVD_LIMIT=150 — dead constant | LOW**
- **What:** Defined, never used. `_fetch_trades_raw` hard-codes `limit=500`.
- **Severity:** LOW
- **Fix:** Apply or delete.

**F-17 | liquidation_tracker.py:707, 767 | Hyperliquid always disabled; HL code unreachable | MED**
- **What:** `p.add_argument("--no-hl", action="store_true", default=True)` — default `True` means always-on regardless of CLI. Line 767 also hardcodes `include_hl=False`. All of `_hl_batch`, `hyperliquid_liq_stream`, `fetch_hl_coins`, `bybit_to_hl_coin` are dead wiring.
- **Severity:** MED
- **Fix:** Remove `default=True`. Pass `include_hl=not args.no_hl`. Or delete all HL code if support is dropped.

**F-18 | screener.py:293 | fetch_klines has undocumented global side effect | LOW**
- **What:** `fetch_klines()` silently writes to `_kl_open_ts` module-level dict. Testing/validation callers mutate global state.
- **Severity:** LOW
- **Fix:** Add `track_ts: bool = True` parameter.

---

### 2.3 Multiple Sources of Truth

**F-19 | screener.py:~5082 vs telegram_alerts.py:780 | SECTOR_MAP defined in two files with different assignments | HIGH**
- **What:** Two separate SECTOR_MAP dicts with different symbol assignments and different sector names (DePIN, Perp, LST in telegram_alerts but not in screener). Same token can be in different sectors depending on which file is queried.
- **Why weak:** Sector rotation signals and heatmap labels are inconsistent.
- **Severity:** HIGH
- **Fix:** Move to a shared `config.py`; import in both files.

**F-20 | pump_detector.py:127–133 vs outcome_tracker.py:56–57 | Two separate outcome trackers — pump data excluded from calibration model | HIGH**
- **What:** pump_detector writes to `outcomes/pump_pending.json` / `outcomes/pump_resolved.csv`. `calibration/train_model.py` trains only on `outcomes/resolved.csv` (screener). Pump-detector signal quality is invisible to the scoring model.
- **Why weak:** The calibration model systematically ignores pump signals. If pump_detector generates many losses, the model never learns to penalize contributing features.
- **Severity:** HIGH
- **Fix:** Merge into a single `resolved.csv` with `source=pump|screener` field, or add `pump_resolved.csv` to the training pipeline.

**F-21 | pump_detector.py:687 vs outcome_tracker.py:451 | MAE sign convention inverted between trackers | HIGH**
- **What:** `pump_detector.py:687`: `mae = (p_entry - ml) / p_entry * 100` → **positive** number (long). `outcome_tracker.py:451`: `mae_pct = ((min_low - entry_px) / entry_px * 100)` → **negative** number (long). Any cross-file MAE analysis combines inverted numbers.
- **Severity:** HIGH
- **Fix:** Standardize to a single convention (MAE = always positive = distance against position) and update both files.

**F-22 | outcome_tracker.py:1210–1218 vs telegram_bot.py:117–131 | Duplicate config loading with different env-var override | LOW**
- **What:** `send_weekly_report` does raw `json.loads(cfg_path.read_text(...))` with no env-var override. Token set only via env will be missing → reports silently fail.
- **Severity:** LOW
- **Fix:** Use `telegram_alerts.load_config()` which already respects env vars.

---

### 2.4 Race Conditions / File Safety

**F-23 | rca_engine.py:450–466 | rca_results.json no file lock | HIGH**
- (See Top-5 #2 above)

**F-24 | pump_detector.py:660–661 | json.load(open(...)) races with atomic_json_update | MED**
- **What:** `json.load(open(PUMP_PENDING_FILE))` — no file lock, no `with`. `_log_pump_pending()` writes via `atomic_json_update`. Concurrent read + atomic write can read a partial file.
- **Severity:** MED
- **Fix:** Use `atomic_json_update` snapshot-read or `with open(...) as f`.

**F-25 | outcome_tracker.py:649 / channel_reader.py:1484 | channel_accuracy.json no lock | MED**
- **What:** `_save_channel_accuracy` uses `Path.write_text()`. `channel_reader` reads the same file concurrently. Partial write leaves malformed JSON; reader falls back to empty dict, losing all accuracy data.
- **Severity:** MED
- **Fix:** Use `atomic_json_update` for both read and write.

**F-26 | sector_heat.py:97–99 | cooldown.json no lock | LOW**
- **What:** `_save_cooldown` is bare `write_text`. Two concurrent scan cycles can interleave, causing sectors to be re-alerted within cooldown.
- **Severity:** LOW
- **Fix:** Use `atomic_json_update` or `tempfile + os.replace`.

**F-27 | free_data.py:54 | Cache writes not atomic | MED**
- **What:** `CACHE_FILE.write_text(json.dumps(cache))` direct write. Screener and background processes share this file. Partial overwrites possible under concurrent access.
- **Severity:** MED
- **Fix:** Write to `.tmp`, then `os.replace()`, or use `atomic_json_update`.

**F-28 | pattern_engine.py:325 / outcome_tracker.py:186 | CSV partial-read + no-lock append | MED**
- **What:** `pattern_engine.generate_patterns()` opens `resolved.csv` with no try/except. `outcome_tracker._append_csv()` uses plain `open("a")` — not atomic. Concurrent read during write can produce a partial last row. `save_patterns()` overwrites `pattern_report.json` with no backup.
- **Severity:** MED
- **Fix:** Wrap CSV open in try/except. Convert `_append_csv` to atomic temp-file swap.

---

### 2.5 Timezone / Data Quality

**F-29 | pump_detector.py:260–262 | datetime.now() (local) mixed with UTC time-gate | MED**
- **What:** `_log()` uses `datetime.now()` (local). Time-gate at line 1879 uses `datetime.now(timezone.utc).hour`. TG alert timestamp at line 1530 uses `datetime.now()` (local). Log timestamps and gate logic use different timezone references.
- **Severity:** MED
- **Fix:** Standardize all `datetime.now()` to `datetime.now(timezone.utc)`.

**F-30 | telegram_bot.py:99,212 / sector_heat.py:199 / channel_reader.py:1168 | Local time in alerts vs UTC signals | MED**
- **What:** `outcome_tracker` stores signals with `datetime.utcnow()`. `telegram_bot.py` caches timestamps with `datetime.now()` (local). `sector_heat.py` displays `datetime.now().strftime('%H:%M')` in alerts. On a non-UTC server, the 4h resolution window fires offset by the timezone.
- **Severity:** MED
- **Fix:** Replace all `datetime.now()` in display/log code with `datetime.now(timezone.utc)`.

**F-31 | outcome_tracker.py:179, 262, 1020 | datetime.utcnow() deprecated in Python 3.12+ | LOW**
- **What:** `datetime.utcnow()` deprecated in 3.12, removed in 3.13.
- **Severity:** LOW
- **Fix:** Replace with `datetime.now(timezone.utc)`.

**F-32 | liquidation_tracker.py:452 | WS timestamp fallback uses local time | LOW**
- **What:** Fallback is `time.time() * 1000`. On non-UTC machines the timestamp is wrong by the timezone offset, causing liq events to appear in wrong time buckets.
- **Severity:** LOW
- **Fix:** Change fallback to `int(datetime.now(timezone.utc).timestamp() * 1000)`.

**F-33 | rug_detector.py:108, 288 | WHALE_TRANSFER_PCT = 0.30 divided by 100 — effective threshold 0.3%, not 30% | HIGH**
- **What:** `whale_th = circ_supply * WHALE_TRANSFER_PCT / 100`. With `WHALE_TRANSFER_PCT = 0.30`, the effective threshold is 0.003× supply = 0.3%, not the implied 30%. The constant name suggests 30%. For a 1B-supply token: 3M tokens trigger the whale flag, not 300M. Trigger rate is 100× higher than intended.
- **Severity:** HIGH
- **Fix:** Rename to `WHALE_TRANSFER_FRAC = 0.003` (remove the `/100`) or change to `WHALE_TRANSFER_PCT = 30.0` and keep the `/100`.

---

### 2.6 Telegram / Output Layer

**F-34 | telegram_alerts.py:581 | short_dist signals silently dropped from watchlist | HIGH**
- (See Top-5 #4 above)

**F-35 | telegram_alerts.py:481 | format_snapshot miscounts setups — bos_fvg hardcoded as short | MED**
- **What:** `longs_count = squeeze_count + breakout_count`, `shorts_count = bos_fvg_count`. Bull BOS (direction="long") is counted as a short. S5 (short_dist) not counted anywhere.
- **Severity:** MED
- **Fix:** Count by `s["setup_dir"] == "short"` / `== "long"` rather than by setup name.

**F-36 | telegram_alerts.py:467 | B+ grade missing from risk sizing | MED**
- **What:** `_grade_risk()` handles A+, A, B, default (1%). B+ (output by `calc_mtf_grade()`) hits the default 1% — more than B's 0.75%, which is wrong ordering.
- **Severity:** MED
- **Fix:** Add `elif grade == "B+": return 0.90` between B and default.

**F-37 | telegram_alerts.py:68 | Claude filter silently overwrites plan values with no logging | MED**
- **What:** When Claude returns `action=="GO"`, `_apply_claude_filter()` overwrites `plan["tp1"]`, `plan["stop"]`, `plan["rr"]` in-place. Original screener values are lost with no trace.
- **Severity:** MED
- **Fix:** Log original vs Claude values before overwriting, or store originals under `plan["orig_tp1"]` etc.

**F-38 | screener.py:3866–3891 | Narrative engine only fires for long direction | MED**
- **What:** All narrative multipliers are inside `if setup_dir == "long":`. Short setups never receive narrative adjustment.
- **Severity:** MED
- **Fix:** Add `elif setup_dir == "short":` branch with mirrored logic, or document that narrative is long-only.

---

### 2.7 Logic / Edge Cases

**F-39 | screener.py (bos_fvg) | direction="none" passes all directional filters | HIGH**
- **What:** `detect_bos_fvg()` can return `direction="none"`. No early-exit in S2 scoring block. Signal enters Telegram with direction "none", hitting edge cases in `format_watchlist` and `_grade_risk`.
- **Severity:** HIGH
- **Fix:** In S2 block: `if bos_fvg_dir == "none": s2 = 0` before any scoring.

**F-40 | screener.py:4892, 4935 | EMA seed applied twice in weekly/15m context | HIGH**
- **What:** Both `compute_weekly_context()` and `compute_15m_context()` seed EMA with `ema = closes[0]`, then loop `for c in closes:` starting from index 0. First candle weighted twice. All EMA-derived trend signals biased.
- **Severity:** HIGH
- **Fix:** Change loop to `for c in closes[1:]:`.

**F-41 | screener.py | score_symbol returns no "grade" key on some code paths | MED**
- **What:** `send_signal_alert()` references `result["grade"]`. `grade` is added only on certain paths. Early returns or missed paths → `KeyError` in alert formatter.
- **Severity:** MED
- **Fix:** Initialize `result["grade"] = "C"` at top of `score_symbol()` before any early returns.

**F-42 | screener.py (TLDB gate) | TLDB PROHIBITED early-return skips time gate and score gate | MED**
- **What:** TLDB PROHIBITED early-returns before `HARD_BLOCK_HOURS` and minimum score checks. When TLDB changes from PROHIBITED to CAUTION, the signal suddenly passes all gates it never encountered before.
- **Severity:** MED
- **Fix:** Run time gate and minimum-score check before TLDB verdict.

**F-43 | screener.py (vol_ratio) | Missing volume data injects neutral score | MED**
- **What:** Insufficient volume data → `vol_ratio_med = vol_ratio_cur = 1.0`. Signal looks like "flat volume" instead of "no data" and proceeds through scoring normally.
- **Severity:** MED
- **Fix:** Set `vol_data_missing = True`; skip volume scoring when set.

**F-44 | screener.py (detect_liq_events) | Current forming candle included in sweep detection | LOW**
- **What:** Iterates all returned klines including the last forming candle. Partial volume on the current candle can spike-flag every scan.
- **Severity:** LOW
- **Fix:** Slice `klines[:-1]` to exclude the forming candle.

**F-45 | screener.py (calc_atr) | No guard for closes[-1] == 0 | LOW**
- **What:** ATR computation divides by `closes[-1]`. If zero (degenerate data) → inf or NaN propagates into stop-loss and TP.
- **Severity:** LOW
- **Fix:** Add `if closes[-1] == 0: return None` at start of `calc_atr()`.

**F-46 | pump_detector.py:1069–1177 | Pattern 3 (SLOW_DIST) evaluated last; VOLUME_SURGE wins if both fire | MED**
- **What:** Patterns 4–7 checked before Pattern 3 (SLOW_DIST). If both VOLUME_SURGE (P4) and SLOW_DIST conditions are simultaneously met, VOLUME_SURGE wins — a rug-setup can be mislabelled as a pump. Not documented as intentional.
- **Severity:** MED
- **Fix:** Evaluate SLOW_DIST before pure pump patterns, or document explicit priority order.

**F-47 | pump_detector.py:1182–1185 | Zero-volume candle passes SLOW_DIST vol-stability gate | MED**
- **What:** `v_ratio_3d = max(v4[-18:]) / min(v4[-18:])`. Guard returns `v_ratio_3d = 0` on zero-volume candle. SLOW_DIST condition `v_ratio_3d <= 3.0` is True → zero-volume candle passes.
- **Severity:** MED
- **Fix:** `if v_ratio_3d == 0: skip SLOW_DIST` or `if v_min <= 0: v_ratio_3d = float('inf')`.

**F-48 | pump_detector.py:1241 | Score cap applied mid-scan; enrichments bypass the 130 cap | MED**
- **What:** `score = min(score, 130)` applied before DEX adjustment and OB/skew enrichments. Those enrichments can push score to 175+, uncapped.
- **Severity:** MED
- **Fix:** Apply `min(score, cap)` after all enrichments in `scan_once()` before sorting.

**F-49 | pump_detector.py:822–823 | CVD fallback spans hours but score bonuses don't discount it | MED**
- **What:** When fewer than 5 trades in last 5 minutes, code falls back to ALL 500 trades (possibly spanning hours). `cvd_source` becomes "CVDAll" but Pattern 1 score bonus (`cvd_pct >= 20 → +15`) fires regardless of source.
- **Severity:** MED
- **Fix:** Cap fallback window (30min by timestamp) or discount CVD score bonuses when `cvd_source == "CVDAll"`.

**F-50 | pump_detector.py:929–931 | 5m sweep threshold (0.2%) far too sensitive vs sweep_watcher's 1H bar | MED**
- **What:** `lows[-2] < recent_low * 0.998` (0.2% below support on 5m candles) is extremely common noise. `sweep_watcher` operates on 1H candles — much higher effective bar. Same economic event may or may not trigger depending on module.
- **Severity:** MED
- **Fix:** Increase to 0.5–1.0% for 5m candles, or add 8–12 candle lookback.

**F-51 | pump_detector.py:118 | CONVICTION_MIN_SCORE=85 is a hardcoded operational hack blocking valid signals | HIGH**
- **What:** Comment says raised from 65 "because Claude credits ran out." Signals scoring 65–84 are silently suppressed. No mechanism to auto-revert. Operational state baked into source.
- **Severity:** HIGH
- **Fix:** Move to `.env` / config file. Revert to 65 when credits restored.

**F-52 | pump_detector.py:200–202 vs sweep_watcher.py:277 | Bad-hours sets not shared across modules | MED**
- **What:** `pump_detector._PUMP_BAD_HOURS = {1, 13, 14, 20, 23, 0}`. `sweep_watcher` imports `BAD_SIGNAL_HOURS` from screener. Hour 13 soft-gated in pump_detector, fully allowed in sweep_watcher. Same hour, opposite treatment.
- **Severity:** MED
- **Fix:** Unify bad-hour definitions into a shared constants file.

**F-53 | orderbook_imbalance.py:214 | Absorption prices list always [1.0, 1.0, ...] — logic never implemented | MED**
- **What:** `prices = [u/u for _, u in recent]` divides value by itself. Comment says "placeholder, нужны фактич. цены." The price-flatness check for absorption was never implemented. `absorbed=True` fires on any buy-counterbalanced sell session regardless of price movement.
- **Severity:** MED
- **Fix:** Store actual trade prices in state. Compute `max(prices)/min(prices) - 1`; gate `absorbed` on this < 0.002.

**F-54 | orderbook_imbalance.py:183 | asks sorted descending — best_ask returns worst ask | HIGH**
- (See Top-5 #1 above)

**F-55 | pattern_engine.py:223–226 | English "LONG" / "SHORT" binned as dir_wait | LOW**
- **What:** `_direction_norm` checks `"ОН" in raw` (Cyrillic). "LONG" (English, from sector_heat) won't match either branch → binned as `dir_wait`. Excluded from most pattern hypotheses.
- **Severity:** LOW
- **Fix:** Add `or raw == "LONG"` and `or raw == "SHORT"` to both branches.

**F-56 | free_data.py:61 | Full JSON cache file loaded from disk on every call | LOW**
- **What:** `_cached()` reads and parses the entire cache JSON on every invocation. Dozens of reads per minute under concurrent screener runs on a growing file.
- **Severity:** LOW
- **Fix:** Keep an in-process `_CACHE: dict = {}` module-level dict; load once at import.

**F-57 | free_data.py:205, 216 | Deribit timeouts 2–3s; fails during high-volatility when data is most needed | MED**
- **What:** Deribit public API responds 1–4s under load. Silent `except` swallows timeouts; options skew skipped. High-volatility windows = highest timeout risk = least data.
- **Severity:** MED
- **Fix:** Raise to 8s minimum. Log warning on timeout.

**F-58 | rug_detector.py:171 | Listing flag disabled for tokens > 180 days | MED**
- **What:** `if age_days >= 180: return 0, []`. Organized pump&dump often uses older tokens that were quiet before receiving exchange listings pre-dump. This entire pattern is excluded.
- **Severity:** MED
- **Fix:** Remove the age ceiling or reduce to 90 days.

**F-59 | boost_watcher.py:96–98 | No cooldown by symbol — rate limit risk on multi-address campaigns | MED**
- **What:** `seen` contains only addresses, not symbols. Same token boosted via multiple wallets triggers separate `rug_detector.analyze()` calls per address. Rate limit risk at CoinGecko (30/min).
- **Severity:** MED
- **Fix:** Add a `seen_symbols` set to deduplicate by symbol alongside by address.

**F-60 | outcome_tracker.py:444 | TP/SL hit order within 15-min bar unknown — pessimistic bias | MED**
- **What:** If TP and SL both hitted within the same 15-min bar, both get the same timestamp → `outcome_model.outcome()` returns "LOSS" (pessimistic assumption). Real trade may have hit TP first.
- **Severity:** MED
- **Fix:** Document the pessimistic assumption. Consider 5-min bars for better resolution.

**F-61 | pump_detector.py:90 | _watch_candidates not persisted — lost on daemon restart | MED**
- **What:** `_watch_candidates: dict = {}` lives only in memory. A candidate found 3 minutes before restart is lost and never reaches momentum check.
- **Severity:** MED
- **Fix:** Persist watch-list to JSON (analogous to pump_pending.json).

**F-62 | pump_detector.py:69 | _sent_recently grows indefinitely — memory leak on long runs | LOW**
- **What:** `_sent_recently: dict[str, float] = {}` accumulates every ever-alerted symbol. After weeks of watch-mode: hundreds of entries, never purged.
- **Severity:** LOW
- **Fix:** Periodically delete entries older than `COOLDOWN_SEC × 2`.

**F-63 | pump_detector.py:1339, 1368, 1408 | Three separate asyncio.run() in scan_once | MED**
- **What:** Three sequential event loops created and destroyed per scan. (a) Overhead per 5-min cycle. (b) RuntimeError if `scan_once` is ever called from an async context.
- **Severity:** MED
- **Fix:** Combine into one `asyncio.run(asyncio.gather(...))`.

**F-64 | sweep_watcher.py:273 | Unknown setup falls back to range_sweep threshold (9999) — permanent block | MED**
- **What:** `min_score = SETUP_TG_MIN_SCORE.get(setup, SETUP_TG_MIN_SCORE.get("range_sweep", 140))`. `SETUP_TG_MIN_SCORE["range_sweep"] = 9999`. Fallback for any unknown setup = 9999. New setup types added without updating this dict are silently blocked forever.
- **Severity:** MED
- **Fix:** Fallback to explicit `min_score = 140`, not via range_sweep reference.

---

## 3. Detailed Section: Pump-Detector (pump_detector.py)

### 3.1 Input Data Sources

| Source | API | Actual window | Labeled window |
|--------|-----|--------------|---------------|
| Bybit 5m klines | `/v5/market/kline`, limit=62 | ~310 min (5h10m) | "4h" in variable names |
| Bybit 5m OI | `/v5/market/open-interest`, limit=50 | ~250 min (4h10m) | "4h" |
| Bybit recent trades | `/v5/market/recent-trade`, limit=500 | 5 min or ALL (fallback) | "5m" (unstable) |
| Bybit 4H klines | `/v5/market/kline`, limit=20 | ~80h (3.3 days) | SLOW_DIST only |
| Funding history | screener.fetch_funding_history, limit=3 | ~24h (3 × 8h periods) | OK |
| liquidations.db | SQLite last 300s / 4h | real-time / 4h | OK |
| Binance Futures OI | `fapi.binance.com`, limit=5, 1h | ~4h | cross-exchange confirm |
| DEXscreener | `api.dexscreener.com` | h1/h6/h24 buckets | spot vol ratio |
| Bybit L/S ratio | `/v5/market/account-ratio`, limit=1 | 1h snapshot | OK |
| Fear & Greed | `alternative.me/fng` | cached 1h TTL | OK |
| CryptoPanic | `cryptopanic.com` | last 2h posts | **DEAD — never called** |

**OI vs Price window mismatch:** `oi_chg_4h` (250 min) vs `price_chg_4h` (310 min) are compared in the same "4h divergence" block. A 60-minute window mismatch at the edge case.

### 3.2 Pattern Trigger Conditions

#### Pattern 1 — SHORT_SQUEEZE (base +45 pts)
```
funding <= -0.03%                             AND
(oi_chg_30m >= 0.5% OR oi_chg_4h >= 5.0%)   AND
price_chg_30m ∈ [-8%, +3%]                   AND
cvd_pct >= -20%                              AND
vol_surge >= 0.7
```
**Weak conditions:**
- `price_chg_30m >= -8.0%` — price in free-fall at -7.9% still activates the squeeze pattern
- `cvd_pct >= -20%` — very wide; negative CVD of -19% does not signal hidden buying
- `vol_surge >= 0.7` — below-average volume activates; does not indicate accumulation

#### Pattern 2 — POST_PUMP_RUG (elif — mutually exclusive with P1)
```
price_chg_5h >= 8.0%    AND
funding >= 0.05%         AND
cvd_pct <= 10%           AND
oi_chg_30m <= 1.0%      AND
vol_surge >= 1.2
```
**Weak conditions:**
- `cvd_pct <= 10%` — allows +9% CVD during price pump (could be genuine demand)
- No BTC-trend gate in entry conditions; only a score modifier (-15 if BTC rising)

#### Pattern 7 — ATR_COIL
- `signal_type = "pump"` hardcoded. ATR coil is directionally neutral; always labelled pump is unvalidated.

#### Pattern 3 — SLOW_DIST (checked last — priority risk)
- Evaluated after Patterns 4, 5, 6, 7. If Volume Surge (P4) fires simultaneously with SLOW_DIST conditions, VOLUME_SURGE always wins.
- Zero-volume 4H candles silently pass the volume-stability gate (`v_ratio_3d == 0` → passes `<= 3.0` check).

### 3.3 Temporal Windows

| Variable | Index used | Actual duration | Label |
|---------|-----------|----------------|-------|
| `price_chg_30m` | `closes[-7]` | 35 min | "30m" |
| `price_chg_4h` | `opens[0]` (limit=62) | 310 min | "4h" |
| `oi_chg_30m` | `oi_hist[-7]` | 35 min | "30m" |
| `oi_chg_4h` | `oi_hist[-48]` | 240 min | "4h" ✓ |
| `vol_avg10` | `volumes[-12:-2]` | 10 bars (2–12 ago) | OK ✓ |
| ATR recent | `highs[-7:]` | 7 bars (35 min) | OK ✓ |
| SLOW_DIST | `c4[-18]` | 72h | "3 days" |

### 3.4 Score Construction and Cap Issue

**Pattern 1 theoretical maximum (before cap):**
- Base: +45
- Price holds: +15
- Sweep: +20
- CVD ≥20%: +15
- Short liquidations ≥$10K: +25
- Coinalyze shorts ≥$500K: +20
- Binance OI ≥2%: +15
- BTC bull: +10
- Basis ≤-0.5%: +20
- **Total: 185** → capped at 130

**After `scan_once` enrichments (bypass cap):**
- DEX adjustment: ±25
- OB skew: ±10
- **Effective final max: ~165** — uncapped

### 3.5 Likely False Positives and False Negatives

**False positives:**
1. P1 fires on standard 5m wick (0.2% below recent low triggers sweep flag — common noise)
2. P4 (VOLUME_SURGE) fires on high sell volume: `vol_surge >= 2.5` with no buy-side confirmation required in base conditions
3. P5 (CVD_BULL_DIV) fires on micro-volume markets: `cvd_pct >= 50%` at near-zero volume = 3 buys vs 1 sell
4. Quiet market CVD fallback: "CVDAll" spans hours, inflates CVD bonus same as 5m CVD

**False negatives:**
1. Funding = -0.02% (below -0.03% threshold) with sweep + bullish CVD → P1 doesn't activate
2. Slow OI accumulation over 12h: +3% at 4h window → misses `oi_chg_4h >= 5.0%` condition
3. Real sweep on 1H timeframe not detected because pump_detector only uses 5m bars

### 3.6 Cross-Module Conflicts

**pump_detector vs sweep_watcher.py:**
- Both modules can independently fire on the same sweep event with no system-level dedup
- pump_detector uses 5m bar + 0.2% threshold; sweep_watcher uses 1H bars — different sensitivity
- Each has its own cooldown dict; no shared cooldown prevents double-alerting

**pump_detector vs rug_detector.py:**
- Incompatible score scales: pump_detector 0–130+, rug_detector 0–100 (thresholds: WATCH=15, SUSPICIOUS=35, RUG_RISK=60)
- Pattern 2 (POST_PUMP_RUG) alerts without calling `rug_detector.analyze()` — rug-risk score is never included in rug_prep signals
- rug_detector integration happens only through boost_watcher DexScreener path

**pump_detector vs screener.py:**
- `detect_sweep`: pump_detector uses 5m klines; screener uses 1H with 3-candle lookback → same event, different classification
- Bad-hour sets differ: pump_detector blocks hour 14, screener doesn't
- CVD: pump_detector uses recent trades; screener uses kline-based 20h CVD — fundamentally different metrics with the same name

---

## 4. Severity Summary and Priority

### HIGH (fix immediately)

| # | File:line | Description |
|---|-----------|-------------|
| F-54 | orderbook_imbalance.py:183 | asks sorted descending — all OB metrics corrupt after first delta |
| F-23 | rca_engine.py:450–466 | rca_results.json no file lock — concurrent corruption |
| F-08 | outcome_tracker.py:363 / screener.py:5339 | TP1 multiplier mismatch (ATR×2.7 vs ×3.6) — inflated win rate |
| F-34 | telegram_alerts.py:581 | short_dist (54.9% WR best setup) silently dropped from watchlist |
| F-05 (#5) | sector_heat.py:177 / outcome_tracker.py:361 | Direction language mismatch (LONG vs ЛОНГ) — full chain broken |
| F-19 | screener.py:~5082 / telegram_alerts.py:780 | SECTOR_MAP defined twice with different assignments |
| F-20 | pump_detector.py:127 / calibration/ | pump_resolved.csv excluded from training model |
| F-21 | pump_detector.py:687 / outcome_tracker.py:451 | MAE sign inverted between two trackers |
| F-33 | rug_detector.py:108 | WHALE_TRANSFER_PCT × 1/100 — real threshold 0.3% not 30% |
| F-39 | screener.py (bos_fvg) | direction="none" passes all directional filters |
| F-40 | screener.py:4892, 4935 | EMA seed applied twice — all EMA trend signals biased |
| F-01 | screener.py:2991–3143 | RS_BTC double-scoring (non-exclusive if/if not elif) |
| F-02 | screener.py:81 | Hour 17 missing from HARD_BLOCK_HOURS despite WR=30.2% catastrophic |
| F-51 | pump_detector.py:118 | CONVICTION_MIN_SCORE=85 hardcoded temp hack — blocks valid signals |

### MED (sprint backlog)

F-03, F-04, F-05 (grade), F-06, F-07, F-11, F-13–F-15, F-17, F-24–F-28, F-29–F-30, F-35–F-38, F-41–F-50, F-52–F-53, F-57–F-64

### LOW (technical debt)

F-10, F-12, F-16, F-18, F-31–F-32, F-44–F-45, F-55–F-56, F-62

---

**Recommended fix order:**
1. `orderbook_imbalance.py:183` — asks sort (all OB metrics wrong)
2. `rca_engine.py:450` — add file lock to rca_results.json
3. `outcome_tracker.py:363` — sync TP1 to ATR×3.6
4. `telegram_alerts.py:581` — add short_dist to watchlist
5. `sector_heat.py:177` — normalize direction to Cyrillic or add English equivalence throughout chain
6. `screener.py` bos_fvg direction="none" — early exit guard
7. `screener.py:4892, 4935` — EMA double-seed fix
8. `screener.py:81` — add hour 17 to HARD_BLOCK_HOURS
9. `SECTOR_MAP` — consolidate to shared config.py
10. `pump_detector.py:118` — move CONVICTION_MIN_SCORE to .env

---

*End of WEAK_POINTS_AUDIT.md — AVEVA-54*
