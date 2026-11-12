import unittest
from signal_tracker import JournalEntry
from paper_backtest import replay, compare


def signal(score=80):
    return JournalEntry(id="test1", ts="2026-11-10 22:00:00",
        signal_time_ms=1000, symbol="BTCUSDT", side="long", interval="15m",
        entry=100, stop=99, tp=102, planned_rr=2, score=score,
        structure="", missing_steps="", kind="retest", status="open")


class BacktestTests(unittest.TestCase):
    def test_entry_bar_never_closes_and_stop_wins_on_next_bar(self):
        candles = {"BTCUSDT": [
            {"open_time_ms": 2000, "low": 98, "high": 103, "closed": True},
            {"open_time_ms": 3000, "low": 98, "high": 103, "closed": True}]}
        trades = replay([signal()], candles)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_bar_ms"], 2000)
        self.assertEqual(trades[0]["exit_bar_ms"], 3000)
        self.assertEqual(trades[0]["reason"], "stop")
        self.assertLess(trades[0]["net_return"], -0.01)

    def test_cutoff_excludes_future_bars(self):
        candles = {"BTCUSDT": [
            {"open_time_ms": 2000, "low": 99.9, "high": 101, "closed": True},
            {"open_time_ms": 3000, "low": 98, "high": 103, "closed": True}]}
        self.assertEqual(replay([signal()], candles, cutoff_ms=3000), [])
        self.assertEqual(compare([signal()], candles)["thresholds"]["65"]["closed"], 1)

    def test_unclosed_bars_ignored(self):
        candles = {"BTCUSDT": [{"open_time_ms": 2000, "low": 99, "high": 101, "closed": False}]}
        self.assertEqual(replay([signal()], candles), [])


if __name__ == "__main__":
    unittest.main()
