"""Offline paper runner. Requires independently supplied candle observations.

No network, exchange API, credentials, or live orders. Caller must serialize access
and persist state to durable storage; this CLI refuses unsafe default activation.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from journal_analytics import quality
from paper_trading import close_on_bar, load_state, save_state, try_open
from signal_tracker import load_journal

def process(state, rows, candles):
    """Process exits first, then only fresh waiting retest signals with a touch."""
    events = []
    diagnostics = {"retest_open": 0, "no_candle": 0, "unclosed_candle": 0,
                   "candle_before_signal": 0, "entry_not_touched": 0,
                   "risk_rejected": {}, "opened": 0}
    position = state.get("position")
    # Count each scan observation, not unique signals; preserve across runs.
    state.setdefault("entry_diagnostics", {"scans": 0, "counts": {}, "risk_rejected": {}})
    stats = state["entry_diagnostics"]
    stats["scans"] += 1
    if position:
        candle = candles.get(position["symbol"])
        if candle:
            event = close_on_bar(state, high=float(candle["high"]), low=float(candle["low"]))
            if event:
                events.append(event)
    # Never open and close using the same candle: unknown intrabar ordering.
    if position is not None:
        return events
    for row in sorted(rows, key=lambda x: x.signal_time_ms, reverse=True):
        if row.kind != "retest" or row.status != "open":
            continue
        diagnostics["retest_open"] += 1
        candle = candles.get(row.symbol)
        if not candle:
            diagnostics["no_candle"] += 1
            continue
        if not candle.get("closed", False):
            diagnostics["unclosed_candle"] += 1
            continue
        # Candle must start after signal; otherwise this is look-ahead.
        if int(candle.get("open_time_ms", 0)) <= row.signal_time_ms:
            diagnostics["candle_before_signal"] += 1
            continue
        if not (float(candle["low"]) <= row.entry <= float(candle["high"])):
            diagnostics["entry_not_touched"] += 1
            continue
        score, _ = quality(row)
        accepted, reason = try_open(
            state, signal_id=row.id, symbol=row.symbol, side=row.side,
            entry=row.entry, stop=row.stop, tp=row.tp, quality_score=score,
            kind=row.kind)
        if not accepted:
            diagnostics["risk_rejected"][reason] = diagnostics["risk_rejected"].get(reason, 0) + 1
        if accepted:
            diagnostics["opened"] += 1
            events.append({"type": "paper_open", **state["position"]})
            break
    for key, value in diagnostics.items():
        if key == "risk_rejected":
            for reason, count in value.items():
                stats["risk_rejected"][reason] = stats["risk_rejected"].get(reason, 0) + count
        else:
            stats["counts"][key] = stats["counts"].get(key, 0) + value
    print("Paper entry diagnostics:", json.dumps(diagnostics, ensure_ascii=False))
    return events

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--candles", type=Path, required=True,
                   help="JSON object symbol -> {open_time_ms,high,low,closed:true}")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--single-writer-confirmed", action="store_true",
                   help="Caller guarantees durable state and exclusive locking")
    args = p.parse_args()
    if not args.single_writer_confirmed:
        p.error("refusing to run without external durable single-writer guarantee")
    if not args.state.exists():
        p.error("refusing to initialize state automatically; create it explicitly")
    state = load_state(args.state)
    candles = json.loads(args.candles.read_text(encoding="utf-8"))
    events = process(state, load_journal(args.journal), candles)
    save_state(state, args.state)
    print(json.dumps(events, ensure_ascii=False))

if __name__ == "__main__":
    main()
