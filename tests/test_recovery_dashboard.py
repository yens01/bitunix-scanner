import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bitunix_scanner import _tg_reply_keyboard, _tg_inline_menu, normalize_query_symbol
from recovery_dashboard import goal_message, entry_watch_message, build_recovery_message


class RecoveryButtonsTests(unittest.TestCase):
    def test_keyboard_and_callbacks(self):
        labels = [x["text"] for row in json.loads(_tg_reply_keyboard())["keyboard"] for x in row]
        self.assertIn("🎯 我的目標", labels)
        self.assertIn("🕒 今日進場觀察", labels)
        _, markup = _tg_inline_menu("HOME_MENU")
        actions = [x["callback_data"] for row in json.loads(markup)["inline_keyboard"] for x in row]
        self.assertIn("menu:GOAL", actions)
        self.assertIn("menu:ENTRY_WATCH", actions)
        self.assertEqual(normalize_query_symbol("🎯 我的目標"), "GOAL")
        self.assertEqual(normalize_query_symbol("🕒 今日進場觀察"), "ENTRY_WATCH")

    def test_goal_separates_historical_from_paper(self):
        state = {"events": [{"type": "close", "estimated_net_pnl_usdt": 0.25}]}
        msg = goal_message(state)
        self.assertIn("11,493.26", msg)
        self.assertIn("+0.2500", msg)
        self.assertIn("15.2500", msg)
        self.assertIn("分開計算", msg)

    def test_no_open_when_halted_or_position_or_limit(self):
        now = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)
        for state, expected in [
            ({"events": [], "halted": True}, "已停止"),
            ({"events": [], "position": {"symbol": "BTCUSDT"}}, "禁止加碼"),
            ({"events": [], "orders_today": 2}, "兩筆上限"),
            ({"events": []}, "尚未驗證"),
        ]:
            with self.subTest(expected=expected):
                self.assertIn(expected, entry_watch_message(state, now=now))
        self.assertIn("10:00", entry_watch_message({"events": []}, now=now))

    def test_missing_state_fails_closed(self):
        with patch("recovery_dashboard.fetch_snapshot", side_effect=OSError("offline")):
            msg = build_recovery_message("ENTRY_WATCH")
        self.assertIn("無法驗證", msg)
        self.assertNotIn("可以開", msg)

    def test_invalid_pnl_fails_closed(self):
        with patch("recovery_dashboard.fetch_snapshot", return_value={
            "events": [{"type": "close", "estimated_net_pnl_usdt": "nan"}]}):
            self.assertIn("無法驗證", build_recovery_message("GOAL"))


if __name__ == "__main__":
    unittest.main()
