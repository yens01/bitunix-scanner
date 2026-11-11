import unittest
from paper_daily_notify import daily_message


class DailyMessageTests(unittest.TestCase):
    def test_filters_to_open_day(self):
        state = {"events": [
            {"type": "open", "signal_id": "a", "symbol": "BTCUSDT",
             "side": "long", "opened_at": "2026-11-11T14:00:00+00:00"},
            {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": 0.3},
            {"type": "open", "signal_id": "b", "symbol": "ETHUSDT",
             "side": "short", "opened_at": "2026-11-12T14:00:00+00:00"},
            {"type": "close", "signal_id": "b", "estimated_net_pnl_usdt": -1}]}
        msg = daily_message(state, "2026-11-11")
        self.assertIn("1 筆", msg)
        self.assertIn("+0.300000 USDT", msg)
        self.assertIn("100.0%", msg)


if __name__ == "__main__":
    unittest.main()
