import unittest
from trade_kline_audit import fetch_candles, diagnose, INTERVAL_MS

class AuditTests(unittest.TestCase):
    def test_exclusive_end_and_pagination(self):
        def fake(params):
            self.assertEqual(params["interval"], "5m")
            return [{"time": t, "high": "102", "low": "98", "close": "101"}
                    for t in range(max(0, params["endTime"] - 2*INTERVAL_MS), params["endTime"], INTERVAL_MS)]
        bars = fetch_candles("BTCUSDT", 0, 4*INTERVAL_MS, sleep=0, fetch=fake)
        self.assertEqual([b["time"] for b in bars], [0, INTERVAL_MS, 2*INTERVAL_MS, 3*INTERVAL_MS])

    def test_next_bar_only_and_adverse_add(self):
        bars = [{"time": i*INTERVAL_MS, "high": 105, "low": 95, "close": 99} for i in range(15)]
        orders = [{"ts": 1, "symbol": "BTCUSDT", "side": "open long", "price": 100, "qty": 1},
                  {"ts": INTERVAL_MS+1, "symbol": "BTCUSDT", "side": "open long", "price": 98, "qty": 1}]
        out = diagnose(orders, {"BTCUSDT": bars})
        self.assertEqual(out[0]["return_15m_pct"], -1.0)
        self.assertEqual(out[1]["added_against_last_entry"], True)
        self.assertEqual(out[0]["adverse_15m_pct"], -5.0)

if __name__ == "__main__":
    unittest.main()
