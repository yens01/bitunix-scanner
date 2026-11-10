"""Read-only analytics: keep simulated signals separate from verified fills."""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from signal_tracker import JOURNAL_PATH, load_journal

CLOSED = {"win", "loss", "timeout"}
ENTRY_STATES = {"waiting_entry", "triggered", "filled", "closed", "expired", "cancelled"}
VALID_FILLS = {"simulated", "exchange_verified"}


def lifecycle(row):
    # Legacy rows have no execution evidence. Never claim a real fill.
    explicit = getattr(row, "entry_state", "") or ""
    if explicit in ENTRY_STATES:
        return explicit
    if row.status == "skip":
        return "expired"
    if row.status in CLOSED:
        return "closed_simulated"
    if row.status == "open":
        return "waiting_entry" if row.kind == "retest" else "signal_open"
    return "unknown"


def quality(row):
    """Observational priority only; never rewrites strategy decisions."""
    score = 50
    if row.side not in ("long", "short"):
        score -= 40
    if row.planned_rr >= 2:
        score += 15
    elif row.planned_rr < 1.5:
        score -= 15
    if "BOS" in (row.missing_steps or ""):
        score -= 15
    if "回測反應" in (row.missing_steps or ""):
        score -= 10
    if row.kind == "base":
        score -= 15  # conservative observational flag, not a backtested filter
    score = max(0, min(100, score))
    return score, "high" if score >= 65 else ("medium" if score >= 40 else "low")


def net_r(row, *, fee_pct=0.12, slippage_pct=0.10, funding_pct=0.0):
    """Estimated net R; returns None if prices/risk are invalid."""
    if row.status not in CLOSED or row.side not in ("long", "short"):
        return None
    risk = abs(row.entry - row.stop)
    if risk <= 0 or row.entry <= 0:
        return None
    cost_r = row.entry * (fee_pct + slippage_pct + funding_pct) / 100 / risk
    return row.realized_r - cost_r


def report(rows, *, fee_pct=0.12, slippage_pct=0.10, funding_pct=0.0):
    unique = {}
    duplicate_ids = 0
    for row in rows:
        if row.id in unique:
            duplicate_ids += 1
        unique[row.id] = row
    rows = list(unique.values())
    groups = defaultdict(list)
    lifecycle_counts = defaultdict(int)
    quality_counts = defaultdict(int)
    for r in rows:
        lifecycle_counts[lifecycle(r)] += 1
        score, tier = quality(r)
        quality_counts[tier] += 1
        hour = (r.ts[11:13] if len(r.ts or "") >= 13 else "unknown")
        bos = "missing" if "BOS" in (r.missing_steps or "") else "not_missing"
        for dimension, key in (
            ("strategy", r.kind), ("side", r.side), ("symbol", r.symbol),
            ("hour_tw", hour), ("bos", bos), ("quality", tier),
        ):
            groups[(dimension, str(key))].append(r)
    summaries = []
    for (dimension, key), subset in sorted(groups.items()):
        closed = [r for r in subset if r.status in CLOSED and r.side in ("long", "short")]
        wins = sum(r.status == "win" for r in closed)
        losses = sum(r.status == "loss" for r in closed)
        estimates = [net_r(r, fee_pct=fee_pct, slippage_pct=slippage_pct,
                           funding_pct=funding_pct) for r in closed]
        estimates = [x for x in estimates if x is not None]
        summaries.append({
            "dimension": dimension, "group": key, "signals": len(subset),
            "closed_simulated": len(closed), "wins": wins, "losses": losses,
            "win_rate_ex_timeout": round(wins / (wins + losses), 4) if wins + losses else None,
            "gross_r": round(sum(r.realized_r for r in closed), 4),
            "estimated_net_r": round(sum(estimates), 4),
            "estimated_net_r_per_closed": round(sum(estimates) / len(estimates), 4) if estimates else None,
        })
    return {
        "schema_version": 1, "signal_count": len(rows), "duplicate_ids": duplicate_ids,
        "lifecycle": dict(lifecycle_counts), "quality": dict(quality_counts),
        "assumptions": {"fee_pct_roundtrip": fee_pct, "slippage_pct_roundtrip": slippage_pct,
                        "funding_pct": funding_pct, "execution": "simulated, NOT verified fills",
                        "note": "Costs are estimates; not historical actual fees or fills."},
        "groups": summaries,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--journal", type=Path, default=JOURNAL_PATH)
    p.add_argument("--output", type=Path, default=Path("journal_analytics.json"))
    args = p.parse_args()
    fee = float(os.getenv("RISK_FEE_PCT_ROUNDTRIP", "0.12"))
    slip = float(os.getenv("RISK_SLIPPAGE_PCT_ROUNDTRIP", "0.10"))
    funding = float(os.getenv("ANALYTICS_FUNDING_PCT", "0"))
    if min(fee, slip, funding) < 0:
        p.error("cost assumptions must be nonnegative")
    result = report(load_journal(args.journal), fee_pct=fee, slippage_pct=slip,
                    funding_pct=funding)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"analytics: {result['signal_count']} signals; {result['duplicate_ids']} duplicate IDs")


if __name__ == "__main__":
    main()
