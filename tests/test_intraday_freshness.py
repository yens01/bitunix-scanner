import unittest
from types import SimpleNamespace
from unittest.mock import patch
from bitunix_scanner import _intraday_level_status


class IntradayFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.plan = SimpleNamespace(symbol="BTCUSDT", structure="偏空", side="short",
                                    entry=74771.4, stop=75071.11, tp=67677.51)

    def candles(self):
        return [(i * 900, 74770, 74790, 74750, 74770) for i in range(60)]

    def test_far_entry_is_suppressed(self):
        with patch("recovery_dashboard._closed_candles", return_value=self.candles()), patch(
                "bitunix_scanner.fetch_ticker_last", return_value=72000):
            status, levels = _intraday_level_status(self.plan)
        self.assertIsNone(levels)
        self.assertIn("禁止進場", status)

    def test_near_entry_caps_target_to_2r(self):
        with patch("recovery_dashboard._closed_candles", return_value=self.candles()), patch(
                "bitunix_scanner.fetch_ticker_last", return_value=74770):
            status, levels = _intraday_level_status(self.plan)
        self.assertIsNotNone(levels)
        self.assertAlmostEqual(levels[2], self.plan.entry - 2*(self.plan.stop-self.plan.entry))
        self.assertNotEqual(levels[2], self.plan.tp)

    def test_missing_price_fails_closed(self):
        with patch("recovery_dashboard._closed_candles", return_value=self.candles()), patch(
                "bitunix_scanner.fetch_ticker_last", return_value=0):
            status, levels = _intraday_level_status(self.plan)
        self.assertIsNone(levels)
        self.assertIn("禁止進場", status)


if __name__ == "__main__":
    unittest.main()
