"""Regression tests for journal performance statistics.

Run: python -m unittest discover -s tests -v
"""
import unittest
from signal_tracker import JournalEntry, compute_stats


def trade(side, status, realized_r):
    return JournalEntry(
        id="test", ts="2026-11-10 12:00:00", signal_time_ms=0,
        symbol="BTCUSDT", side=side, interval="15m",
        entry=100.0, stop=99.0, tp=102.0, planned_rr=2.0,
        score=0, structure="", missing_steps="",
        status=status, realized_r=realized_r, kind="plan",
    )


class JournalStatisticsTests(unittest.TestCase):
    def test_directionless_plans_do_not_count_as_trades(self):
        rows = [
            trade("short", "win", 2.0),
            trade("long", "loss", -1.0),
            trade("none", "win", 0.0),
            trade("none", "loss", 0.0),
        ]
        stats = compute_stats(rows)
        self.assertEqual(stats.total, 4)
        self.assertEqual(stats.win_n, 1)
        self.assertEqual(stats.loss_n, 1)
        self.assertAlmostEqual(stats.avg_realized_r, 0.5)
        self.assertAlmostEqual(stats.win_rate, 50.0)

    def test_timeout_in_expectancy_but_not_win_rate(self):
        rows = [
            trade("short", "win", 1.5),
            trade("short", "loss", -1.0),
            trade("short", "timeout", 0.0),
        ]
        stats = compute_stats(rows)
        self.assertEqual(stats.timeout_n, 1)
        self.assertAlmostEqual(stats.win_rate, 50.0)
        self.assertAlmostEqual(stats.expectancy_r, 1 / 6)


if __name__ == "__main__":
    unittest.main()
