import unittest
from types import SimpleNamespace
from small_cap_radar import score_candidate, rank_candidates, fetch_with_backoff, trade_plan


def candles():
    bars = [SimpleNamespace(time=i * 900000, high=101, low=99, close=100, volume=100)
            for i in range(44)]
    bars[-1] = SimpleNamespace(time=43 * 900000, high=103, low=100, close=102, volume=400)
    return bars


class SmallCapRadarTests(unittest.TestCase):
    def test_plan_uses_closed_candles_and_bounded_stop(self):
        bars = candles()
        plan = trade_plan(bars, 101, volume_ratio=3)
        self.assertEqual(plan["entry_status"], "突破確認")
        self.assertEqual(plan["reference_entry"], 101)
        self.assertEqual(plan["reference_target"], 105)
        self.assertEqual(plan["reference_rr"], 2)

    def test_failed_breakout_suppresses_plan(self):
        bars = candles()
        bars[-2] = SimpleNamespace(time=42 * 900000, high=103, low=100, close=102, volume=400)
        bars[-1] = SimpleNamespace(time=43 * 900000, high=102, low=99, close=100, volume=400)
        plan = trade_plan(bars, 103, volume_ratio=3)
        self.assertEqual(plan["entry_status"], "突破失敗")
        self.assertIsNone(plan["reference_entry"])

    def test_excessive_stop_distance_suppresses_plan(self):
        bars = candles()
        bars[-3] = SimpleNamespace(time=41 * 900000, high=101, low=50, close=100, volume=100)
        plan = trade_plan(bars, 101, volume_ratio=3)
        self.assertIsNone(plan["reference_entry"])

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

    def test_rate_limit_retries_with_backoff(self):
        calls = []
        sleeps = []
        def fetch(symbol):
            calls.append(symbol)
            if len(calls) < 3:
                raise RuntimeError("API 錯誤: request too frequently")
            return candles()
        result = fetch_with_backoff(fetch, "TESTUSDT", sleep=sleeps.append)
        self.assertEqual(result, candles())
        self.assertEqual(sleeps, [3, 6])

    def test_non_rate_limit_error_is_not_retried(self):
        calls = []
        def fetch(symbol):
            calls.append(symbol)
            raise RuntimeError("API 錯誤: invalid symbol")
        with self.assertRaises(RuntimeError):
            fetch_with_backoff(fetch, "TESTUSDT", sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_major_symbols_excluded(self):
        ticks = [{"symbol": "BTCUSDT", "quoteVol": "4000000", "lastPrice": "102", "open": "100"},
                 {"symbol": "TESTUSDT", "quoteVol": "4000000", "lastPrice": "102", "open": "100"}]
        results = rank_candidates(ticks, lambda _: candles(), now_ms=44 * 900000)
        self.assertEqual([r["symbol"] for r in results], ["TESTUSDT"])


if __name__ == "__main__":
    unittest.main()
