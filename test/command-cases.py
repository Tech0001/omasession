#!/usr/bin/env python3
"""Static contracts for app-only CLI routing."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "omasession"


class CommandCases(unittest.TestCase):
    def test_chrome_app_restore_uses_the_native_replay_filter(self) -> None:
        source = BIN.read_text(encoding="utf-8")
        match = re.search(
            r"cmd_restore_app\(\) \{(?P<body>.*?)^\}\n\n#",
            source,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn('google-chrome) filter="google-chrome"', body)
        self.assertIn('python3 "$LIB/replay.py" --app "$filter"', body)
        self.assertNotIn('cmd_restore_browser google-chrome', body)

    def test_panel_chrome_action_uses_app_restore(self) -> None:
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn(
            'command: [root.cliPath, "restore-app", "google-chrome"]',
            panel,
        )

    def test_browser_specs_use_native_restore_without_titles(self) -> None:
        source = (ROOT / "lib" / "replay.py").read_text(encoding="utf-8")
        self.assertIn("if app in BROWSERS:\n", source)
        self.assertNotIn("if app in BROWSERS and title_records:", source)


if __name__ == "__main__":
    unittest.main()
