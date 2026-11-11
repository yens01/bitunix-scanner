import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from paper_comparison import build_comparison_message


class PaperComparisonTests(unittest.TestCase):
    def test_separates_signal_results_and_paper_fills(self):
        rows = [SimpleNamespace(ts="2026-11-11 08:21:00", status="win",
                                side="short", realized_r=2.5),
                SimpleNamespace(ts="2026-11-11 09:01:00", status="open",
                                side="short", realized_r=0)]
        state = {"events": [
            {"type": "open", "signal_id": "a", "symbol": "ONEUSDT", "side": "short"},
            {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": 0.37}],
            "entry_diagnostics": {"counts": {"retest_open": 8, "no_candle": 3,
                                             "entry_not_touched": 4, "opened": 1},
                                  "risk_rejected": {}}}
        msg = build_comparison_message(rows, state,
            now=datetime(2026, 11, 11, tzinfo=timezone(timedelta(hours=8))))
        self.assertIn("+2.50R", msg)
        self.assertIn("+0.3700 USDT", msg)
        self.assertIn("候選檢查：8", msg)
        self.assertIn("累積掃描次數，非獨立訊號", msg)

    def test_empty_state(self):
        msg = build_comparison_message([], {"events": []},
            now=datetime(2026, 11, 11, tzinfo=timezone(timedelta(hours=8))))
        self.assertIn("勝率：—", msg)
        self.assertIn("尚無累積診斷", msg)


if __name__ == "__main__":
    unittest.main()
