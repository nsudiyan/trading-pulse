"""swing_chart.py — H4 chart with Hadiukov Swing annotations (СЕТАП 6)."""

from __future__ import annotations
import io
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np


# ─── colours ─────────────────────────────────────────────────────────────────
BG        = "#0d1117"
GRID      = "#21262d"
SPINE     = "#30363d"
TXT       = "#e6edf3"
TXT_DIM   = "#8b949e"
BULL_C    = "#26a69a"
BEAR_C    = "#ef5350"
POINT_A   = "#ffd700"
ENTRY_L   = "#00e676"   # long entry
ENTRY_S   = "#ff4444"   # short entry
TP_L      = "#00e676"
TP_S      = "#ff4444"
SL_C      = "#ff9800"
IMB_BULL  = "#163b2e"
IMB_BEAR  = "#3b1616"
PRICE_L   = "#ffffff"


def generate_swing_chart(symbol: str, r: dict) -> Optional[bytes]:
    """
    Generates H4 candlestick chart for a СЕТАП 6 (Hadiukov Swing) signal.

    Overlays:
      - All H4 FVG imbalance zones (shaded)
      - Point A zone (W FVG or W fractal) — yellow
      - H4 entry FVG — green/red border
      - Target (TP1) — dashed line
      - Stop-loss fractal — dotted line
      - Current price — thin white line

    Returns PNG bytes, or None on any failure (non-critical).
    """
    try:
        import screener as sc
    except Exception:
        return None

    # Re-fetch H4 OHLCV (last 80 candles; [-1] is live/unclosed, skip it)
    try:
        op4, hi4, lo4, cl4, _ = sc.fetch_klines(symbol, "240", limit=82)
    except Exception:
        return None

    op4 = op4[:-1]; hi4 = hi4[:-1]; lo4 = lo4[:-1]; cl4 = cl4[:-1]

    # Use last N completed candles for the chart
    N = min(len(cl4), 72)
    op = np.array(op4[-N:])
    hi = np.array(hi4[-N:])
    lo = np.array(lo4[-N:])
    cl = np.array(cl4[-N:])

    # Re-detect H4 FVGs (for imbalance overlay); use full array for context
    fvgs_h4 = sc.detect_fvg(hi4, lo4, cl4, lookback=60)

    direction   = r.get("swing_dir", "long")
    phase       = r.get("swing_phase", "—")
    frac_sl     = r.get("swing_frac_sl")
    score       = r.get("score", 0)
    price       = r.get("price", float(cl[-1]))
    chart_data  = r.get("swing_chart_data") or {}

    try:
        plan       = sc.build_trade_plan(r)
        tp1        = plan.get("tp1")
        entry_p    = plan.get("entry")
    except Exception:
        tp1 = None; entry_p = price

    xs = np.arange(N)

    # ── Figure ────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(13, 7), facecolor=BG)
    ax.set_facecolor(BG)
    ax.tick_params(colors=TXT_DIM, labelsize=8)
    for sp in ax.spines.values():
        sp.set_edgecolor(SPINE)
    ax.grid(axis="y", color=GRID, linewidth=0.5, linestyle="--", zorder=0)
    ax.set_xlim(-1, N + 2)

    # ── H4 FVG imbalance zones (background layer) ─────────────────────────────
    for fvg in fvgs_h4:
        color = IMB_BULL if fvg["type"] == "bull" else IMB_BEAR
        ax.axhspan(fvg["bottom"], fvg["top"], alpha=0.5, color=color, zorder=1)

    # ── Point A (W FVG zone or W fractal level) ───────────────────────────────
    pt_a_top = chart_data.get("point_a_top")
    pt_a_bot = chart_data.get("point_a_bot")
    if pt_a_top and pt_a_bot:
        ax.axhspan(pt_a_bot, pt_a_top, alpha=0.30, color=POINT_A, zorder=2)
        ax.axhline(pt_a_top, color=POINT_A, linewidth=1.0, linestyle="--", alpha=0.85, zorder=2)
        ax.axhline(pt_a_bot, color=POINT_A, linewidth=1.0, linestyle="--", alpha=0.85, zorder=2)
        mid = (pt_a_top + pt_a_bot) / 2
        ax.text(1.5, mid, "А", color=POINT_A, fontsize=12, fontweight="bold",
                va="center", zorder=6, bbox=dict(facecolor=BG, alpha=0.5, pad=1, edgecolor="none"))
    elif pt_a_top:
        ax.axhline(pt_a_top, color=POINT_A, linewidth=1.8, linestyle="-.",
                   alpha=0.85, zorder=2)
        ax.text(1.5, pt_a_top, "А", color=POINT_A, fontsize=12, fontweight="bold",
                va="bottom", zorder=6, bbox=dict(facecolor=BG, alpha=0.5, pad=1, edgecolor="none"))

    # ── H4 entry FVG (highlighted) ───────────────────────────────────────────
    ef_top = chart_data.get("entry_fvg_top")
    ef_bot = chart_data.get("entry_fvg_bot")
    ec = ENTRY_L if direction == "long" else ENTRY_S
    if ef_top and ef_bot:
        ax.axhspan(ef_bot, ef_top, alpha=0.38, color=ec, zorder=3)
        ax.axhline(ef_top, color=ec, linewidth=1.5, linestyle="-", alpha=0.95, zorder=3)
        ax.axhline(ef_bot, color=ec, linewidth=1.5, linestyle="-", alpha=0.95, zorder=3)
        mid_e = (ef_top + ef_bot) / 2
        ax.text(N - 2, mid_e, "IMB\nВХОД", color=ec, fontsize=8, fontweight="bold",
                ha="right", va="center", zorder=6,
                bbox=dict(facecolor=BG, alpha=0.6, pad=2, edgecolor="none"))

    # ── Target line ───────────────────────────────────────────────────────────
    tp_color = TP_L if direction == "long" else TP_S
    if tp1:
        ax.axhline(tp1, color=tp_color, linewidth=1.5, linestyle="--", alpha=0.9, zorder=3)
        ax.text(N + 0.5, tp1, f"TP  {tp1:.5g}", color=tp_color,
                fontsize=8, va="center", fontweight="bold", zorder=6)

    # ── Stop-loss fractal ────────────────────────────────────────────────────
    if frac_sl:
        ax.axhline(frac_sl, color=SL_C, linewidth=1.2, linestyle=":", alpha=0.9, zorder=3)
        ax.text(N + 0.5, frac_sl, f"SL  {frac_sl:.5g}", color=SL_C,
                fontsize=8, va="center", fontweight="bold", zorder=6)

    # ── Candlesticks (drawn on top) ───────────────────────────────────────────
    W = 0.55
    for i in range(N):
        o, h, l, c = op[i], hi[i], lo[i], cl[i]
        bull = c >= o
        col = BULL_C if bull else BEAR_C
        body_lo, body_hi = (o, c) if bull else (c, o)
        ax.add_patch(mpatches.Rectangle(
            (i - W / 2, body_lo), W, max(body_hi - body_lo, price * 0.0001),
            color=col, zorder=4
        ))
        ax.plot([i, i], [l, h], color=col, linewidth=0.8, zorder=4)

    # ── Current price line ────────────────────────────────────────────────────
    ax.axhline(price, color=PRICE_L, linewidth=0.7, linestyle="-", alpha=0.45, zorder=3)
    ax.text(N + 0.5, price, f"{price:.5g}", color=PRICE_L,
            fontsize=8, va="center", zorder=6)

    # ── Title & annotation ────────────────────────────────────────────────────
    phase_map = {
        "trend_bull":      "Тренд ↑",
        "trend_bear":      "Тренд ↓",
        "correction_bull": "Коррекция ↑",
        "correction_bear": "Коррекция ↓",
        "range":           "Рейндж",
    }
    dir_txt   = "▲ ЛОНГ" if direction == "long" else "▼ ШОРТ"
    dir_color = BULL_C   if direction == "long" else BEAR_C
    ph_label  = phase_map.get(phase, phase)

    ax.set_title(
        f"{symbol}  H4  ·  СЕТАП 6 — Hadiukov Swing",
        color=TXT, fontsize=13, fontweight="bold", pad=10,
    )
    ax.text(
        0.01, 0.97,
        f"{dir_txt}   {ph_label}   score={score}",
        transform=ax.transAxes, color=dir_color,
        fontsize=11, fontweight="bold", va="top", zorder=7,
    )

    # ── Legend chips ─────────────────────────────────────────────────────────
    legend_items = [
        mpatches.Patch(color=POINT_A, alpha=0.7, label="Point A (W FVG / фрактал)"),
        mpatches.Patch(color=ec,      alpha=0.7, label="Вход (H4 IMB)"),
    ]
    if fvgs_h4:
        legend_items += [
            mpatches.Patch(color=IMB_BULL, alpha=0.8, label="Бычий IMB"),
            mpatches.Patch(color=IMB_BEAR, alpha=0.8, label="Медвежий IMB"),
        ]
    ax.legend(handles=legend_items, loc="lower left",
              facecolor="#161b22", edgecolor=SPINE,
              labelcolor=TXT_DIM, fontsize=7.5, framealpha=0.85)

    ax.set_xticks([])

    fig.tight_layout(pad=1.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
