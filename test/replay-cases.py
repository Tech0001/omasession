#!/usr/bin/env python3
"""Behavioral contracts for browser title matching."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import replay  # noqa: E402


class ReplayCases(unittest.TestCase):
    def test_exact_title_is_reserved_before_approximate_match(self) -> None:
        windows = [
            {"title": "Report draft", "address": "0xa"},
            {"title": "Report", "address": "0xb"},
        ]
        candidates = [
            {"title": "Report", "workspace": "2"},
            {"title": "Report draft copy", "workspace": "3"},
        ]

        matches, remaining = replay.plan_browser_matches(windows, candidates)

        self.assertEqual(matches[1]["workspace"], "2")
        self.assertEqual(matches[0]["workspace"], "3")
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
