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

    def test_entry_bar_time_survives_and_prevents_same_bar_exit(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true", "TRADING_KILL_SWITCH": "false"}):
            state = default_state()
            first = {"open_time_ms": 2000, "low": 99.9, "high": 101, "closed": True}
            events = process(state, [row()], {"BTCUSDT": [first]})
            self.assertEqual(len(events), 1)
            self.assertEqual(state["position"]["entry_bar_ms"], 2000)
            self.assertEqual(len([e for e in state["events"] if e["type"] == "open"]), 1)
            self.assertEqual(process(state, [row()], {"BTCUSDT": [first]}), [])
            self.assertIsNotNone(state["position"])
            second = {"open_time_ms": 3000, "low": 98.5, "high": 101, "closed": True}
            closed = process(state, [row()], {"BTCUSDT": [first, second]})
            self.assertEqual(len(closed), 1)
            self.assertEqual(closed[0]["reason"], "stop")
            self.assertIsNone(state["position"])
            self.assertEqual(process(state, [], {"BTCUSDT": [first, second]}), [])
            self.assertEqual(len([e for e in state["events"] if e["type"] == "close"]), 1)

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
