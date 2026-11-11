import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bitunix_scanner import _tg_reply_keyboard, _tg_inline_menu, normalize_query_symbol
from signal_tracker import JournalEntry
from telegram_dashboard import (
    build_dashboard_message, equity_curve, latest_signals, performance,
    signal_funnel, strategy_quality, system_status,
)


def signal(id="sig1"):
    return JournalEntry(
        id=id, ts="2026-11-11 12:00:00", signal_time_ms=1794355200000,
        symbol="BTCUSDT", side="long", interval="15m", entry=100,
        stop=99, tp=102, planned_rr=2, score=70,
        structure="偏多", missing_steps="", kind="retest")


class TelegramDashboardTests(unittest.TestCase):
    def test_eight_home_buttons_and_submenus(self):
        keyboard = json.loads(_tg_reply_keyboard())["keyboard"]
        labels = [item["text"] for row in keyboard for item in row]
        self.assertEqual(len(labels), 8)
        self.assertEqual(len(keyboard), 4)
        for name in ("📊 市場總覽", "🎯 最新訊號", "🧪 模擬交易",
                     "📋 日內規劃", "🪙 小幣推薦", "📈 台美股",
                     "📰 財金新聞", "⚙️ 更多功能"):
            self.assertIn(name, labels)
        callbacks = set()
        for menu in ("PAPER_MENU", "STOCK_MENU", "MORE_MENU", "HOME_MENU"):
            _, markup = _tg_inline_menu(menu)
            callbacks.update(item["callback_data"] for row in
                             json.loads(markup)["inline_keyboard"] for item in row)
        for action in ("PAPER", "PERFORMANCE", "EQUITY", "FUNNEL", "QUALITY",
                       "SYSTEM", "TW", "TWHEAT", "USHEAT", "STATS",
                       "PLAN", "ALTS", "NEWS", "LATEST", "HOME_MENU"):
            self.assertIn("menu:" + action, callbacks)

    def test_aliases(self):
        for label, command in (
            ("📊 市場總覽", "STATS"), ("🧪 模擬交易", "PAPER_MENU"),
            ("📈 台美股", "STOCK_MENU"), ("⚙️ 更多功能", "MORE_MENU"),
            ("🎯 最新訊號", "LATEST"), ("💰 交易績效", "PERFORMANCE"),
            ("📈 資金曲線", "EQUITY"), ("🔎 訊號漏斗", "FUNNEL"),
            ("🧠 策略品質", "QUALITY"), ("⚙️ 系統狀態", "SYSTEM"),
            ("/equity", "EQUITY"), ("/system", "SYSTEM")):
            self.assertEqual(normalize_query_symbol(label), command)

    def test_latest_signals_deduplicated_and_escaped(self):
        r = signal()
        r.symbol = "<BTC&USDT>"
        msg = latest_signals([r, r])
        self.assertIn("&lt;BTC&amp;USDT&gt;", msg)
        self.assertEqual(msg.count("進場"), 1)
        self.assertIn("非成交", msg)

    def test_performance_and_curve(self):
        state = {"events": [
            {"type": "open", "signal_id": "a", "symbol": "BTCUSDT"},
            {"type": "close", "signal_id": "a", "estimated_net_pnl_usdt": 0.5}],
            "position": None}
        self.assertIn("+0.5000 USDT", performance(state))
        self.assertIn("50.5000 USDT", equity_curve(state))
        self.assertIn("已平倉", equity_curve(state))

    def test_funnel_distinguishes_unique_and_scans(self):
        state = {"entry_diagnostics": {
            "unique_signals": {"a": {"stage": "opened"}, "b": {"stage": "entry_not_touched"}},
            "counts": {"retest_open": 12}, "risk_rejected": {}}}
        msg = signal_funnel(state)
        self.assertIn("獨立候選：2", msg)
        self.assertIn("候選檢查：12", msg)

    def test_quality_sample_gate(self):
        state = {"events": [
            {"type": "open", "signal_id": "sig1"},
            {"type": "close", "signal_id": "sig1", "estimated_net_pnl_usdt": 0.5}]}
        msg = strategy_quality([signal()], state)
        self.assertIn("樣本不足 20 筆", msg)
        self.assertIn("非預測勝率", msg)

    def test_system_stale_warning_and_no_claim_of_health(self):
        state = {"events": [], "day": "2026-11-11"}
        commit = {"commit": {"committer": {"date": "2026-11-11T00:00:00Z"}}}
        msg = system_status(state, cloud_commit=commit,
                            now=datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc))
        self.assertIn("60 分鐘", msg)
        self.assertIn("超過 45 分鐘", msg)
        self.assertIn("無法從快照直接驗證", msg)

    def test_missing_cloud_fails_closed(self):
        with patch("telegram_dashboard.fetch_snapshot", side_effect=OSError("offline")):
            msg = build_dashboard_message("PERFORMANCE")
        self.assertIn("無法取得", msg)
        self.assertNotIn("0 筆", msg)


if __name__ == "__main__":
    unittest.main()
