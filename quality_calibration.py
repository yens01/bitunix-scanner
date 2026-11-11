"""Observational quality calibration on paper fills, never a predicted win rate."""
import argparse
import json
from pathlib import Path
from journal_analytics import quality
from paper_performance import report
from signal_tracker import load_journal


def calibrate(rows, state, min_samples=20):
    by_id = {r.id: r for r in rows}
    bins = {"low": [], "medium": [], "high": []}
    missing = 0
    for trade in report(state)["trades"]:
        row = by_id.get(trade["signal_id"])
        if row is None:
            missing += 1
            continue
        _, tier = quality(row)
        bins[tier].append(float(trade["estimated_net_pnl_usdt"]))
    groups = {}
    for tier, values in bins.items():
        n = len(values)
        groups[tier] = {
            "samples": n,
            "eligible_for_interpretation": n >= min_samples,
            "observed_win_rate_pct": round(100 * sum(x > 0 for x in values) / n, 2)
            if n >= min_samples else None,
            "observed_mean_net_pnl_usdt": round(sum(values) / n, 8)
            if n >= min_samples else None,
        }
    return {"source": "paper fills matched to signal quality; observational only",
            "min_samples_per_tier": min_samples, "unmatched_fills": missing,
            "groups": groups, "auto_adjust_threshold": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("paper_state.json"))
    parser.add_argument("--output", type=Path, default=Path("quality_calibration.json"))
    parser.add_argument("--min-samples", type=int, default=20)
    args = parser.parse_args()
    if args.min_samples < 1:
        parser.error("min-samples must be positive")
    result = calibrate(load_journal(), json.loads(args.state.read_text(encoding="utf-8")),
                       args.min_samples)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()
