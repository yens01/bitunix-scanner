import unittest
from paper_notify import format_event


class PaperNotifyTests(unittest.TestCase):
    def test_open_contains_full_position(self):
        e = dict(type="paper_open", symbol="ONEUSDT", signal_id="abc",
                 side="short", entry=0.0015594, stop=0.0015729,
                 tp=0.0015308, quantity=14744.0)
        msg = format_event(e)
        for value in ("做空 SHORT", "0.0015594", "0.0015729",
                      "0.0015308", "14,744.000000", "倉位名目價值",
                      "預估停損損失", "abc", "非真實委託"):
            self.assertIn(value, msg)

    def test_close_contains_exit_and_pnl(self):
        msg = format_event(dict(type="close", signal_id="abc", symbol="ONEUSDT",
                                side="short", entry=0.0015594, stop=0.0015729,
                                tp=0.0015308, quantity=14744, reason="tp",
                                exit=0.0015308, estimated_net_pnl_usdt=0.2))
        self.assertIn("停利 TP", msg)
        self.assertIn("0.2 USDT", msg)
        self.assertIn("做空 SHORT", msg)

    def test_old_event_is_readable(self):
        self.assertIn("未提供", format_event(dict(type="paper_open", symbol="ONEUSDT", signal_id="abc")))


if __name__ == "__main__":
    unittest.main()
