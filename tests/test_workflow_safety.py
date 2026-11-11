"""Guard against accidentally overwriting durable paper state from manual scan."""
import unittest
from pathlib import Path


class WorkflowSafetyTests(unittest.TestCase):
    def test_manual_scan_cannot_publish_cloud_journal(self):
        workflow = Path(".github/workflows/scan.yml").read_text(encoding="utf-8")
        self.assertNotIn("git push --force", workflow)
        self.assertNotIn("cloud/paper_state.json", workflow)
        self.assertNotIn("git checkout --orphan cloud-journal", workflow)

    def test_auto_scan_restores_paper_before_simulation(self):
        workflow = Path(".github/workflows/auto-scan.yml").read_text(encoding="utf-8")
        self.assertLess(workflow.index("Restore paper state from cloud journal"),
                        workflow.index("Paper trade simulation"))
        self.assertLess(workflow.index("Publish journal snapshot branch"),
                        workflow.index("Telegram paper open/close notifications"))


if __name__ == "__main__":
    unittest.main()
