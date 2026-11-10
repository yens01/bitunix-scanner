import os
import unittest
from unittest.mock import patch
from paper_trading import default_state, try_open, close_on_bar, PaperConfig

ARGS = dict(signal_id="abc", symbol="BTCUSDT", side="long", entry=100,
            stop=99, tp=102, quality_score=80, kind="retest")

class PaperGuardrailTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(try_open(default_state(), **ARGS)[0])

    def test_kill_switch(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true", "TRADING_KILL_SWITCH": "true"}):
            self.assertFalse(try_open(default_state(), **ARGS)[0])

    def test_live_always_rejected(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true", "TRADING_KILL_SWITCH": "false"}):
            self.assertFalse(try_open(default_state(), **ARGS, live=True)[0])

    def test_single_position_and_cost(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true", "TRADING_KILL_SWITCH": "false"}):
            s = default_state()
            self.assertTrue(try_open(s, **ARGS)[0])
            self.assertFalse(try_open(s, **{**ARGS, "signal_id": "def"})[0])
            self.assertLessEqual(s["position"]["entry"] * s["position"]["quantity"], 50)
            self.assertEqual(close_on_bar(s, high=102, low=99)["reason"], "stop")

    def test_order_limit_and_quality(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true", "TRADING_KILL_SWITCH": "false"}):
            s = default_state()
            self.assertFalse(try_open(s, **{**ARGS, "quality_score": 30})[0])
            s["orders_today"] = 2
            self.assertFalse(try_open(s, **ARGS)[0])

    def test_daily_loss_cap(self):
        with patch.dict(os.environ, {"PAPER_TRADING_ENABLED": "true", "TRADING_KILL_SWITCH": "false"}):
            s = default_state()
            s["realized_loss_today"] = 1.0
            self.assertFalse(try_open(s, **ARGS)[0])

if __name__ == "__main__":
    unittest.main()
