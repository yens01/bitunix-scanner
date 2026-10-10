#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
實盤風控輔助（不下單，只算倉位／擋超量）
- 固定 1R：每筆風險 = 帳戶 × risk_pct
- 最大同向未結倉：超過則不推／不記 YES
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from signal_tracker import JournalEntry, load_journal


@dataclass
class RiskConfig:
    account_usdt: float = 1000.0
    risk_pct_per_r: float = 1.0   # 每筆虧損上限占帳戶 %
    max_same_side: int = 2        # 同向最多未結倉
    max_total_open: int = 4       # 總未結倉上限


@dataclass
class SizePlan:
    risk_usdt: float
    stop_pct: float
    notional_usdt: float
    qty: float
    leverage_cap: float  # 名義／帳戶，僅參考


def load_risk_config() -> RiskConfig:
    def _f(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        raw = os.environ.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    return RiskConfig(
        account_usdt=max(_f("RISK_ACCOUNT_USDT", 1000.0), 1.0),
        risk_pct_per_r=max(_f("RISK_PCT_PER_R", 1.0), 0.05),
        max_same_side=max(_i("RISK_MAX_SAME_SIDE", 2), 1),
        max_total_open=max(_i("RISK_MAX_TOTAL_OPEN", 4), 1),
    )


def calc_1r_size(entry: float, stop: float, cfg: RiskConfig | None = None) -> SizePlan | None:
    """依停損距離反推名義價值／數量，使停損 ≈ 1R。"""
    cfg = cfg or load_risk_config()
    if entry <= 0 or stop <= 0 or entry == stop:
        return None
    stop_pct = abs(entry - stop) / entry
    if stop_pct <= 0:
        return None
    risk_usdt = cfg.account_usdt * cfg.risk_pct_per_r / 100.0
    notional = risk_usdt / stop_pct
    qty = notional / entry
    lev = notional / cfg.account_usdt if cfg.account_usdt else 0.0
    return SizePlan(
        risk_usdt=risk_usdt,
        stop_pct=stop_pct * 100,
        notional_usdt=notional,
        qty=qty,
        leverage_cap=lev,
    )


def format_risk_line(entry: float, stop: float, cfg: RiskConfig | None = None) -> str:
    cfg = cfg or load_risk_config()
    sp = calc_1r_size(entry, stop, cfg)
    if not sp:
        return ""
    return (
        f"風控1R: 風險≈{sp.risk_usdt:.2f}U "
        f"（帳戶{cfg.account_usdt:.0f}U×{cfg.risk_pct_per_r:.2f}%）｜"
        f"名義≈{sp.notional_usdt:.0f}U｜約{sp.leverage_cap:.1f}x名義/帳戶"
    )


def count_open_by_side(rows: list[JournalEntry] | None = None) -> dict[str, int]:
    rows = rows if rows is not None else load_journal()
    out = {"long": 0, "short": 0}
    for r in rows:
        if r.status == "open" and r.side in out:
            out[r.side] += 1
    return out


def filter_yes_by_risk(
    setups: list[Any],
    *,
    cfg: RiskConfig | None = None,
    journal_rows: list[JournalEntry] | None = None,
) -> tuple[list[Any], list[str]]:
    """
    依同向／總倉上限過濾 YES。
    回傳 (可推清單, 擋下原因)。
    """
    cfg = cfg or load_risk_config()
    rows = journal_rows if journal_rows is not None else load_journal()
    counts = count_open_by_side(rows)
    total = counts["long"] + counts["short"]
    allowed: list[Any] = []
    blocked: list[str] = []
    # 本波內也要累加，避免一次推爆同向
    wave = {"long": 0, "short": 0}

    for s in setups:
        side = getattr(s, "side", "none")
        sym = getattr(s, "symbol", "?")
        if side not in ("long", "short"):
            blocked.append(f"{sym}(無方向)")
            continue
        side_n = counts[side] + wave[side]
        tot_n = total + wave["long"] + wave["short"]
        if side_n >= cfg.max_same_side:
            blocked.append(f"{sym}(同向已滿{cfg.max_same_side})")
            continue
        if tot_n >= cfg.max_total_open:
            blocked.append(f"{sym}(總倉已滿{cfg.max_total_open})")
            continue
        allowed.append(s)
        wave[side] += 1
    return allowed, blocked
