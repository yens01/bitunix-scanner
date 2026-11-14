import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
import os
from radar_notify import select_alerts, deliver, format_alert


class RadarNotifyTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"candidates": [
            {"symbol": "EVAAUSDT", "stage": "爆發初期", "score": 99,
             "last_closed_price": 1.1569, "resistance": 1.1316,
             "breakout_pct": 2.24, "volume_ratio": 6.41,
             "change_24h_pct": 9.95},
            {"symbol": "TESTUSDT", "stage": "潛伏", "score": 70}]}

    def test_new_breakout_only_and_cooldown(self):
        self.assertEqual(len(select_alerts(self.snapshot, {}, now=100000)), 1)
        self.assertEqual(select_alerts(self.snapshot, {"last_alert_at": {"EVAAUSDT": 99999}},
                                       now=100000), [])

    def test_message_contains_breakout_details(self):
        msg = format_alert(self.snapshot["candidates"][0])
        self.assertIn("EVAAUSDT", msg)
        self.assertIn("1.1316", msg)
        self.assertIn("6.41", msg)
        self.assertIn("\n", msg)

    def test_success_persists_and_does_not_resend(self):
        with TemporaryDirectory() as directory:
            old = os.getcwd()
            try:
                os.chdir(directory)
                sent = []
                state = {}
                deliver(self.snapshot, state, sent.append, now=100000)
                deliver(self.snapshot, state, sent.append, now=100001)
                self.assertEqual(len(sent), 1)
                self.assertTrue(Path("radar_alert_state.json").exists())
            finally:
                os.chdir(old)

    def test_failure_does_not_acknowledge(self):
        with TemporaryDirectory() as directory:
            old = os.getcwd()
            try:
                os.chdir(directory)
                state = {}
                with self.assertRaises(RuntimeError):
                    deliver(self.snapshot, state, lambda msg: (_ for _ in ()).throw(RuntimeError("fail")), now=100000)
                self.assertNotIn("EVAAUSDT", state["last_alert_at"])
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
