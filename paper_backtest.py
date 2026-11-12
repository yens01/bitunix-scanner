"""Deterministic, paper-only candle replay for quality threshold experiments.

Input bars must be historical, closed, sorted 15m candles; this module never fetches
market data or places orders. Outcomes are estimates, not executable fills.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from journal_analytics import quality
from signal_tracker import load_journal

COST_RATE = 0.0022


def replay(rows, candles, threshold=50, *, cutoff_ms=None):
    if not 0 <= threshold <= 100:
        raise ValueError("threshold must be between 0 and 100")
    results = []
    for row in sorted(rows, key=lambda r: (r.signal_time_ms, r.id)):
        if row.kind != "retest" or row.side not in ("long", "short"):
            continue
        score, _ = quality(row)
        if score < threshold or not ((row.stop < row.entry < row.tp) if row.side == "long"
                                     else (row.tp < row.entry < row.stop)):
            continue
        bars = sorted(candles.get(row.symbol, []), key=lambda b: b["open_time_ms"])
        if cutoff_ms is not None and row.signal_time_ms >= cutoff_ms:
            continue
        eligible = [b for b in bars if b.get("closed") is True
                    and b["open_time_ms"] > row.signal_time_ms
                    and (cutoff_ms is None or b["open_time_ms"] < cutoff_ms)]
        entry_index = next((i for i, b in enumerate(eligible)
                            if b["low"] <= row.entry <= b["high"]), None)
        if entry_index is None:
            continue
        # Entry bar cannot be used for exit: intrabar order is unknown.
        for bar in eligible[entry_index + 1:]:
            stop_hit = bar["low"] <= row.stop if row.side == "long" else bar["high"] >= row.stop
            tp_hit = bar["high"] >= row.tp if row.side == "long" else bar["low"] <= row.tp
            if not (stop_hit or tp_hit):
                continue
            exit_price = row.stop if stop_hit else row.tp
            direction = 1 if row.side == "long" else -1
            net_return = direction * (exit_price - row.entry) / row.entry - COST_RATE
            results.append({"signal_id": row.id, "symbol": row.symbol,
                            "score": score, "entry_bar_ms": eligible[entry_index]["open_time_ms"],
                            "exit_bar_ms": bar["open_time_ms"],
                            "reason": "stop" if stop_hit else "tp",
                            "net_return": round(net_return, 10)})
            break
    return results


def compare(rows, candles, thresholds=(50, 65), cutoff_ms=None):
    result = {}
    for threshold in thresholds:
        trades = replay(rows, candles, threshold, cutoff_ms=cutoff_ms)
        result[str(threshold)] = {
            "closed": len(trades),
            "win_rate_pct": round(100 * sum(t["net_return"] > 0 for t in trades) / len(trades), 2)
                            if trades else None,
            "mean_net_return": round(sum(t["net_return"] for t in trades) / len(trades), 8)
                               if trades else None,
            "trades": trades,
        }
    return {"assumptions": "closed candles, next-bar exit, stop-first, 0.22% roundtrip cost; no fill liquidity model",
            "cutoff_ms": cutoff_ms, "thresholds": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--candles", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("backtest_comparison.json"))
    parser.add_argument("--cutoff-ms", type=int)
    args = parser.parse_args()
    rows = load_journal(args.journal)
    candles = json.loads(args.candles.read_text(encoding="utf-8"))
    args.output.write_text(json.dumps(compare(rows, candles, cutoff_ms=args.cutoff_ms),
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
