import unittest
from bitunix_scanner import TRADE_SYMBOLS, build_signals, normalize_query_symbol, _tg_reply_keyboard, _tg_inline_menu, build_alt_watchlist_message


class BtcEthOnlyTests(unittest.TestCase):
    def test_only_two_trade_symbols(self):
        self.assertEqual(TRADE_SYMBOLS, {"BTCUSDT", "ETHUSDT"})

    def test_scanner_excludes_small_coins(self):
        tickers = [
            {"symbol": symbol, "lastPrice": "100", "open": "99",
             "high": "101", "low": "98", "quoteVol": "2000000", "baseVol": "20000"}
            for symbol in ("BTCUSDT", "ETHUSDT", "SOXSUSDT", "SOLUSDT")
        ]
        result = build_signals(tickers, [], [], min_quote_vol=0)
        self.assertEqual({s.symbol for s in result}, {"BTCUSDT", "ETHUSDT"})

    def test_manual_symbol_restricted(self):
        self.assertEqual(normalize_query_symbol("BTC"), "BTCUSDT")
        self.assertEqual(normalize_query_symbol("ETHUSDT"), "ETHUSDT")
        self.assertIsNone(normalize_query_symbol("SOXSUSDT"))
        self.assertIsNone(normalize_query_symbol("SOL"))

    def test_telegram_hides_alt_buttons(self):
        self.assertNotIn("小幣", _tg_reply_keyboard())
        self.assertNotIn("小幣", _tg_inline_menu("HOME_MENU")[0] + _tg_inline_menu("HOME_MENU")[1])
        self.assertIn("已停用", build_alt_watchlist_message())


if __name__ == "__main__":
    unittest.main()
