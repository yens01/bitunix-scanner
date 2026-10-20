#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC／ETH 日內規劃圖：15m 收盤＋今日高低／Fibo。
回傳 PNG bytes；無 matplotlib 時回 None。
"""

from __future__ import annotations

import io
from typing import Any


def _px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4g}"
    return f"{v:.6g}"


def _span_hi_lo(plan: Any) -> tuple[float, float] | None:
    lo = float(getattr(plan, "plan_day_lo", 0) or 0)
    hi = float(getattr(plan, "plan_day_hi", 0) or 0)
    if lo > 0 and hi > lo:
        return lo, hi
    lo = float(getattr(plan, "zone_lo", 0) or 0)
    hi = float(getattr(plan, "zone_hi", 0) or 0)
    if lo > 0 and hi > lo:
        return lo, hi
    return None


def _draw_one(
    ax: Any,
    plan: Any,
    candles: list[Any],
) -> None:
    symbol = str(getattr(plan, "symbol", "") or "")
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    xs = list(range(len(closes)))

    # 簡易陰陽棒（影線＋實體）
    for i, c in enumerate(candles):
        o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
        up = cl >= o
        col = "#26a69a" if up else "#ef5350"
        ax.plot([i, i], [l, h], color=col, linewidth=0.7, solid_capstyle="round")
        body_lo, body_hi = (o, cl) if up else (cl, o)
        if abs(body_hi - body_lo) < 1e-12:
            ax.plot([i - 0.3, i + 0.3], [cl, cl], color=col, linewidth=1.2)
        else:
            ax.vlines(i, body_lo, body_hi, colors=col, linewidth=2.4, alpha=0.9)

    ax.plot(xs, closes, color="#90caf9", linewidth=0.9, alpha=0.55, zorder=1)

    span = _span_hi_lo(plan)
    if span:
        lo, hi = span
        w = hi - lo
        levels = [
            ("0 Hi", hi, "#ff7043", 1.4),
            ("0.382", hi - w * 0.382, "#ffd54f", 1.0),
            ("0.5", hi - w * 0.5, "#fff176", 1.1),
            ("0.618", hi - w * 0.618, "#ffd54f", 1.0),
            ("1 Lo", lo, "#42a5f5", 1.4),
        ]
        for name, y, col, lw in levels:
            ax.axhline(y, color=col, linewidth=lw, linestyle="--", alpha=0.85)
            ax.text(
                len(xs) - 1,
                y,
                f" {name} {_px(y)}",
                color=col,
                fontsize=8,
                va="center",
                ha="left",
                clip_on=False,
            )

    # 操作箱（若裁近端且與今日高低不同）
    zlo = float(getattr(plan, "zone_lo", 0) or 0)
    zhi = float(getattr(plan, "zone_hi", 0) or 0)
    if (
        span
        and zlo > 0
        and zhi > zlo
        and (
            abs(zlo - span[0]) / span[0] > 0.0005
            or abs(zhi - span[1]) / span[1] > 0.0005
        )
    ):
        ax.axhspan(zlo, zhi, color="#7e57c2", alpha=0.12, zorder=0)
        ax.text(
            0,
            (zlo + zhi) / 2,
            " trade box ",
            color="#b39ddb",
            fontsize=7,
            va="center",
        )

    last = closes[-1] if closes else 0
    title = f"{symbol}  15m  last {_px(last)}"
    struct = str(getattr(plan, "structure", "") or "")
    if struct:
        title += f"  ·  {struct}"
    ax.set_title(title, color="#eceff1", fontsize=11, loc="left", pad=8)
    ax.set_facecolor("#1a1d23")
    ax.tick_params(colors="#90a4ae", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#37474f")
    ax.set_xlim(-1, max(len(xs) - 1, 1) + max(8, len(xs) // 12))
    if highs and lows:
        pad = (max(highs) - min(lows)) * 0.06 or 1
        y0, y1 = min(lows) - pad, max(highs) + pad
        if span:
            y0 = min(y0, span[0] - pad * 0.3)
            y1 = max(y1, span[1] + pad * 0.3)
        ax.set_ylim(y0, y1)
    ax.grid(True, color="#263238", linewidth=0.5, alpha=0.8)


def render_session_plan_chart(
    plans: list[Any],
    candles_by_symbol: dict[str, list[Any]],
) -> bytes | None:
    """
    畫 BTC／ETH（或傳入清單）日內規劃圖。
    candles_by_symbol: symbol → Candle 列表（舊→新）。
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    usable = [
        p
        for p in plans
        if candles_by_symbol.get(str(getattr(p, "symbol", "") or ""))
    ]
    if not usable:
        return None

    n = len(usable)
    fig, axes = plt.subplots(
        n,
        1,
        figsize=(10.5, 3.6 * n),
        dpi=140,
        squeeze=False,
        facecolor="#12151a",
    )
    fig.suptitle(
        "BTC / ETH Intraday Plan  ·  Fibo from Day High/Low (0=High)",
        color="#eceff1",
        fontsize=12,
        y=0.995,
    )
    for i, plan in enumerate(usable):
        sym = str(getattr(plan, "symbol", "") or "")
        _draw_one(axes[i][0], plan, candles_by_symbol[sym])

    fig.tight_layout(rect=(0, 0, 0.92, 0.97))
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)
