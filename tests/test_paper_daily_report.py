import unittest
from paper_daily_report import daily_message
from paper_trading import default_state


class DailyReportTests(unittest.TestCase):
    def test_empty_account_is_explicit_and_html_safe(self):
        state = default_state()
        state["entry_diagnostics"] = {"counts": {"retest_open": 3, "opened": 0},
                                      "risk_rejected": {"<bad>": 1}}
        msg = daily_message(state)
        self.assertIn("已平倉：0 筆", msg)
        self.assertIn("&lt;bad&gt;", msg)
        self.assertIn("非當日績效", msg)


if __name__ == "__main__":
    unittest.main()
