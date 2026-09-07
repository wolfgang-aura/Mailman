from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.review_packet import collect, render_packet_page, write_packet_page
from tests.test_review_page import write_decision, write_run


def write_named_run(root: Path, run_id: str, number: int) -> Path:
    """A second, third, fourth run beside the fixture's own."""
    directory = write_run(root)
    renamed = root / run_id
    directory.rename(renamed)
    run = json.loads((renamed / "run.json").read_text(encoding="utf-8"))
    run["run_id"] = run_id
    (renamed / "run.json").write_text(json.dumps(run), encoding="utf-8")
    issue = json.loads((renamed / "issue.json").read_text(encoding="utf-8"))
    issue["reference"]["number"] = number
    issue["reference"]["url"] = f"https://github.com/example/project/issues/{number}"
    (renamed / "issue.json").write_text(json.dumps(issue), encoding="utf-8")
    return renamed


class PacketTests(unittest.TestCase):
    def test_questions_are_numbered_once_across_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = write_named_run(root, "20260101T000000Z-000001", 11)
            second = write_named_run(root, "20260101T000000Z-000002", 12)
            write_decision(first)
            write_decision(second)

            entries = collect([first, second])

        self.assertEqual([entry.first_question for entry in entries], [1, 2])

    def test_the_rollup_counts_what_is_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = write_named_run(root, "20260101T000000Z-000001", 11)
            second = write_named_run(root, "20260101T000000Z-000002", 12)
            write_decision(first)
            write_decision(second, recommendation="HOLD")

            page = render_packet_page([first, second])

        self.assertIn("Runs waiting", page)
        self.assertIn("Questions that block", page)
        # two runs, one of them recommended to send
        self.assertIn('<span class="value">2</span>', page)
        self.assertIn('<span class="value">1</span>', page)

    def test_a_run_without_a_decision_is_listed_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ready = write_named_run(root, "20260101T000000Z-000001", 11)
            bare = write_named_run(root, "20260101T000000Z-000002", 12)
            write_decision(ready)

            page = render_packet_page([ready, bare])

        self.assertIn("Not ready to decide", page)
        self.assertIn("20260101T000000Z-000002", page)

    def test_links_are_relative_to_where_the_packet_lands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = write_named_run(root, "20260101T000000Z-000001", 11)
            write_decision(run_directory)
            destination = write_packet_page(
                [run_directory], root / "packet" / "index.html"
            )
            page = destination.read_text(encoding="utf-8")

        self.assertIn("../20260101T000000Z-000001/review.html", page)


if __name__ == "__main__":
    unittest.main()
