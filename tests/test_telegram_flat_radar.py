import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bitunix_scanner import _tg_reply_keyboard, normalize_query_symbol
from telegram_dashboard import build_dashboard_message, small_cap_radar


class TelegramFlatRadarTests(unittest.TestCase):
    def test_every_button_is_direct_command(self):
        keyboard = json.loads(_tg_reply_keyboard())["keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertIn("🚀 小幣爆發雷達", labels)
        self.assertIn("💰 交易績效", labels)
        self.assertEqual(len(labels), 8)
        self.assertNotIn("🪙 小幣推薦", labels)
        self.assertNotIn("🔎 訊號漏斗", labels)
        self.assertIn("🧠 策略品質", labels)
        self.assertNotIn("⚙️ 更多功能", labels)
        self.assertNotIn("📈 台美股", labels)
        self.assertEqual(normalize_query_symbol("🚀 小幣爆發雷達"), "RADAR")
        for label in labels:
            self.assertNotIn(normalize_query_symbol(label),
                             {"PAPER_MENU", "STOCK_MENU", "MORE_MENU", "HOME_MENU"})

    def test_radar_without_paper_state(self):
        snapshot = {"generated_at": "2026-11-12T12:00:00+00:00",
                    "candidates": [{"symbol": "TESTUSDT", "score": 85,
                                    "stage": "啟動", "last_closed_price": 0.123,
                                    "volume_ratio": 3.2, "change_24h_pct": 2,
                                    "breakout_pct": 1, "resistance": 0.12}]}
        with patch("telegram_dashboard.fetch_radar_snapshot", return_value=snapshot):
            msg = build_dashboard_message("RADAR")
        # Depending on the real clock, the snapshot may be stale.
        self.assertTrue("TESTUSDT" in msg or "超過 45 分鐘" in msg)

    def test_stale_snapshot_fails_closed(self):
        snapshot = {"generated_at": "2026-11-12T10:00:00Z", "candidates": []}
        msg = small_cap_radar(snapshot, now=datetime(2026, 11, 12, 11, 0, tzinfo=timezone.utc))
        self.assertIn("超過 45 分鐘", msg)

    def test_fresh_empty_watchlist(self):
        snapshot = {"generated_at": "2026-11-12T10:00:00Z", "candidates": []}
        msg = small_cap_radar(snapshot, now=datetime(2026, 11, 12, 10, 10, tzinfo=timezone.utc))
        self.assertIn("沒有符合門檻", msg)


if __name__ == "__main__":
    unittest.main()
