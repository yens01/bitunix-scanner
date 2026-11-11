"""Regression checks for paper-only accounting integrity."""
import math
import unittest

from paper_performance import report


class PaperAccountingIntegrityTests(unittest.TestCase):
    def test_nonfinite_pnl_does_not_poison_equity(self):
        state = {"events": [
            {"type": "open", "signal_id": "a", "symbol": "BTCUSDT"},
            {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": "nan"},
            {"type": "open", "signal_id": "b", "symbol": "ETHUSDT"},
            {"type": "close", "signal_id": "b", "estimated_net_pnl_usdt": 2.5},
        ]}
        result = report(state)
        self.assertEqual(result["overall"]["closed_trades"], 1)
        self.assertEqual(result["overall"]["estimated_net_pnl_usdt"], 2.5)
        self.assertEqual(result["unmatched_close_events"], 1)
        self.assertTrue(math.isfinite(result["overall"]["ending_equity_usdt"]))

    def test_bad_mark_does_not_break_report(self):
        state = {"events": [], "position": {"symbol": "BTCUSDT", "entry": 100,
                 "quantity": 1, "side": "long"},
                 "mark": {"symbol": "BTCUSDT", "price": "nan"}}
        result = report(state)
        self.assertIsNone(result["estimated_unrealized_pnl_usdt"])
        self.assertIsNone(result["estimated_marked_equity_usdt"])


if __name__ == "__main__":
    unittest.main()
