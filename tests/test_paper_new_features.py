import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from paper_notify import deliver
from paper_performance import report
from quality_calibration import calibrate
from signal_tracker import JournalEntry


def row(id, rr=2):
    return JournalEntry(id=id, ts="2026-11-11 00:00:00", signal_time_ms=1,
        symbol="BTCUSDT", side="long", interval="15m", entry=100,
        stop=99, tp=102, planned_rr=rr, score=80,
        structure="", missing_steps="", kind="retest")


class NewFeaturesTests(unittest.TestCase):
    def test_outbox_retry_and_ack(self):
        state = {"notification_outbox": [
            {"id": "open:a", "event": {"type": "paper_open", "signal_id": "a",
                "symbol": "BTCUSDT", "side": "long", "entry": 100,
                "stop": 99, "tp": 102}, "sent": False},
            {"id": "open:b", "event": {"type": "paper_open", "signal_id": "b",
                "symbol": "BTCUSDT", "side": "long", "entry": 100,
                "stop": 99, "tp": 102}, "sent": False}]}
        calls = []
        def fail_second(message):
            calls.append(message)
            if len(calls) == 2:
                raise RuntimeError("telegram down")
        with patch("paper_trading.save_state"):
            with self.assertRaises(RuntimeError):
                deliver(state, fail_second)
            self.assertTrue(state["notification_outbox"][0]["sent"])
            self.assertFalse(state["notification_outbox"][1]["sent"])
            retried = []
            deliver(state, retried.append)
            self.assertEqual(len(retried), 1)

    def test_mark_to_market_and_missing_mark(self):
        state = {"events": [], "position": {
            "symbol": "BTCUSDT", "side": "long", "entry": 100, "quantity": 1},
            "mark": {"symbol": "BTCUSDT", "price": 102}}
        r = report(state)
        self.assertAlmostEqual(r["estimated_unrealized_pnl_usdt"], 1.7778, places=4)
        self.assertAlmostEqual(r["estimated_marked_equity_usdt"], 51.7778, places=4)
        state.pop("mark")
        self.assertIsNone(report(state)["estimated_unrealized_pnl_usdt"])

    def test_calibration_min_sample_gate(self):
        state = {"events": [
            {"type": "open", "signal_id": "a"},
            {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": 1}]}
        result = calibrate([row("a")], state, min_samples=20)
        self.assertEqual(result["groups"]["high"]["samples"], 1)
        self.assertIsNone(result["groups"]["high"]["observed_win_rate_pct"])
        self.assertFalse(result["auto_adjust_threshold"])


if __name__ == "__main__":
    unittest.main()
