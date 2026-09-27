from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.prescreen import (
    PRESCREEN_SCHEMA_VERSION,
    REJECTED_BY_COORDINATOR,
    check,
    load_prescreen,
    prescreen_path,
    reject_by_hand,
)


class RejectByHandTests(unittest.TestCase):
    """Hunt 20260927T212801Z-67a9aa turned six issues down by hand and
    recorded none, so `hunt targets` offered all six again."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_the_rejection_is_stored_where_hunt_targets_looks(self) -> None:
        record = reject_by_hand(
            self.root,
            "marimo-team/marimo#6250",
            reason="Postgres drops the offset, not marimo; the fix is a new option",
            evidence="https://github.com/marimo-team/marimo/issues/6250",
        )

        self.assertTrue(prescreen_path(self.root, "marimo-team/marimo", 6250).is_file())
        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [REJECTED_BY_COORDINATOR])
        self.assertEqual(record["schema_version"], PRESCREEN_SCHEMA_VERSION)
        self.assertIn("Postgres", record["next"])
        _, refusal = check(self.root, "marimo-team/marimo#6250")
        self.assertIn(REJECTED_BY_COORDINATOR, refusal)

    def test_a_machine_screen_keeps_its_evidence(self) -> None:
        path = prescreen_path(self.root, "example/project", 7)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": PRESCREEN_SCHEMA_VERSION,
                    "repository": "example/project",
                    "issue_number": 7,
                    "verdict": "pass",
                    "blocking": [],
                    "claims": {"comments_read": 4},
                }
            ),
            encoding="utf-8",
        )

        reject_by_hand(self.root, "example/project#7", reason="the reporter's own mistake")
        reject_by_hand(self.root, "example/project#7", reason="the reporter's own mistake")

        stored = load_prescreen(self.root, "example/project", 7)
        self.assertEqual(stored["claims"], {"comments_read": 4})
        self.assertEqual(stored["blocking"], [REJECTED_BY_COORDINATOR])

    def test_a_rejection_needs_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            reject_by_hand(self.root, "example/project#7", reason="no")


if __name__ == "__main__":
    unittest.main()
