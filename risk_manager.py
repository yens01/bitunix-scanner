#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
實盤風控輔助（不下單，只算倉位／擋超量）
- 固定 1R：每筆風險 = 帳戶 × risk_pct
- 最大同向未結倉：超過則不推／不記 YES
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from typing import Any

from signal_tracker import JournalEntry, load_journal


@dataclass
class RiskConfig:
    account_usdt: float = 1000.0
    risk_pct_per_r: float = 1.0   # 每筆虧損上限占帳戶 %
    max_same_side: int = 2        # 同向最多未結倉
    max_total_open: int = 4       # 總未結倉上限
    max_leverage: float = 3.0     # 名目/帳戶上限（不是交易所設定槓桿）
    max_notional_usdt: float = 3000.0
    fee_pct_roundtrip: float = 0.12  # 進出場總手續費百分比（可依費率調整）
    slippage_pct_roundtrip: float = 0.10  # 進出場總滑價百分比
    min_stop_pct: float = 0.10  # 最小停損距離百分比


@dataclass
class SizePlan:
    risk_usdt: float
    stop_pct: float
    notional_usdt: float
    qty: float
    leverage_cap: float  # 名義／帳戶，僅參考
    estimated_cost_usdt: float = 0.0
    estimated_loss_usdt: float = 0.0


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
        max_leverage=max(_f("RISK_MAX_LEVERAGE", 3.0), 0.1),
        max_notional_usdt=max(_f("RISK_MAX_NOTIONAL_USDT", 3000.0), 1.0),
        fee_pct_roundtrip=max(_f("RISK_FEE_PCT_ROUNDTRIP", 0.12), 0.0),
        slippage_pct_roundtrip=max(_f("RISK_SLIPPAGE_PCT_ROUNDTRIP", 0.10), 0.0),
        min_stop_pct=max(_f("RISK_MIN_STOP_PCT", 0.10), 0.01),
    )


def calc_1r_size(entry: float, stop: float, cfg: RiskConfig | None = None) -> SizePlan | None:
    """Cost-aware 1R size. Reject invalid/tiny stops; never exceed configured caps."""
    cfg = cfg or load_risk_config()
    if not all(math.isfinite(v) for v in (entry, stop, cfg.account_usdt, cfg.risk_pct_per_r)):
        return None
    if entry <= 0 or stop <= 0 or entry == stop or cfg.account_usdt <= 0 or cfg.risk_pct_per_r <= 0:
        return None
    stop_pct = abs(entry - stop) / entry
    if stop_pct * 100 < cfg.min_stop_pct:
        return None
    cost_fraction = (cfg.fee_pct_roundtrip + cfg.slippage_pct_roundtrip) / 100.0
    if not math.isfinite(cost_fraction) or cost_fraction < 0:
        return None
    risk_budget = cfg.account_usdt * cfg.risk_pct_per_r / 100.0
    notional = min(
        risk_budget / (stop_pct + cost_fraction),
        cfg.account_usdt * cfg.max_leverage,
        cfg.max_notional_usdt,
    )
    if not math.isfinite(notional) or notional <= 0:
        return None
    cost = notional * cost_fraction
    return SizePlan(
        risk_usdt=risk_budget,
        stop_pct=stop_pct * 100,
        notional_usdt=notional,
        qty=notional / entry,
        leverage_cap=notional / cfg.account_usdt,
        estimated_cost_usdt=cost,
        estimated_loss_usdt=notional * stop_pct + cost,
    )


def format_risk_line(entry: float, stop: float, cfg: RiskConfig | None = None) -> str:
    cfg = cfg or load_risk_config()
    sp = calc_1r_size(entry, stop, cfg)
    if not sp:
        return ""
    return (
        f"風控1R: 風險≈{sp.risk_usdt:.2f}U "
        f"（帳戶{cfg.account_usdt:.0f}U×{cfg.risk_pct_per_r:.2f}%）｜"
        f"名義≈{sp.notional_usdt:.0f}U｜約{sp.leverage_cap:.1f}x名義/帳戶｜"
        f"預估成本≈{sp.estimated_cost_usdt:.2f}U｜含成本停損≈{sp.estimated_loss_usdt:.2f}U"
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
    依同向／總倉及單筆風控上限過濾 YES。
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
        entry = getattr(s, "entry", None)
        stop = getattr(s, "stop", None)
        if entry is not None and stop is not None:
            sp = calc_1r_size(entry, stop, cfg)
            if sp is None:
                blocked.append(f"{sym}(停損距離或倉位不合法)")
                continue
        allowed.append(s)
        wave[side] += 1
    return allowed, blocked
