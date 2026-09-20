#!/usr/bin/env python3
"""Static contracts for the app controls in Panel.qml."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PANEL = Path(__file__).resolve().parents[1] / "Panel.qml"


class PanelCases(unittest.TestCase):
    def test_app_toggles_are_blocked_when_the_cli_is_unavailable(self) -> None:
        source = PANEL.read_text(encoding="utf-8")
        for toggle_id in ("chromeToggle", "ghosttyToggle"):
            match = re.search(
                rf"id:\s*{toggle_id}\b(?P<body>.*?)(?=\n\s*foreground:)",
                source,
                re.DOTALL,
            )
            self.assertIsNotNone(match, toggle_id)
            enabled = re.search(r"\benabled:\s*(?P<expr>.*)$",
                                match.group("body"), re.DOTALL)
            self.assertIsNotNone(enabled, toggle_id)
            expression = enabled.group("expr")
            self.assertIn("!root.mockMode", expression, toggle_id)
            self.assertIn("!root.cliMissing", expression, toggle_id)


if __name__ == "__main__":
    unittest.main()
