import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from argparse import Namespace
from pathlib import Path

import daily_digest


class DailyDigestWhoopTests(unittest.TestCase):
    def test_collect_whoop_preserves_current_metric_names(self):
        payload = {
            "latest": {
                "date": "2026-05-07",
                "color": "red",
                "recovery": 28,
                "hrv_rmssd": 31,
                "rhr": 64,
                "strain": 14.2,
            },
            "trends": {},
            "streaks": {"red_recovery_days": 2},
            "insights": ["Bias toward recovery today."],
            "recent_days": [],
            "recent_workouts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            monitor = Path(tmp) / "whoop_monitor.py"
            monitor.write_text(
                "import json\n"
                f"print(json.dumps({json.dumps(payload)}))\n",
                encoding="utf-8",
            )
            old_bin = daily_digest.WHOOP_MONITOR_BIN
            old_python = daily_digest.WHOOP_PYTHON_BIN
            try:
                daily_digest.WHOOP_MONITOR_BIN = monitor
                daily_digest.WHOOP_PYTHON_BIN = sys.executable
                result = daily_digest.collect_whoop()
            finally:
                daily_digest.WHOOP_MONITOR_BIN = old_bin
                daily_digest.WHOOP_PYTHON_BIN = old_python

        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 1)
        self.assertIn("HRV 31 ms", result.items[0]["title"])
        self.assertIn("RHR 64", result.items[0]["title"])
        self.assertIn("strain 14.2", result.items[0]["title"])

    def test_fallback_digest_formats_whoop_metrics_and_removes_dry_run_placeholder(self):
        packet = {
            "sources": [
                {
                    "label": "WHOOP",
                    "items": [
                        {
                            "key": "whoop:2026-05-07",
                            "title": "WHOOP RED | 28% recovery | HRV 31 ms | RHR 64 | strain 14.2",
                            "latest": {
                                "color": "red",
                                "recovery": 28,
                                "hrv_rmssd": 31,
                                "rhr": 64,
                                "sleep_performance": 72,
                                "strain": 14.2,
                            },
                            "insights": ["Bias toward recovery today."],
                            "pattern_insights": ["HRV is trending down over 7 days versus 30 days (28 vs 33 ms, -5 ms)."],
                        }
                    ],
                },
                {"label": "Weather", "items": []},
                {"label": "Calendar", "items": []},
                {"label": "iCloud Mail", "items": []},
                {"label": "Gmail", "items": []},
                {"label": "Blogs / News", "items": []},
                {"label": "Inbox Cleanup", "items": []},
                {"label": "Tasks", "items": []},
            ],
            "errors": [],
        }
        digest = daily_digest.fallback_digest(packet)
        telegram = daily_digest.render_telegram(digest, dry_run=True)

        self.assertIn("HRV 31 ms", digest["markdown"])
        self.assertIn("RHR 64", digest["markdown"])
        self.assertIn("sleep 72%", digest["markdown"])
        self.assertIn("strain 14.2", digest["markdown"])
        self.assertIn("Trend: HRV is trending down", digest["markdown"])
        self.assertNotIn("{{NOTION_URL}}", telegram)
        self.assertIn("[DRY RUN] Notion was not written.", telegram)

    def test_inbox_cleanup_defaults_to_dry_run(self):
        with mock.patch.dict(os.environ, {"INBOX_CLEANUP_APPLY": ""}):
            parser = daily_digest.build_arg_parser()
            args = parser.parse_args([])
        self.assertFalse(args.inbox_cleanup_apply)


if __name__ == "__main__":
    unittest.main()
