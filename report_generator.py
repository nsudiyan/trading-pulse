"""
report_generator.py — Weekly / session CEO report from TRADE_LEARNINGS_DB.

Combines TLDB (patterns, rules, filters), resolved.csv (trade history),
and rca_results.json (error classification) into one Markdown report.

CLI:
  python3 report_generator.py                     # session report (all data), print to stdout
  python3 report_generator.py --mode weekly        # last 7 days of trades
  python3 report_generator.py --mode last-20       # last N trades
  python3 report_generator.py --out report.md      # write to file instead of stdout
  python3 report_generator.py --mode weekly --out weekly_report.md
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR  = Path(__file__).parent
DB_PATH   = BASE_DIR / "outcomes" / "trade_learnings_db.json"
RCA_PATH  = BASE_DIR / "outcomes" / "rca_results.json"
CSV_PATH  = BASE_DIR / "outcomes" / "resolved.csv"

BASELINE_WR = 46.05   # pattern engine overall baseline


# ─── loaders ─────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_csv(path: Path) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _parse_ts(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


# ─── filtering ────────────────────────────────────────────────────────────────

def _filter_rca(rca_all: list, mode: str) -> list:
    if mode == "session" or not mode:
        return rca_all

    cutoff = None
    if mode == "weekly":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    elif mode.startswith("last-"):
        try:
            n = int(mode.split("-")[1])
            return rca_all[-n:]
        except (IndexError, ValueError):
            return rca_all

    if cutoff:
        return [r for r in rca_all if _parse_ts(r.get("run_ts", "")) >= cutoff]
    return rca_all


def _filter_csv(rows: list, mode: str) -> list:
    if mode == "session" or not mode:
        return rows

    cutoff = None
    if mode == "weekly":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    elif mode.startswith("last-"):
        try:
            n = int(mode.split("-")[1])
            return rows[-n:]
        except (IndexError, ValueError):
            return rows

    if cutoff:
        out = []
        for row in rows:
            ts_str = row.get("run_ts", row.get("resolve_24h_ts", ""))
            if _parse_ts(ts_str) >= cutoff:
                out.append(row)
        return out
    return rows


# ─── analytics ────────────────────────────────────────────────────────────────

def _wr(wins: int, total: int) -> str:
    if not total:
        return "n/a"
    return f"{100*wins/total:.1f}%"


def _perf_by_setup(rca: list) -> dict:
    setups: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "flat": 0,
                                         "total": 0, "rr_sum": 0.0})
    for r in rca:
        s = r.get("setup", "unknown")
        cat = r.get("outcome_category", "UNKNOWN")
        setups[s]["total"] += 1
        if cat == "WIN":
            setups[s]["wins"] += 1
        elif cat == "LOSS":
            setups[s]["losses"] += 1
        else:
            setups[s]["flat"] += 1
        try:
            setups[s]["rr_sum"] += float(r.get("rr_ratio") or 0)
        except (TypeError, ValueError):
            pass
    return dict(setups)


def _error_category_breakdown(rca: list) -> Counter:
    from learning_generator import classify_error
    counts: Counter = Counter()
    for r in rca:
        if r.get("outcome_category") in ("LOSS", "FLAT"):
            counts[classify_error(r)] += 1
    return counts


# ─── report builder ───────────────────────────────────────────────────────────

def _section(title: str, level: int = 2) -> str:
    return f"\n{'#' * level} {title}\n"


def build_report(mode: str = "session") -> str:
    db  = _load_json(DB_PATH) or {}
    rca_all = _load_json(RCA_PATH) or []
    csv_all = _load_csv(CSV_PATH)

    rca = _filter_rca(rca_all, mode)
    csv_rows = _filter_csv(csv_all, mode)

    stats      = db.get("statistics", {})
    losing_pat = db.get("losing_patterns", [])
    winning_pat = db.get("winning_patterns", [])
    corr_rules  = db.get("active_correction_rules", [])
    prohibited  = db.get("prohibited_entry_conditions", [])
    conf_filt   = db.get("confirmation_filters", [])

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    db_ver  = db.get("db_version", "?")
    db_upd  = db.get("last_updated", "?")

    lines: list[str] = []

    # ── header ──────────────────────────────────────────────────────────────
    mode_label = {"session": "All-Time Session", "weekly": "Weekly"}.get(
        mode, mode.replace("last-", "Last ") + " Trades"
    )
    lines.append(f"# CEO Trading Report — {mode_label}")
    lines.append(f"\n*Generated: {now_str} | TLDB v{db_ver} | last updated: {db_upd}*\n")

    # ── executive summary ────────────────────────────────────────────────────
    lines.append(_section("Executive Summary"))

    n_total  = len(rca)
    n_wins   = sum(1 for r in rca if r.get("outcome_category") == "WIN")
    n_losses = sum(1 for r in rca if r.get("outcome_category") == "LOSS")
    n_flat   = sum(1 for r in rca if r.get("outcome_category") not in ("WIN", "LOSS"))

    wr_pct = 100 * n_wins / n_total if n_total else 0
    delta  = wr_pct - BASELINE_WR

    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Trades analyzed | {n_total} |")
    lines.append(f"| Wins | {n_wins} |")
    lines.append(f"| Losses | {n_losses} |")
    lines.append(f"| Flat / Neutral | {n_flat} |")
    lines.append(f"| Win Rate | **{wr_pct:.1f}%** |")
    lines.append(f"| vs Baseline ({BASELINE_WR}%) | {delta:+.1f} pp |")
    lines.append(f"| TLDB losing patterns | {len(losing_pat)} |")
    lines.append(f"| TLDB winning patterns | {len(winning_pat)} |")
    lines.append(f"| Active correction rules | {len(corr_rules)} |")
    lines.append(f"| Prohibited conditions | {len(prohibited)} |")
    lines.append(f"| Confirmation filters | {len(conf_filt)} |")

    # headline verdict
    lines.append("")
    if n_total == 0:
        lines.append("> No trades in the selected window.")
    elif wr_pct >= 55:
        lines.append(f"> **System performing above baseline** — WR {wr_pct:.1f}% ({delta:+.1f} pp vs {BASELINE_WR}% base).")
    elif wr_pct >= BASELINE_WR:
        lines.append(f"> System near baseline — WR {wr_pct:.1f}% ({delta:+.1f} pp). Monitor losing patterns closely.")
    else:
        lines.append(f"> **System underperforming baseline** — WR {wr_pct:.1f}% ({delta:+.1f} pp). Review HIGH-priority rules immediately.")

    # ── performance by setup ─────────────────────────────────────────────────
    lines.append(_section("Performance by Setup"))

    perf = _perf_by_setup(rca)
    if perf:
        lines.append("| Setup | N | Wins | Losses | Flat | WR% | PE Baseline |")
        lines.append("|-------|---|------|--------|------|-----|-------------|")
        by_setup = stats.get("by_pattern", {})
        for s, d in sorted(perf.items(), key=lambda x: -x[1]["total"]):
            pe = by_setup.get(s, {})
            pe_wr = f"{pe.get('pe_win_rate','?')}%" if pe else "—"
            lines.append(
                f"| {s} | {d['total']} | {d['wins']} | {d['losses']} | {d['flat']} "
                f"| {_wr(d['wins'], d['total'])} | {pe_wr} |"
            )
    else:
        lines.append("*No setup data in selected window.*")

    # ── error category breakdown ─────────────────────────────────────────────
    lines.append(_section("Error Category Breakdown (Losses/Flat)"))

    losses_flat = [r for r in rca if r.get("outcome_category") in ("LOSS", "FLAT")]
    if losses_flat:
        try:
            ec = _error_category_breakdown(rca)
            lines.append("| Error Category | Count |")
            lines.append("|---------------|-------|")
            for cat, cnt in ec.most_common():
                lines.append(f"| {cat} | {cnt} |")
        except Exception as e:
            lines.append(f"*Could not compute error categories: {e}*")
    else:
        lines.append("*No losses/flat trades in selected window.*")

    # ── top loss tags ────────────────────────────────────────────────────────
    lines.append(_section("Most Common Loss Tags"))

    loss_tags: Counter = Counter()
    for r in rca:
        if r.get("outcome_category") == "LOSS":
            for t in r.get("tags", []):
                loss_tags[t] += 1

    if loss_tags:
        lines.append("| Tag | Occurrences |")
        lines.append("|-----|-------------|")
        for tag, cnt in loss_tags.most_common(10):
            lines.append(f"| {tag} | {cnt} |")
    else:
        lines.append("*No loss tags in selected window.*")

    # ── winning patterns ─────────────────────────────────────────────────────
    lines.append(_section("Top Winning Patterns (TLDB)"))
    lines.append("*Conditions from pattern engine with WR significantly above baseline.*\n")

    strong_pos = [p for p in winning_pat if p.get("verdict") in ("STRONG_POSITIVE", "POSITIVE")]
    strong_pos.sort(key=lambda x: -x.get("win_rate", 0))

    if strong_pos:
        lines.append("| Condition | WR% | Δ pp | N | Confidence |")
        lines.append("|-----------|-----|------|---|------------|")
        for p in strong_pos[:10]:
            lines.append(
                f"| `{p['condition']}` | {p['win_rate']}% | {p['delta_pp']:+.1f} "
                f"| {p['frequency']} | {p['confidence']} |"
            )
    else:
        lines.append("*No winning patterns recorded yet.*")

    # ── losing patterns ───────────────────────────────────────────────────────
    lines.append(_section("Top Losing Patterns (TLDB)"))
    lines.append("*High-confidence conditions to avoid entering.*\n")

    strong_neg = [p for p in losing_pat if p.get("verdict") in ("STRONG_NEGATIVE",)]
    strong_neg.sort(key=lambda x: x.get("win_rate", 99))

    if strong_neg:
        lines.append("| Condition | WR% | Δ pp | N | Confidence |")
        lines.append("|-----------|-----|------|---|------------|")
        for p in strong_neg[:10]:
            lines.append(
                f"| `{p['condition']}` | {p['win_rate']}% | {p['delta_pp']:+.1f} "
                f"| {p['frequency']} | {p['confidence']} |"
            )
    else:
        lines.append("*No strong negative patterns recorded.*")

    # ── active correction rules ───────────────────────────────────────────────
    lines.append(_section("Active Correction Rules"))

    by_priority: dict = defaultdict(list)
    for r in corr_rules:
        by_priority[r.get("priority", "LOW")].append(r)

    for prio in ("HIGH", "MEDIUM", "LOW"):
        rules = by_priority.get(prio, [])
        if not rules:
            continue
        lines.append(_section(f"{prio} Priority ({len(rules)})", level=3))
        for r in rules:
            lines.append(f"- **{r.get('id','?')}**: {r.get('rule','')}")

    if not corr_rules:
        lines.append("*No correction rules active.*")

    # ── prohibited conditions ─────────────────────────────────────────────────
    lines.append(_section("Prohibited Entry Conditions"))
    lines.append("*These conditions are blocked at screener gate.*\n")

    if prohibited:
        lines.append("| ID | Condition | Reason |")
        lines.append("|----|-----------|--------|")
        for p in prohibited[:15]:
            lines.append(f"| {p.get('id','?')} | `{p.get('condition','')}` | {p.get('reason','')} |")
    else:
        lines.append("*None active.*")

    # ── confirmation filters ──────────────────────────────────────────────────
    lines.append(_section("Top Confirmation Filters"))
    lines.append("*Conditions that improve signal quality when present.*\n")

    if conf_filt:
        lines.append("| ID | Filter | Description |")
        lines.append("|----|--------|-------------|")
        for f in conf_filt[:10]:
            lines.append(f"| {f.get('id','?')} | `{f.get('filter','')}` | {f.get('description','')} |")
    else:
        lines.append("*None active.*")

    # ── recent trade detail ───────────────────────────────────────────────────
    if rca:
        lines.append(_section("Recent Trades (RCA)"))
        recent = sorted(rca, key=lambda r: r.get("run_ts", ""), reverse=True)[:10]
        lines.append("| Symbol | Setup | Direction | Score | Outcome | R:R | Primary Cause |")
        lines.append("|--------|-------|-----------|-------|---------|-----|---------------|")
        for r in recent:
            cause = (r.get("primary_cause") or "")[:55]
            lines.append(
                f"| {r.get('symbol','')} | {r.get('setup','')} | {r.get('direction','')} "
                f"| {r.get('score','?')} | {r.get('outcome','')} | {r.get('rr_ratio','?')} "
                f"| {cause} |"
            )

    # ── recommendations ────────────────────────────────────────────────────────
    lines.append(_section("Recommendations"))

    recs: list[str] = []

    # WR-based recommendation
    if n_total > 0 and wr_pct < BASELINE_WR - 5:
        recs.append(
            "**Priority 1 — Address underperformance**: WR is "
            f"{abs(delta):.1f} pp below baseline. Review HIGH-priority correction rules first."
        )

    # top losing pattern recommendation
    top_neg = [p for p in strong_neg if p.get("confidence") in ("high", "medium")][:3]
    if top_neg:
        worst = top_neg[0]
        recs.append(
            f"**Avoid**: `{worst['condition']}` has WR={worst['win_rate']}% "
            f"(Δ{worst['delta_pp']:+.1f} pp, n={worst['frequency']}). "
            "This combination is the single strongest predictor of losing trades."
        )

    # top winning pattern recommendation
    top_pos = [p for p in strong_pos if p.get("confidence") in ("high", "medium")][:3]
    if top_pos:
        best = top_pos[0]
        recs.append(
            f"**Prioritize**: `{best['condition']}` — WR={best['win_rate']}% "
            f"(Δ{best['delta_pp']:+.1f} pp, n={best['frequency']}). "
            "Lean into setups where this condition holds."
        )

    # high-priority rules
    high_rules = by_priority.get("HIGH", [])
    if high_rules:
        recs.append(
            f"**Implement HIGH-priority rules**: {len(high_rules)} rules await screener integration. "
            "Implementing these is the highest-ROI code change available."
        )

    # session timing
    session_neg = [p for p in strong_neg if "session_ny" in p.get("condition", "") and
                   p.get("confidence") in ("high", "medium")]
    if session_neg:
        recs.append(
            "**Session filter**: Multiple STRONG_NEGATIVE patterns involve `session_ny`. "
            "Consider deprioritizing or skipping entries during NY session."
        )

    # funding regime warning
    fund_neg = [p for p in strong_neg if "fund_extreme" in p.get("condition", "") and
                p.get("confidence") == "high"]
    if fund_neg:
        recs.append(
            "**Funding regime**: Extreme funding (positive or negative) appears in "
            f"{len(fund_neg)} STRONG_NEGATIVE patterns. Add funding gate to screener."
        )

    if not recs:
        recs.append("System is performing within expected range. Continue monitoring pattern confidence levels.")

    for i, rec in enumerate(recs, 1):
        lines.append(f"{i}. {rec}\n")

    # ── footer ────────────────────────────────────────────────────────────────
    lines.append("\n---")
    lines.append(f"*Report mode: `{mode}` | TLDB source: `{DB_PATH.name}` | "
                 f"RCA source: `{RCA_PATH.name}`*")

    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Generate CEO weekly/session report from TRADE_LEARNINGS_DB"
    )
    ap.add_argument(
        "--mode",
        default="session",
        help="Report window: 'session' (all), 'weekly' (7 days), 'last-N' (last N trades). "
             "Default: session",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Write report to this file path (default: print to stdout)",
    )
    args = ap.parse_args()

    report = build_report(mode=args.mode)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(report, encoding="utf-8")
        print(f"Report written to {out_path}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
