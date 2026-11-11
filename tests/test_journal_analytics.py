import tempfile
import unittest
from pathlib import Path
from signal_tracker import JournalEntry, load_journal, save_journal
from journal_analytics import lifecycle, quality, net_r, report


def row(**overrides):
    data = dict(id="a", ts="2026-11-10 20:00:00", signal_time_ms=0,
                symbol="BTCUSDT", side="short", interval="15m", entry=100,
                stop=101, tp=98, planned_rr=2, score=50, structure="",
                missing_steps="", status="win", realized_r=2, kind="retest")
    data.update(overrides)
    return JournalEntry(**data)


class AnalyticsTests(unittest.TestCase):
    def test_old_journal_is_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            save_journal([row()], path)
            restored = load_journal(path)
            self.assertEqual(restored[0].id, "a")
            self.assertEqual(lifecycle(restored[0]), "closed_simulated")

    def test_waiting_not_counted_as_fill(self):
        self.assertEqual(lifecycle(row(status="open")), "waiting_entry")

    def test_net_r_includes_costs(self):
        self.assertAlmostEqual(net_r(row(), fee_pct=0.1, slippage_pct=0.1), 1.8)

    def test_unknown_direction_excluded(self):
        result = report([row(side="none"), row(id="b")])
        strategy = next(g for g in result["groups"] if g["dimension"] == "strategy")
        self.assertEqual(strategy["closed_simulated"], 1)

    def test_duplicate_ids_deduped(self):
        result = report([row(), row(status="loss", realized_r=-1)])
        self.assertEqual(result["duplicate_ids"], 1)
        self.assertEqual(result["signal_count"], 1)

    def test_quality_is_observational(self):
        score, tier = quality(row(missing_steps="03 BOS, 08 回測反應"))
        self.assertEqual(score, 56)
        self.assertEqual(tier, "medium")
        self.assertEqual(row().score, 50)  # original scanner score is independent


if __name__ == "__main__":
    unittest.main()
