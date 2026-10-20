#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC／ETH 日內規劃圖：15m（進出）＋4H（結構）＋今日高低／Fibo。
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
    *,
    interval: str,
    show_trade_box: bool = False,
    label_fib: bool = True,
) -> None:
    symbol = str(getattr(plan, "symbol", "") or "")
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    xs = list(range(len(closes)))

    # 簡易陰陽棒（影線＋實體）
    body_w = 2.2 if interval.lower() in {"4h", "1h"} else 2.4
    wick_w = 0.9 if interval.lower() in {"4h", "1h"} else 0.7
    for i, c in enumerate(candles):
        o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
        up = cl >= o
        col = "#26a69a" if up else "#ef5350"
        ax.plot([i, i], [l, h], color=col, linewidth=wick_w, solid_capstyle="round")
        body_lo, body_hi = (o, cl) if up else (cl, o)
        if abs(body_hi - body_lo) < 1e-12:
            ax.plot([i - 0.3, i + 0.3], [cl, cl], color=col, linewidth=1.2)
        else:
            ax.vlines(i, body_lo, body_hi, colors=col, linewidth=body_w, alpha=0.9)

    ax.plot(xs, closes, color="#90caf9", linewidth=0.9, alpha=0.45, zorder=1)

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
            if label_fib:
                ax.text(
                    len(xs) - 1,
                    y,
                    f" {name} {_px(y)}",
                    color=col,
                    fontsize=7.5,
                    va="center",
                    ha="left",
                    clip_on=False,
                )

    # 操作箱只畫在 15m（進出用）
    zlo = float(getattr(plan, "zone_lo", 0) or 0)
    zhi = float(getattr(plan, "zone_hi", 0) or 0)
    if (
        show_trade_box
        and span
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
    role = "entry" if interval.lower() in {"15m", "5m"} else "structure"
    title = f"{symbol}  {interval}  ({role})  last {_px(last)}"
    struct = str(getattr(plan, "structure", "") or "")
    if struct and interval.lower() in {"4h", "1h"}:
        title += f"  ·  {struct}"
    ax.set_title(title, color="#eceff1", fontsize=10, loc="left", pad=6)
    ax.set_facecolor("#1a1d23")
    ax.tick_params(colors="#90a4ae", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#37474f")
    ax.set_xlim(-1, max(len(xs) - 1, 1) + max(6, len(xs) // 14))
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
    candles_by_symbol: dict[str, list[Any]] | dict[str, dict[str, list[Any]]],
    *,
    htf_interval: str = "4h",
) -> bytes | None:
    """
    每個幣：左 15m（進出）｜右 4H（結構）。
    candles_by_symbol 支援：
      - 舊格式 symbol → 15m candles
      - 新格式 symbol → {"15m": [...], "4h": [...]}
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    def _pack(sym: str) -> dict[str, list[Any]]:
        raw = candles_by_symbol.get(sym)
        if not raw:
            return {}
        if isinstance(raw, dict):
            return {str(k).lower(): v for k, v in raw.items() if v}
        return {"15m": raw}

    usable: list[tuple[Any, dict[str, list[Any]]]] = []
    for p in plans:
        sym = str(getattr(p, "symbol", "") or "")
        pack = _pack(sym)
        if pack.get("15m") or pack.get(htf_interval.lower()):
            usable.append((p, pack))
    if not usable:
        return None

    n = len(usable)
    fig, axes = plt.subplots(
        n,
        2,
        figsize=(14.5, 3.5 * n),
        dpi=130,
        squeeze=False,
        facecolor="#12151a",
    )
    fig.suptitle(
        f"BTC / ETH Plan  ·  15m entry + {htf_interval} structure  ·  "
        "Fibo = Day High/Low (0=High)",
        color="#eceff1",
        fontsize=11,
        y=0.995,
    )
    for i, (plan, pack) in enumerate(usable):
        c15 = pack.get("15m") or []
        c_htf = pack.get(htf_interval.lower()) or []
        if c15:
            _draw_one(
                axes[i][0],
                plan,
                c15,
                interval="15m",
                show_trade_box=True,
                label_fib=True,
            )
        else:
            axes[i][0].set_facecolor("#1a1d23")
            axes[i][0].text(0.5, 0.5, "no 15m", ha="center", color="#90a4ae")
            axes[i][0].set_xticks([])
            axes[i][0].set_yticks([])
        if c_htf:
            _draw_one(
                axes[i][1],
                plan,
                c_htf,
                interval=htf_interval,
                show_trade_box=False,
                label_fib=True,
            )
        else:
            axes[i][1].set_facecolor("#1a1d23")
            axes[i][1].text(0.5, 0.5, f"no {htf_interval}", ha="center", color="#90a4ae")
            axes[i][1].set_xticks([])
            axes[i][1].set_yticks([])

    fig.tight_layout(rect=(0, 0, 0.93, 0.97))
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)
