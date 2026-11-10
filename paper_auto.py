"""Paper-only scheduled runner using Bitunix public candles, no order APIs."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from bitunix_scanner import fetch_klines
from sr_logic import parse_klines
from signal_tracker import load_journal
from paper_runner import process
from paper_trading import load_state, save_state

STATE = Path("paper_state.json")
EVENTS = Path("paper_events_pending.json")

def main():
    # A missing persisted state must never reset exposure or daily limits.
    if not STATE.exists():
        raise RuntimeError("paper state unavailable: refusing to trade")
    if os.getenv("PAPER_TRADING_ENABLED", "false").lower() != "true":
        print("Paper trading not enabled")
        return
    if os.getenv("TRADING_KILL_SWITCH", "true").lower() != "false":
        print("Paper kill switch enabled")
        return
    state = load_state(STATE)
    rows = load_journal()
    selected = []
    if state.get("position"):
        selected.append(state["position"]["symbol"])
    else:
        selected = list(dict.fromkeys(r.symbol for r in sorted(rows, key=lambda x:x.signal_time_ms, reverse=True)
            if r.kind == "retest" and r.status == "open" and r.side in ("long", "short")
            and 0 <= int(datetime.now(timezone.utc).timestamp()*1000) - r.signal_time_ms <= 8*3600*1000))[:12]
    candles = {}
    for symbol in selected:
        try:
            bars = parse_klines(fetch_klines(symbol, "15m", 5))
            # The newest bar may be incomplete. Use only confirmed closed bars.
            now = int(datetime.now(timezone.utc).timestamp()*1000)
            closed = [b for b in bars if b.time + 15*60*1000 <= now]
            if closed:
                b = closed[-1]
                candles[symbol] = {"open_time_ms":b.time, "high":b.high, "low":b.low, "closed":True}
        except Exception as exc:
            # Fail closed: do not open if market observation failed.
            print(f"Paper candle unavailable for {symbol}: {exc}")
    events = process(state, rows, candles)
    save_state(state, STATE)
    EVENTS.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"paper_events":events, "position":state.get("position")}, ensure_ascii=False))

if __name__ == "__main__":
    main()
