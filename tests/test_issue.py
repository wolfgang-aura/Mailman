from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run
from mailman.issue import (
    capture_issue_from_file,
    capture_issue_from_github,
    load_issue_record,
    parse_issue_url,
    render_issue,
)


ISSUE_URL = "https://github.com/example/project/issues/7"


def write_stub_github_cli(directory: Path, payload: str) -> Path:
    """Write a fake `gh` that prints one captured payload, without a shell."""
    (directory / "payload.json").write_text(payload, encoding="utf-8")
    if sys.platform == "win32":
        stub = directory / "gh.cmd"
        stub.write_text(
            "@echo off\r\ntype \"%~dp0payload.json\"\r\n", encoding="utf-8"
        )
        return stub
    stub = directory / "gh.sh"
    stub.write_text(
        '#!/bin/sh\ncat "$(dirname "$0")/payload.json"\n', encoding="utf-8"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def make_run(root: Path):
    return create_run(
        repository="https://github.com/example/project.git",
        issue=ISSUE_URL,
        base_commit="a" * 40,
        primary="codex",
        reviewer="claude",
        data_root=root,
    )


class IssueUrlTests(unittest.TestCase):
    def test_parses_owner_repository_and_number(self) -> None:
        reference = parse_issue_url(ISSUE_URL)
        self.assertEqual(reference.owner, "example")
        self.assertEqual(reference.repository, "project")
        self.assertEqual(reference.number, 7)
        self.assertEqual(reference.slug, "example/project#7")

    def test_rejects_pull_request_and_foreign_hosts(self) -> None:
        for url in (
            "https://github.com/example/project/pull/7",
            "https://gitlab.com/example/project/issues/7",
            "https://github.com/example/project/issues/0",
            "http://github.com/example/project/issues/7",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "GitHub issue URL"):
                parse_issue_url(url)


class IssueRenderingTests(unittest.TestCase):
    def test_redacts_credentials_and_records_capture_boundary(self) -> None:
        markdown = render_issue(
            parse_issue_url(ISSUE_URL),
            {
                "title": "Crash on empty input",
                "body": "Run it with api_key=abcdef0123456789 and it crashes.",
                "state": "OPEN",
                "author": {"login": "reporter"},
                "labels": [{"name": "bug"}, {"name": "good first issue"}],
            },
            source="github-cli",
            captured_at="2026-09-02T00:00:00+00:00",
        )
        self.assertIn("example/project#7: Crash on empty input", markdown)
        self.assertIn("- Labels: bug, good first issue", markdown)
        self.assertNotIn("abcdef0123456789", markdown)
        self.assertIn("[REDACTED_SECRET]", markdown)
        self.assertIn("accepted upstream fix are deliberately absent", markdown)

    def test_renders_an_empty_body(self) -> None:
        markdown = render_issue(
            parse_issue_url(ISSUE_URL),
            {"title": "No body", "body": ""},
            source="github-cli",
            captured_at="2026-09-02T00:00:00+00:00",
        )
        self.assertIn("_The issue has no body._", markdown)


class IssueCaptureTests(unittest.TestCase):
    def test_capture_from_github_cli_replaces_the_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            _, run_directory = make_run(root)
            self.assertIn(
                "Issue content has not been captured",
                (run_directory / "issue.md").read_text(encoding="utf-8"),
            )
            stub = write_stub_github_cli(
                Path(temporary_directory),
                json.dumps(
                    {
                        "number": 7,
                        "title": "Crash on empty input",
                        "body": "It crashes.",
                        "state": "OPEN",
                        "url": ISSUE_URL,
                        "author": {"login": "reporter"},
                        "labels": [{"name": "bug"}],
                    }
                ),
            )
            record = capture_issue_from_github(
                run_directory,
                issue_url=ISSUE_URL,
                executable=str(stub),
                timeout_seconds=30,
            )

            self.assertTrue(record["success"], record)
            self.assertEqual(record["title"], "Crash on empty input")
            self.assertEqual(record["labels"], ["bug"])
            issue_markdown = (run_directory / "issue.md").read_text(encoding="utf-8")
            self.assertIn("It crashes.", issue_markdown)
            self.assertNotIn("Issue content has not been captured", issue_markdown)
            self.assertEqual(load_issue_record(run_directory)["source"], "github-cli")

    def test_unreadable_payload_fails_without_touching_the_issue_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            _, run_directory = make_run(root)
            stub = write_stub_github_cli(Path(temporary_directory), "not json")
            record = capture_issue_from_github(
                run_directory,
                issue_url=ISSUE_URL,
                executable=str(stub),
                timeout_seconds=30,
            )

            self.assertFalse(record["success"])
            self.assertIn("unreadable JSON", record["detail"])
            self.assertIn(
                "Issue content has not been captured",
                (run_directory / "issue.md").read_text(encoding="utf-8"),
            )

    def test_capture_from_file_records_the_source_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            _, run_directory = make_run(root)
            source = Path(temporary_directory) / "issue.txt"
            source.write_text("Transcribed by hand.\n", encoding="utf-8")

            record = capture_issue_from_file(
                run_directory,
                issue_url=ISSUE_URL,
                source_file=source,
                title="Crash on empty input",
            )

            self.assertTrue(record["success"])
            self.assertEqual(len(record["source_sha256"]), 64)
            markdown = (run_directory / "issue.md").read_text(encoding="utf-8")
            self.assertIn("Transcribed by hand.", markdown)
            self.assertIn("manual file capture", markdown)

    def test_capture_rejects_an_issue_url_the_run_cannot_address(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            _, run_directory = make_run(root)
            source = Path(temporary_directory) / "issue.txt"
            source.write_text("body", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "GitHub issue URL"):
                capture_issue_from_file(
                    run_directory,
                    issue_url="https://example.com/issues/7",
                    source_file=source,
                )


if __name__ == "__main__":
    unittest.main()
