import unittest
from paper_status import format_paper_positions
from bitunix_scanner import normalize_query_symbol, _tg_reply_keyboard

class PaperStatusTests(unittest.TestCase):
    def test_keyboard_and_command(self):
        self.assertIn("🧪 模擬倉位", _tg_reply_keyboard())
        self.assertEqual(normalize_query_symbol("🧪 模擬倉位"), "PAPER")

    def test_empty_state(self):
        message = format_paper_positions({"position":None,"events":[],"orders_today":0})
        self.assertIn("目前沒有模擬持倉", message)
        self.assertIn("0 筆", message)

    def test_position_and_history(self):
        state={"position":{"symbol":"BTCUSDT","side":"long","entry":100,
                "stop":99,"tp":102,"quantity":0.1,"opened_at":"2026-11-10T00:00:00+00:00"},
               "events":[{"type":"open","signal_id":"x"},
                         {"type":"close","signal_id":"old","reason":"tp","estimated_net_pnl_usdt":0.2}],
               "orders_today":1}
        message=format_paper_positions(state)
        self.assertIn("BTCUSDT",message)
        self.assertIn("+0.2000",message)
        self.assertIn("1 / 2",message)

if __name__ == "__main__":
    unittest.main()
