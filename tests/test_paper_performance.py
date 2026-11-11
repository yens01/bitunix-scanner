import unittest
from paper_performance import report


class PaperPerformanceTests(unittest.TestCase):
    def test_closed_trades_only_and_groups(self):
        state = {"day": "2026-11-11", "orders_today": 3,
                 "position": {"symbol": "ETHUSDT", "side": "long"},
                 "events": [
                     {"type": "open", "signal_id": "a", "symbol": "BTCUSDT", "side": "long"},
                     {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": 2.0},
                     {"type": "open", "signal_id": "b", "symbol": "BTCUSDT", "side": "short"},
                     {"type": "close", "signal_id": "b", "estimated_net_pnl_usdt": -1.0},
                     {"type": "open", "signal_id": "c", "symbol": "ETHUSDT", "side": "long"}]}
        r = report(state, starting_capital_usdt=50)
        self.assertEqual(r["overall"]["closed_trades"], 2)
        self.assertEqual(r["overall"]["win_rate_pct"], 50)
        self.assertEqual(r["overall"]["profit_factor"], 2)
        self.assertEqual(r["overall"]["estimated_net_pnl_usdt"], 1)
        self.assertEqual(r["overall"]["ending_equity_usdt"], 51)
        self.assertEqual(r["overall"]["max_drawdown_usdt"], 1)
        self.assertEqual(r["open_position"]["symbol"], "ETHUSDT")
        self.assertEqual(len(r["trades"]), 2)
        self.assertEqual(len(r["groups"]), 3)

    def test_empty_and_unmatched_close(self):
        r = report({"events": [{"type": "close", "signal_id": "missing",
                                 "estimated_net_pnl_usdt": 10}]})
        self.assertEqual(r["overall"]["closed_trades"], 0)
        self.assertIsNone(r["overall"]["profit_factor"])
        self.assertEqual(r["unmatched_close_events"], 1)

    def test_no_loss_profit_factor_is_undefined(self):
        r = report({"events": [{"type": "open", "signal_id": "a"},
                               {"type": "close", "signal_id": "a",
                                "estimated_net_pnl_usdt": 1}]})
        self.assertIsNone(r["overall"]["profit_factor"])

    def test_invalid_capital(self):
        with self.assertRaises(ValueError):
            report({}, starting_capital_usdt=0)


if __name__ == "__main__":
    unittest.main()
