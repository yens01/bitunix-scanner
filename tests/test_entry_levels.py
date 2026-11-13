import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from recovery_dashboard import _watch_levels, entry_watch_message


class EntryLevelTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)

    def candles(self, minutes, count=100):
        t = self.now.timestamp()
        return [
            {"time": int((t - (count - i) * minutes * 60) * 1000),
             "open": 100 + i * .1, "high": 100.4 + i * .1,
             "low": 99.8 + i * .1, "close": 100.2 + i * .1}
            for i in range(count)
        ]

    def test_price_stop_target_and_no_market_order(self):
        def fetch(symbol, interval="15m", limit=120):
            return self.candles(60 if interval == "1h" else 15)
        with patch("bitunix_scanner.fetch_klines", side_effect=fetch):
            msg = _watch_levels("BTCUSDT", self.now)
        self.assertIn("參考進場價", msg)
        self.assertIn("停損", msg)
        self.assertIn("目標 2R", msg)
        self.assertIn("不能直接市價下單", msg)

    def test_stale_data_blocks_plan(self):
        with patch("bitunix_scanner.fetch_klines", return_value=self.candles(15, 80)):
            msg = entry_watch_message({"events": []}, now=self.now)
        self.assertIn("禁止進場", msg)

    def test_existing_position_blocks_market_fetch(self):
        with patch("bitunix_scanner.fetch_klines") as fetch:
            msg = entry_watch_message({"events": [], "position": {"symbol": "ETHUSDT"}}, now=self.now)
        fetch.assert_not_called()
        self.assertIn("禁止加碼", msg)


if __name__ == "__main__":
    unittest.main()
