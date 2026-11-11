"""Integration regression tests for paper replay, accounting, and integrity."""
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from paper_runner import process
from paper_trading import default_state, try_open, close_on_bar
from paper_performance import report
from paper_daily_notify import daily_message
from signal_tracker import JournalEntry


def signal():
    return JournalEntry(id="fresh", ts="2026-11-11 08:00:00",
        signal_time_ms=1000, symbol="BTCUSDT", side="long", interval="15m",
        entry=100, stop=99, tp=102, planned_rr=2, score=80,
        structure="", missing_steps="", kind="retest", status="open")


def bar(t, low, high):
    return {"open_time_ms": t, "low": low, "high": high, "closed": True}


class PaperIntegrationTests(unittest.TestCase):
    def test_replay_earlier_stop_before_later_target(self):
        state = default_state()
        state["position"] = {"signal_id": "old", "symbol": "BTCUSDT",
            "side": "long", "entry": 100, "stop": 99, "tp": 102,
            "quantity": 0.1, "opened_at": "2026-11-11T00:00:00+00:00"}
        bars = [bar(1794356100000, 98, 101), bar(1794357000000, 100, 103)]
        events = process(state, [], {"BTCUSDT": bars})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "stop")
        self.assertIsNone(state["position"])

    def test_replay_checkpoint_prevents_double_exit(self):
        state = default_state()
        state["last_processed_bar_ms"] = {"BTCUSDT": 2000}
        state["position"] = {"signal_id": "old", "symbol": "BTCUSDT",
            "side": "long", "entry": 100, "stop": 99, "tp": 102,
            "quantity": 0.1, "opened_at": "1970-01-01T00:00:00+00:00"}
        self.assertEqual(process(state, [], {"BTCUSDT": [bar(1000, 98, 103)]}), [])
        self.assertIsNotNone(state["position"])

    def test_close_time_and_net_pnl(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true",
                                      "TRADING_KILL_SWITCH": "false"}):
            state = default_state()
            self.assertTrue(try_open(state, signal_id="x", symbol="BTCUSDT",
                side="long", entry=100, stop=99, tp=102,
                quality_score=80)[0])
            event = close_on_bar(state, high=103, low=100)
            self.assertIsNotNone(event["closed_at"])
            self.assertAlmostEqual(state["realized_net_today"], event["estimated_net_pnl_usdt"], places=7)

    def test_event_integrity_and_close_day(self):
        state = {"events": [
            {"type": "open", "signal_id": "a", "symbol": "BTCUSDT",
             "opened_at": "2026-11-10T23:00:00+00:00"},
            {"type": "close", "signal_id": "a", "closed_at": "2026-11-11T00:15:00+00:00",
             "estimated_net_pnl_usdt": 0.25},
            {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": 0.25},
        ]}
        r = report(state)
        self.assertEqual(r["duplicate_close_events"], 1)
        self.assertEqual(r["overall"]["closed_trades"], 1)
        self.assertIn("當日平倉：1 筆", daily_message(state, "2026-11-11"))
        self.assertIn("當日平倉：0 筆", daily_message(state, "2026-11-10"))


if __name__ == "__main__":
    unittest.main()
