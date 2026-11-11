"""Performance analytics for actual PAPER open/close events, never scanner signals."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _summary(trades, capital):
    pnls = [t["estimated_net_pnl_usdt"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = capital
    peak = capital
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    curve = []
    for trade in trades:
        equity += trade["estimated_net_pnl_usdt"]
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, drawdown / peak * 100)
        curve.append({"signal_id": trade["signal_id"], "equity_usdt": round(equity, 8)})
    return {
        "closed_trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "breakeven": len(pnls) - len(wins) - len(losses),
        "win_rate_pct": round(100 * len(wins) / len(pnls), 2) if pnls else None,
        "estimated_net_pnl_usdt": round(sum(pnls), 8),
        "average_net_pnl_usdt": round(sum(pnls) / len(pnls), 8) if pnls else None,
        "average_win_usdt": round(gross_profit / len(wins), 8) if wins else None,
        "average_loss_usdt": round(-gross_loss / len(losses), 8) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "max_drawdown_usdt": round(max_drawdown, 8),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "ending_equity_usdt": round(equity, 8),
        "equity_curve": curve,
    }


def report(state, *, starting_capital_usdt=50.0):
    if starting_capital_usdt <= 0:
        raise ValueError("starting capital must be positive")
    events = state.get("events") or []
    opens = {}
    trades = []
    unmatched_closes = 0
    duplicate_opens = 0
    duplicate_closes = 0
    seen_closes = set()
    for event in events:
        signal_id = event.get("signal_id")
        if event.get("type") == "open" and signal_id:
            if signal_id in opens or signal_id in seen_closes:
                duplicate_opens += 1
                continue
            opens[signal_id] = event
        elif event.get("type") == "close":
            if signal_id in seen_closes:
                duplicate_closes += 1
                continue
            opened = opens.pop(signal_id, None)
            if opened is None:
                unmatched_closes += 1
                continue
            try:
                pnl = float(event["estimated_net_pnl_usdt"])
            except (KeyError, ValueError, TypeError):
                unmatched_closes += 1
                continue
            seen_closes.add(signal_id)
            trades.append({
                "signal_id": signal_id, "symbol": opened.get("symbol", "unknown"),
                "side": opened.get("side", "unknown"),
                "opened_at": opened.get("opened_at"),
                "closed_at": event.get("closed_at"),
                "reason": event.get("reason"), "estimated_net_pnl_usdt": pnl,
            })
    mark = state.get("mark") or {}
    position = state.get("position") or {}
    unrealized = None
    if position and mark.get("symbol") == position.get("symbol") and mark.get("price") is not None:
        price = float(mark["price"])
        entry = float(position["entry"])
        qty = float(position["quantity"])
        direction = 1 if position["side"] == "long" else -1
        unrealized = round((price - entry) * qty * direction -
                           (price + entry) * qty * 0.0011, 8)
    grouped = defaultdict(list)
    for trade in trades:
        grouped[("symbol", trade["symbol"])].append(trade)
        grouped[("side", trade["side"])].append(trade)
    by_group = [
        {"dimension": dimension, "group": name,
         **{k: v for k, v in _summary(subset, starting_capital_usdt).items()
            if k not in ("equity_curve", "ending_equity_usdt", "max_drawdown_usdt", "max_drawdown_pct")}}
        for (dimension, name), subset in sorted(grouped.items())
    ]
    return {
        "schema_version": 1,
        "source": "paper_state.events (simulated orders, not exchange fills)",
        "assumptions": {"starting_capital_usdt": starting_capital_usdt,
                        "pnl": "estimated net after modelled costs; no live execution",
                        "unrealized_pnl": "estimated using last closed candle, not executable quote",
                        "drawdown": "closed-trade equity only; excludes intratrade excursions"},
        "day_utc": state.get("day"), "orders_today": state.get("orders_today", 0),
        "open_position": state.get("position"),
        "mark": mark if unrealized is not None else None,
        "estimated_unrealized_pnl_usdt": unrealized,
        "estimated_marked_equity_usdt": round(
            starting_capital_usdt + sum(t["estimated_net_pnl_usdt"] for t in trades)
            + unrealized, 8) if unrealized is not None else None,
        "unmatched_close_events": unmatched_closes,
        "duplicate_open_events": duplicate_opens,
        "duplicate_close_events": duplicate_closes,
        "unmatched_open_events": len(opens),
        "overall": _summary(trades, starting_capital_usdt),
        "groups": by_group,
        "trades": trades,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("paper_state.json"))
    parser.add_argument("--output", type=Path, default=Path("paper_performance.json"))
    parser.add_argument("--capital", type=float, default=50.0)
    args = parser.parse_args()
    result = report(json.loads(args.state.read_text(encoding="utf-8")),
                    starting_capital_usdt=args.capital)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Paper performance:", json.dumps(result["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
