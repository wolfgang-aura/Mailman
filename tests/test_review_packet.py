from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from mailman.cli import main
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


class PacketGateTests(unittest.TestCase):
    """The exit code is the gate, so it has to arrive before the page is written.

    `mailman packet` wrote the page and then exited 1, so a call for a batch
    that was not ready destroyed the previous batch's page at the same path.
    https://github.com/wolfgang-aura/Mailman/issues/104
    """

    def _run(self, root: Path, run_id: str, number: int) -> Path:
        """A run `load_run` can read, which the page fixture does not need."""
        directory = write_named_run(root, run_id, number)
        path = directory / "run.json"
        run = json.loads(path.read_text(encoding="utf-8"))
        run["created_at"] = "2026-09-02T00:00:00+00:00"
        run["updated_at"] = "2026-09-02T00:10:00+00:00"
        path.write_text(json.dumps(run), encoding="utf-8")
        return directory

    def _packet(self, root: Path, run_ids: list[str], output: Path) -> tuple[int, str]:
        printed = StringIO()
        with redirect_stdout(printed):
            code = main(
                [
                    "packet",
                    *run_ids,
                    "--output",
                    str(output),
                    "--no-open",
                    "--data-root",
                    str(root),
                ]
            )
        return code, printed.getvalue()

    def test_an_existing_page_survives_a_batch_with_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bare = self._run(root, "20260101T000000Z-000002", 12)
            output = root / "review-packet" / "index.html"
            output.parent.mkdir(parents=True)
            previous = "<html>the previous batch's page</html>"
            output.write_text(previous, encoding="utf-8")
            before = output.read_bytes()

            code, printed = self._packet(root, [bare.name], output)
            after = output.read_bytes()

        self.assertEqual(code, 1)
        self.assertEqual(after, before)
        report = json.loads(printed)
        self.assertIsNone(report["packet"])
        self.assertEqual(report["without_decision"], ["20260101T000000Z-000002"])
        self.assertIn("left alone", report["detail"])

    def test_one_run_without_a_decision_stops_the_whole_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ready = self._run(root, "20260101T000000Z-000001", 11)
            bare = self._run(root, "20260101T000000Z-000002", 12)
            write_decision(ready)
            output = root / "review-packet" / "index.html"

            code, printed = self._packet(root, [ready.name, bare.name], output)
            written = output.exists()

        self.assertEqual(code, 1)
        self.assertFalse(written)
        self.assertEqual(
            json.loads(printed)["without_decision"], ["20260101T000000Z-000002"]
        )

    def test_a_complete_batch_is_written_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = self._run(root, "20260101T000000Z-000001", 11)
            second = self._run(root, "20260101T000000Z-000002", 12)
            write_decision(first)
            write_decision(second)
            output = root / "review-packet" / "index.html"

            code, printed = self._packet(root, [first.name, second.name], output)
            page = output.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed)["without_decision"], [])
        self.assertIn("20260101T000000Z-000002", page)
