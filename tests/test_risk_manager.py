"""Risk sizing regression tests. Run: python -m unittest discover -s tests -v"""
import math
import unittest

from risk_manager import RiskConfig, calc_1r_size, filter_yes_by_risk


class RiskSizingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig(account_usdt=1000, risk_pct_per_r=1, max_leverage=3,
                              max_notional_usdt=2000, fee_pct_roundtrip=0.12,
                              slippage_pct_roundtrip=0.10, min_stop_pct=0.1)

    def test_caps_and_cost_budget(self):
        result = calc_1r_size(100, 99.8, self.cfg)
        self.assertIsNotNone(result)
        self.assertLessEqual(result.notional_usdt, 2000)
        self.assertLessEqual(result.leverage_cap, 3)
        self.assertLessEqual(result.estimated_loss_usdt, 10 + 1e-8)

    def test_tiny_stop_rejected(self):
        self.assertIsNone(calc_1r_size(100, 99.99, self.cfg))

    def test_invalid_prices_rejected(self):
        self.assertIsNone(calc_1r_size(float("nan"), 99, self.cfg))
        self.assertIsNone(calc_1r_size(100, 100, self.cfg))

    def test_cost_reduces_notional(self):
        no_cost = RiskConfig(account_usdt=1000, risk_pct_per_r=1, max_leverage=100,
                             max_notional_usdt=100000, fee_pct_roundtrip=0,
                             slippage_pct_roundtrip=0)
        with_cost = RiskConfig(account_usdt=1000, risk_pct_per_r=1, max_leverage=100,
                               max_notional_usdt=100000, fee_pct_roundtrip=0.12,
                               slippage_pct_roundtrip=0.10)
        self.assertLess(calc_1r_size(100, 99, with_cost).notional_usdt,
                        calc_1r_size(100, 99, no_cost).notional_usdt)


if __name__ == "__main__":
    unittest.main()
