import unittest
from types import SimpleNamespace
from small_cap_radar import score_candidate, rank_candidates


def candles():
    bars = [SimpleNamespace(time=i * 900000, high=101, low=99, close=100, volume=100)
            for i in range(44)]
    bars[-1] = SimpleNamespace(time=43 * 900000, high=103, low=100, close=102, volume=400)
    return bars


class SmallCapRadarTests(unittest.TestCase):
    def test_breakout_volume_ranks_candidate(self):
        result = score_candidate("TESTUSDT", candles(), 4000000, 2,
                                 now_ms=44 * 900000)
        self.assertIsNotNone(result)
        self.assertEqual(result["stage"], "啟動")
        self.assertGreaterEqual(result["score"], 80)

    def test_unclosed_latest_candle_ignored(self):
        self.assertIsNone(score_candidate("TESTUSDT", candles(), 4000000, 2,
                                          now_ms=43 * 900000))

    def test_reject_already_pumped_and_illiquid(self):
        self.assertIsNone(score_candidate("TESTUSDT", candles(), 4000000, 25,
                                          now_ms=44 * 900000))
        self.assertIsNone(score_candidate("TESTUSDT", candles(), 10000, 2,
                                          now_ms=44 * 900000))

    def test_major_symbols_excluded(self):
        ticks = [{"symbol": "BTCUSDT", "quoteVol": "4000000", "lastPrice": "102", "open": "100"},
                 {"symbol": "TESTUSDT", "quoteVol": "4000000", "lastPrice": "102", "open": "100"}]
        results = rank_candidates(ticks, lambda _: candles(), now_ms=44 * 900000)
        self.assertEqual([r["symbol"] for r in results], ["TESTUSDT"])


if __name__ == "__main__":
    unittest.main()
