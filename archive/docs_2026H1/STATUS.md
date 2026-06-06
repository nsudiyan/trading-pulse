# System Status

*Last updated: 2026-04-25 — AVEVA-35 / Phase 1 complete*

## Phase 1 — COMPLETE ✅

All 4 Phase 1 quick wins from ROADMAP.md are implemented and verified (AVEVA-31 + AVEVA-35).

| Task | Status | Notes |
|------|--------|-------|
| T1.1 — k1w / k15m kline fetches | ✅ Done | screener.py:4804–4805; max_workers 8→10 |
| T1.2 — SHORT LR calibration | ✅ Done | AUC=0.4844; score>150→−8.8 pts deployed |
| T1.3 — detect_* unit tests | ✅ Done | 24/24 pass; tests/test_detect.py |
| T1.4 — WAIT misclassification fix | ✅ Done | outcome_tracker.py; ЖДАТЬ→FLAT |

**Test suite:** `pytest tests/test_detect.py` → 24 passed in 0.08s

## Phase 2 — NEXT (pending CEO answers on 3 open questions)

All 8 Phase 2 tasks (T2.1–T2.8) use already-fetched data; no new API dependencies.

| Task | Notes |
|------|-------|
| T2.1 — d_ctx / h4_ctx structs | Wrap inline daily/4H logic in screener.py:1773 |
| T2.2 — compute_weekly_context() | Consumes k1w from T1.1 |
| T2.3 — compute_15m_context() | Consumes k15m from T1.1 |
| T2.4 — calc_mtf_grade() | Core MTF grading per MTF_ENGINE_DESIGN.md §4.3 |
| T2.5 — X-grade hard block | 1W+1D both opposing → suppress |
| T2.6 — C-grade threshold | mtf_grade C + score < 130 → suppress |
| T2.7 — MTF line in TG templates | 📊 MTF [⭐A+] line in all 5 alert formats |
| T2.8 — mtf_grade in outcome tracker | pending.json + resolved.csv columns |

**Open questions before Phase 2 execution (from ROADMAP.md):**
1. C-grade threshold: 130 for all setups, or 140 for Breakout?
2. X-block rollout: immediate or 1-week paper mode?
3. Deployment: single commit or sequential small PRs (T2.1→T2.4)?

## Recent Completions

| Task | Status | Date |
|------|--------|------|
| Phase 1 complete (AVEVA-31 + AVEVA-35) | ✅ Done | 2026-04-25 |
| TRADE_ANALYSIS.md — full trade analysis (2289 signals) | ✅ Done | 2026-04-25 |
| calibration/model_report.md — logistic regression model | ✅ Done | prev |
| calibration/signal_weights.json | ✅ Done | prev |
| sweep_watcher.py — WebSocket sweep listener | ✅ Done | prev |
| CHoCH↑1H tier promotion (AVEC-10) | ✅ Done | prev |

## Calibration State

| Model | File | AUC | Status |
|-------|------|-----|--------|
| LONG LR (9 features) | calibration/signal_weights.json | 0.59 | Active |
| SHORT LR (1 weight) | calibration/signal_weights_short.json | 0.4844 | Deployed (partial) |
| Per-setup LR (4 models) | — | — | Phase 3 (T3.4) |

## Key Findings from TRADE_ANALYSIS.md

- **Best setup**: `bos_fvg` — 55.0% WR (n=806)
- **Worst setup**: `short_dist` — 45.1% WR, broken R:R
- **Best signal combo**: `bos_fvg` + CHoCH↑1H — 77.8% WR
- **ЖДАТЬ signals**: 82.7% WR — should be primary entry trigger
- **Avoid**: UTC 17–19, `mtf_bear=1` on LONG, score > 160 in isolation
