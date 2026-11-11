"""Fail-closed paper trading guardrails. NO exchange order endpoint exists here.

This module deliberately does not place live orders. Persist state on durable storage
before relying on limits across scheduled runs. Single-writer lock required.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

@dataclass
class PaperConfig:
    capital_usdt: float = 50.0
    risk_fraction: float = 0.005
    daily_loss_fraction: float = 0.02
    max_open_positions: int = 1
    max_new_orders_per_day: int = 2
    max_leverage: float = 1.0
    max_notional_usdt: float = 50.0
    min_quality_score: int = 50

@dataclass
class PaperPosition:
    signal_id: str
    symbol: str
    side: str
    entry: float
    stop: float
    tp: float
    quantity: float
    opened_at: str

def today_utc():
    return datetime.now(timezone.utc).date().isoformat()

def default_state():
    return {"day": today_utc(), "orders_today": 0, "realized_loss_today": 0.0,
            "position": None, "events": [], "halted": False}

def load_state(path: Path):
    if not path.exists():
        return default_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("day") != today_utc():
        state["day"] = today_utc()
        state["orders_today"] = 0
        state["realized_loss_today"] = 0.0
    return state

def save_state(state, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".paper-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def try_open(state, *, signal_id, symbol, side, entry, stop, tp,
             quality_score, kind="retest", config=None, live=False):
    """Returns (accepted, reason). No external side effects or exchange calls."""
    c = config or PaperConfig()
    # Paper-only quality threshold can be tuned without enabling live orders.
    raw_quality = os.getenv('PAPER_MIN_QUALITY_SCORE', '').strip()
    if raw_quality:
        try:
            threshold = int(raw_quality)
        except ValueError:
            return False, 'invalid PAPER_MIN_QUALITY_SCORE'
        if not 0 <= threshold <= 100:
            return False, 'invalid PAPER_MIN_QUALITY_SCORE'
        c.min_quality_score = threshold
    if live:
        return False, "live orders disabled"
    if os.getenv("PAPER_TRADING_ENABLED", "false").lower() != "true":
        return False, "paper trading disabled"
    if os.getenv("TRADING_KILL_SWITCH", "true").lower() != "false":
        return False, "kill switch enabled"
    if state.get("halted"):
        return False, "state halted"
    if state.get("day") != today_utc():
        return False, "stale daily state; reload first"
    if kind != "retest" or quality_score < c.min_quality_score:
        return False, "strategy/quality rejected"
    if not signal_id or not symbol or side not in ("long", "short"):
        return False, "invalid signal"
    if not all(isinstance(x, (int, float)) and 0 < x < float("inf") for x in (entry, stop, tp)):
        return False, "invalid prices"
    if not ((stop < entry < tp) if side == "long" else (tp < entry < stop)):
        return False, "invalid stop/target geometry"
    if state.get("position") is not None:
        return False, "position already open"
    if state.get("orders_today", 0) >= c.max_new_orders_per_day:
        return False, "daily order limit"
    if state.get("realized_loss_today", 0) >= c.capital_usdt * c.daily_loss_fraction:
        return False, "daily loss limit"
    if any(e.get("signal_id") == signal_id and e.get("type") == "open" for e in state.get("events", [])):
        return False, "duplicate signal"
    risk_per_unit = abs(entry - stop)
    risk_budget = min(c.capital_usdt * c.risk_fraction,
                      max(0.0, c.capital_usdt * c.daily_loss_fraction - state.get("realized_loss_today", 0)))
    # Reserve 0.22% of entry notional for estimated roundtrip fees + slippage.
    qty = min(risk_budget / (risk_per_unit + entry * 0.0022),
              c.capital_usdt * c.max_leverage / entry,
              c.max_notional_usdt / entry)
    if qty <= 0:
        return False, "zero quantity"
    position = PaperPosition(signal_id, symbol, side, entry, stop, tp, qty,
                             datetime.now(timezone.utc).isoformat())
    state["position"] = asdict(position)
    state["orders_today"] = state.get("orders_today", 0) + 1
    state.setdefault("events", []).append({"type": "open", **asdict(position)})
    return True, "paper position opened"

def close_on_bar(state, *, high, low, config=None):
    """Conservative: if stop and target touch in same bar, stop wins."""
    p = state.get("position")
    if p is None:
        return None
    if high < low or low <= 0:
        raise ValueError("invalid candle")
    side = p["side"]
    stop_hit = low <= p["stop"] if side == "long" else high >= p["stop"]
    tp_hit = high >= p["tp"] if side == "long" else low <= p["tp"]
    if not (stop_hit or tp_hit):
        return None
    price = p["stop"] if stop_hit else p["tp"]
    gross = (price - p["entry"]) * p["quantity"] * (1 if side == "long" else -1)
    costs = (p["entry"] + price) * p["quantity"] * 0.0011
    pnl = gross - costs
    state["realized_loss_today"] = state.get("realized_loss_today", 0) + max(0, -pnl)
    event = {"type": "close", **p, "reason": "stop" if stop_hit else "tp",
             "exit": price, "estimated_net_pnl_usdt": round(pnl, 8)}
    state.setdefault("events", []).append(event)
    state["position"] = None
    return event

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Paper trading state inspection only")
    parser.add_argument("--state", type=Path, default=Path("paper_state.json"))
    args = parser.parse_args()
    print(json.dumps(load_state(args.state), ensure_ascii=False, indent=2))
