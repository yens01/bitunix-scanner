"""Offline paper runner. Requires independently supplied candle observations.

No network, exchange API, credentials, or live orders. Caller must serialize access
and persist state to durable storage; this CLI refuses unsafe default activation.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from datetime import datetime
from journal_analytics import quality
from paper_trading import close_on_bar, load_state, save_state, try_open
from signal_tracker import load_journal

def process(state, rows, candles):
    """Replay all newly observed closed bars in chronological order.

    Legacy callers may pass one candle per symbol. A position opened on a bar
    cannot exit on that same bar because intrabar ordering is unknowable.
    """
    events = []
    stats = state.setdefault("entry_diagnostics", {"scans": 0, "counts": {}, "risk_rejected": {}})
    stats["scans"] += 1
    unique = stats.setdefault("unique_signals", {})
    observations = {}
    for symbol, bars in candles.items():
        if isinstance(bars, dict):
            bars = [bars]
        observations[symbol] = sorted(
            (b for b in bars if b.get("closed", False)),
            key=lambda b: int(b.get("open_time_ms", 0)))
    last = state.setdefault("last_processed_bar_ms", {})
    # Existing position gets every available bar since the last processed one.
    position = state.get("position")
    if position:
        symbol = position["symbol"]
        opened_ms = int(position.get("entry_bar_ms") or 0)
        if not opened_ms and position.get("opened_at"):
            opened_ms = int(datetime.fromisoformat(position["opened_at"]).timestamp() * 1000)
        for bar in observations.get(symbol, []):
            stamp = int(bar.get("open_time_ms", 0))
            if stamp <= last.get(symbol, -1) or stamp <= opened_ms:
                continue
            event = close_on_bar(state, high=float(bar["high"]), low=float(bar["low"]))
            last[symbol] = stamp
            if event:
                events.append(event)
                break
        # Never open a replacement in the same run as a previously open position.
        return events

    diagnostics = {"retest_open": 0, "no_candle": 0, "unclosed_candle": 0,
                   "candle_before_signal": 0, "entry_not_touched": 0,
                   "risk_rejected": {}, "opened": 0}
    for row in sorted(rows, key=lambda x: x.signal_time_ms, reverse=True):
        if row.kind != "retest" or row.status != "open":
            continue
        diagnostics["retest_open"] += 1
        unique.setdefault(row.id, {"symbol": row.symbol, "stage": "candidate"})
        bars = observations.get(row.symbol, [])
        if not bars:
            diagnostics["no_candle"] += 1
            unique[row.id]["stage"] = "no_candle"
            continue
        future = [b for b in bars
                  if int(b.get("open_time_ms", 0)) > row.signal_time_ms
                  and int(b.get("open_time_ms", 0)) > last.get(row.symbol, -1)]
        if not future:
            diagnostics["candle_before_signal"] += 1
            unique[row.id]["stage"] = "waiting_candle"
            continue
        touched = next((b for b in future
                        if float(b["low"]) <= row.entry <= float(b["high"])), None)
        if touched is None:
            diagnostics["entry_not_touched"] += 1
            unique[row.id]["stage"] = "entry_not_touched"
            continue
        score, _ = quality(row)
        accepted, reason = try_open(
            state, signal_id=row.id, symbol=row.symbol, side=row.side,
            entry=row.entry, stop=row.stop, tp=row.tp, quality_score=score,
            kind=row.kind, entry_bar_ms=int(touched["open_time_ms"]))
        if not accepted:
            diagnostics["risk_rejected"][reason] = diagnostics["risk_rejected"].get(reason, 0) + 1
            unique[row.id]["stage"] = "rejected"
            unique[row.id]["reason"] = reason
        else:
            diagnostics["opened"] += 1
            unique[row.id]["stage"] = "opened"
            last[row.symbol] = int(touched["open_time_ms"])
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
