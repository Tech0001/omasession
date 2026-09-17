#!/usr/bin/env python3
"""Pure checks for the live browser-placement repair planner."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import browser_repair as repair  # noqa: E402


def window(title: str, workspace: int, address: str = "0x1") -> dict:
    return {
        "class": "chromium",
        "title": title,
        "address": address,
        "workspace": {"id": workspace},
        "mapped": True,
        "pid": os.getpid(),
    }


class BrowserRepairCases(unittest.TestCase):
    def test_matches_are_one_to_one_and_keep_saved_workspace(self) -> None:
        current = [window("Docs", 1, "0x1"), window("Mail", 4, "0x2")]
        saved = [
            {"class": "chromium", "title": "Docs", "workspace": 3},
            {"class": "chromium", "title": "Mail", "workspace": 4},
        ]

        matches = repair.match_plan(current, saved)
        moves = repair.repair_plan(current, saved)

        self.assertEqual([target["title"] for _, target in matches], ["Docs", "Mail"])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0]["title"], "Docs")
        self.assertEqual(moves[0][1]["workspace"], 3)

    def test_duplicate_titles_do_not_reuse_a_saved_slot(self) -> None:
        current = [window("New Tab", 1, "0x1"), window("New Tab", 2, "0x2")]
        saved = [{"class": "chromium", "title": "New Tab", "workspace": 5}]

        self.assertEqual(len(repair.match_plan(current, saved)), 1)
        self.assertEqual(len(repair.repair_plan(current, saved)), 1)

    def test_unmatched_windows_are_left_alone(self) -> None:
        current = [window("A page the snapshot never saw", 7)]
        saved = [{"class": "chromium", "title": "Saved page", "workspace": 2}]

        self.assertEqual(repair.match_plan(current, saved), [])
        self.assertEqual(repair.repair_plan(current, saved), [])

    def test_exact_title_wins_before_a_fuzzy_title_can_steal_it(self) -> None:
        current = [window("Docs Pro", 1, "0x1"), window("Docs", 1, "0x2")]
        saved = [
            {"class": "chromium", "title": "Docs", "workspace": 3},
            {"class": "chromium", "title": "Docs Project", "workspace": 4},
        ]

        matches = repair.match_plan(current, saved)
        self.assertEqual(
            [(window["title"], target["title"]) for window, target in matches],
            [("Docs", "Docs"), ("Docs Pro", "Docs Project")],
        )

    def test_duplicate_exact_titles_reserve_all_equal_slots(self) -> None:
        current = [
            window("Docs Pro", 1, "0x1"),
            window("Docs", 1, "0x2"),
            window("Docs", 1, "0x3"),
        ]
        saved = [
            {"class": "chromium", "title": "Docs", "workspace": 3},
            {"class": "chromium", "title": "Docs", "workspace": 4},
            {"class": "chromium", "title": "Docs Project", "workspace": 5},
        ]

        matches = repair.match_plan(current, saved)
        self.assertEqual(
            sorted((window["title"], target["title"]) for window, target in matches),
            [("Docs", "Docs"), ("Docs", "Docs"), ("Docs Pro", "Docs Project")],
        )

    def test_proc_starttime_is_a_generation_token(self) -> None:
        token = repair.proc_starttime(os.getpid())
        self.assertIsNotNone(token)
        self.assertTrue(token.isdigit())

    def test_client_filter_excludes_unmapped_and_scratchpad(self) -> None:
        clients = [
            window("visible", 1),
            {"mapped": False, "workspace": {"id": 2}},
            {"mapped": True, "workspace": {"id": -99}},
        ]

        self.assertEqual(len(repair.mapped_clients(clients)), 1)
        self.assertEqual(repair.mapped_clients(clients)[0]["title"], "visible")

    def test_partial_reopen_stays_pending_until_all_saved_titles_arrive(self) -> None:
        token = f"{os.getpid()}:{repair.proc_starttime(os.getpid())}"
        state = {
            "browsers": {"chromium": {"identity": ["old:1"]}},
            "layout": {
                "chromium": [
                    {"class": "chromium", "title": "Docs", "workspace": 3},
                    {"class": "chromium", "title": "Mail", "workspace": 4},
                ]
            },
            "pending": {},
        }
        first = [window("Docs", 1)]
        with patch.object(repair, "REPAIR_TIMEOUT", 0), patch.object(
            repair, "time", wraps=repair.time
        ) as clock, patch.object(repair.R, "move_to") as move:
            clock.time.return_value = 10
            changed, pending = repair.process_app("chromium", first, state, False, False)
        self.assertFalse(changed)
        self.assertTrue(pending)
        self.assertEqual(move.call_count, 0)
        self.assertEqual(state["browsers"]["chromium"]["identity"], ["old:1"])

        second = [window("Docs", 1), window("Mail", 1, "0x2")]
        # The persisted pending timestamp is deliberately old enough to be
        # retained, while this invocation observes the complete saved set.
        state["pending"]["chromium"]["since"] = 10
        verified = [window("Docs", 3), window("Mail", 4, "0x2")]
        with patch.object(repair, "REPAIR_TIMEOUT", 0), patch.object(
            repair.R, "move_to"
        ) as move, patch.object(
            repair, "read_clients", side_effect=[second, second, second, verified]
        ):
            changed, pending = repair.process_app("chromium", second, state, False, False)
        self.assertTrue(changed)
        self.assertFalse(pending)
        self.assertEqual(move.call_count, 2)
        self.assertEqual(state["browsers"]["chromium"]["identity"], [token])

    def test_ipc_read_failure_never_counts_as_give_up(self) -> None:
        state = {
            "browsers": {"chromium": {"identity": ["old:1"]}},
            "layout": {"chromium": [{"class": "chromium", "title": "Docs", "workspace": 3}]},
            "pending": {"chromium": {"identity": ["new:2"], "since": 1}},
        }
        current = [window("Docs", 1)]
        with patch.object(repair, "REPAIR_TIMEOUT", 0), patch.object(
            repair, "read_clients", return_value=None
        ):
            changed, pending = repair.process_app("chromium", current, state, False, False)

        self.assertFalse(changed)
        self.assertTrue(pending)
        self.assertEqual(state["browsers"]["chromium"]["identity"], ["old:1"])

    def test_manual_action_does_not_require_a_new_browser_generation(self) -> None:
        token = f"{os.getpid()}:{repair.proc_starttime(os.getpid())}"
        state = {
            "browsers": {"chromium": {"identity": [token]}},
            "layout": {"chromium": [{"class": "chromium", "title": "Docs", "workspace": 3}]},
            "pending": {},
        }
        current = [window("Docs", 1)]
        verified = [window("Docs", 1)]
        after_move = [window("Docs", 3)]
        with patch.object(repair, "REPAIR_TIMEOUT", 0), patch.object(
            repair, "read_clients", side_effect=[verified, verified, after_move]
        ), patch.object(repair.R, "move_to") as move:
            changed, pending = repair.process_app(
                "chromium", current, state, False, False, force=True
            )

        self.assertTrue(changed)
        self.assertFalse(pending)
        move.assert_called_once_with("0x1", 3)

    def test_manual_action_applies_matches_when_a_saved_window_is_absent(self) -> None:
        token = f"{os.getpid()}:{repair.proc_starttime(os.getpid())}"
        state = {
            "browsers": {"chromium": {"identity": [token]}},
            "layout": {
                "chromium": [
                    {"class": "chromium", "title": "Docs", "workspace": 3},
                    {"class": "chromium", "title": "Mail", "workspace": 4},
                ]
            },
            "pending": {},
        }
        current = [window("Docs", 1)]
        after_move = [window("Docs", 3)]
        with patch.object(repair, "REPAIR_TIMEOUT", 0), patch.object(
            repair, "read_clients", side_effect=[current, current, after_move]
        ), patch.object(repair.R, "move_to") as move:
            changed, pending = repair.process_app(
                "chromium", current, state, False, False, force=True
            )

        self.assertTrue(changed)
        self.assertFalse(pending)
        move.assert_called_once_with("0x1", 3)

    def test_move_stops_when_a_window_changes_after_planning(self) -> None:
        token = f"{os.getpid()}:{repair.proc_starttime(os.getpid())}"
        state = {
            "browsers": {"chromium": {"identity": [token]}},
            "layout": {
                "chromium": [
                    {"class": "chromium", "title": "Docs", "workspace": 3},
                    {"class": "chromium", "title": "Mail", "workspace": 4},
                ]
            },
            "pending": {},
        }
        current = [window("Docs", 1, "0x1"), window("Mail", 1, "0x2")]
        changed = [window("Docs", 1, "0x1"), window("Different page", 8, "0x2")]
        with patch.object(repair, "REPAIR_TIMEOUT", 0), patch.object(
            repair, "read_clients", side_effect=[current, current, changed]
        ), patch.object(repair.R, "move_to") as move:
            did_change, pending = repair.process_app(
                "chromium", current, state, False, False, force=True
            )

        self.assertTrue(did_change)
        self.assertTrue(pending)
        move.assert_called_once_with("0x1", 3)

    def test_move_rejects_a_reused_pid_or_missing_starttime(self) -> None:
        expected = window("Docs", 1)
        observed = window("Docs", 1)
        with patch.object(repair, "proc_starttime", return_value="10"):
            expected["_identity"] = repair.client_identity(expected)
        with patch.object(repair, "proc_starttime", return_value="20"):
            self.assertFalse(repair.same_window_source(expected, observed))
        with patch.object(repair, "proc_starttime", return_value=None):
            self.assertFalse(repair.same_window_source(expected, observed))


if __name__ == "__main__":
    unittest.main()
