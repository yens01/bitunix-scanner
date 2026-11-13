import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bitunix_scanner import _plan_coin_block, _session_action_brief


class SessionActionPlanTests(unittest.TestCase):
    def test_asia_london_and_instructions_preserved(self):
        r = SimpleNamespace(
            structure="偏空", zone_lo=68000.0, zone_hi=69300.0,
            plan_session_hl=[("亞洲", 68200.0, 69300.0, False),
                             ("倫敦", 68100.0, 69000.0, True)])
        lines = "\n".join(_session_action_brief(r, "距離過遠", html=False))
        for term in ("亞洲", "倫敦", "68,000", "15m", "距離過遠", "禁止追空"):
            self.assertIn(term, lines)

    def test_existing_fibonacci_still_in_plan(self):
        r = SimpleNamespace(
            symbol="BTCUSDT", structure="偏空", side="short",
            week_rhythm="", zone_lo=68000., zone_hi=69300.,
            plan_day_lo=67500., plan_day_hi=70000.,
            plan_session_hl=[("亞洲", 68000., 69000., False)],
            entry=69000., stop=69500., tp=68000.)
        with patch("bitunix_scanner._plan_htf_bias_line", return_value="偏空"), \
             patch("bitunix_scanner._intraday_level_status", return_value=("距離過遠", None)), \
             patch("bitunix_scanner._fetch_plan_sr_levels", return_value=([], [])):
            text = "\n".join(_plan_coin_block(r))
        self.assertIn("Fibo", text)
        self.assertIn("亞洲", text)
        self.assertIn("今晚操作順序", text)
        self.assertNotIn("🎯 進", text)


if __name__ == "__main__":
    unittest.main()
