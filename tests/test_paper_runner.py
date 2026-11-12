import os
import unittest
from unittest.mock import patch
from signal_tracker import JournalEntry
from paper_trading import default_state
from paper_runner import process

def row():
    return JournalEntry(id="r1", ts="2026-11-10 22:00:00",
        signal_time_ms=1000, symbol="BTCUSDT", side="long", interval="15m",
        entry=100, stop=99, tp=102, planned_rr=2, score=80,
        structure="", missing_steps="", kind="retest", status="open")

class PaperRunnerTests(unittest.TestCase):
    def test_candle_must_be_later_than_signal(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED":"true","TRADING_KILL_SWITCH":"false"}):
            state=default_state()
            self.assertEqual(process(state,[row()],{"BTCUSDT":{"open_time_ms":1000,"low":99,"high":101,"closed":True}}),[])
            self.assertIsNone(state["position"])

    def test_only_closed_candles(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED":"true","TRADING_KILL_SWITCH":"false"}):
            state=default_state()
            self.assertEqual(process(state,[row()],{"BTCUSDT":{"open_time_ms":2000,"low":99,"high":101,"closed":False}}),[])

    def test_quality_comparison_diagnostics(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED":"true", "TRADING_KILL_SWITCH":"false"}):
            state = default_state()
            candidate = row()
            candidate.missing_steps = "BOS"
            output = StringIO()
            with redirect_stdout(output):
                process(state, [candidate], {"BTCUSDT": {"open_time_ms": 2000, "low": 99.9, "high": 101, "closed": True}})
            diagnostic = state["entry_diagnostics"]["counts"]
            self.assertEqual(diagnostic["quality_50_pass"], 1)
            self.assertEqual(diagnostic["quality_65_pass"], 0)
            self.assertEqual(diagnostic["touched_quality_50_pass"], 1)
            self.assertEqual(diagnostic["touched_quality_65_pass"], 0)

    def test_paper_open_with_valid_observation(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED":"true","TRADING_KILL_SWITCH":"false"}):
            state=default_state()
            events=process(state,[row()],{"BTCUSDT":{"open_time_ms":2000,"low":99.9,"high":101,"closed":True}})
            self.assertEqual(len(events),1)
            self.assertEqual(state["position"]["signal_id"],"r1")
            self.assertEqual(events[0]["side"], "long")
            self.assertEqual(events[0]["quantity"], state["position"]["quantity"])

if __name__=="__main__":
    unittest.main()
