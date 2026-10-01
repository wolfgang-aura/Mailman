"""Most targets fail the pre-screen, and failing before a run exists is the point.

https://github.com/wolfgang-aura/Mailman/issues/75
"""

import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from mailman.cli import main
from mailman.hunt import create_hunt, hunt_path, save
from mailman.prescreen import (
    ISSUE_RESERVED_FOR_HUMANS,
    DECIDABLE,
    DESIGN_UNDECIDED,
    INVITED_ENHANCEMENT,
    MAINTAINER_DECLINED,
    MAINTAINER_DISPUTED,
    ISSUE_NOT_BOUNDED_FIX,
    ISSUE_NOT_TRIAGED_HERE,
    ISSUE_LACKS_REQUIRED_LABEL,
    PRESCREEN_HOURS,
    REPOSITORY_SCREEN_FAILED,
    TRIVIAL,
    base_branch_refusal,
    identifier_terms,
    TRIVIAL_FIX,
    TRIVIAL_FIX_DIRECT_PUSH,
    REPORTED_FIXED_ON_MAIN,
    UNACKNOWLEDGED_ISSUE,
    UNTRIAGED_ASK_FIRST,
    UNKNOWN,
    check,
    estimate_fix_size,
    is_fresh,
    issue_reference,
    issue_symbols,
    load_prescreen,
    prescreen_directory,
    prescreen_issue,
    prescreen_path,
)
from mailman.screen import screen_path
from mailman.shortlist import (
    MAINTAINER_INVITED,
    NO_LINKED_PR,
    RECENT,
    UNACKNOWLEDGED,
)
from mailman.targeting import (
    ALREADY_FIXED_UPSTREAM,
    CITED_MERGED_BEFORE_ISSUE,
    CITED_MERGED_ELSEWHERE,
    MAINTAINER_REMARK_ELSEWHERE,
    CITED_MERGED_IN_BODY,
    DUPLICATE_FORBIDDEN_OPEN_ATTEMPT,
    MAINTAINER_CLOSED_ATTEMPT,
    MAINTAINER_CLOSED_ATTEMPT_REAFFIRMED,
    MAINTAINER_OWNED_FIX,
    MAINTAINER_PENDING_FIX,
    NO_MAINTAINER_REPLY,
    STALE_PRIOR_ATTEMPT,
    NO_REPRODUCTION,
    NO_TARGET_INTEL,
    OPEN_PULL_REQUEST,
    assess_target,
)


class IssueReferenceTests(unittest.TestCase):
    def test_reads_short_form_and_url(self) -> None:
        self.assertEqual(
            issue_reference("pdm-project/pdm#3877"), ("pdm-project/pdm", 3877)
        )
        self.assertEqual(
            issue_reference("https://github.com/pdm-project/pdm/issues/3877"),
            ("pdm-project/pdm", 3877),
        )

    def test_refuses_a_repository_without_an_issue(self) -> None:
        with self.assertRaises(ValueError):
            issue_reference("pdm-project/pdm")


class PrescreenPathTests(unittest.TestCase):
    def test_a_dotted_repository_keeps_one_verdict_per_issue(self) -> None:
        # plotly.py's issues all wrote plotly__plotly.json. Mailman #282.
        root = Path("data")
        first = prescreen_path(root, "plotly/plotly.py", 5632)
        second = prescreen_path(root, "plotly/plotly.py", 5613)
        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "plotly__plotly.py__5632.json")


class FixSizeTests(unittest.TestCase):
    """What the issue says it wants, read for how long the change would take.

    https://github.com/wolfgang-aura/Mailman/issues/79
    """

    def test_a_documentation_typo_reads_as_trivial(self) -> None:
        estimate, reason = estimate_fix_size("Typo in the README", "", [])
        self.assertEqual(estimate, TRIVIAL)
        self.assertIn("typo", reason)

    def test_documentation_that_contradicts_the_code_reads_as_trivial(self) -> None:
        # pdm-project/pdm#3877, the issue behind the wasted run: one line in
        # docs/reference/pep621.md.
        estimate, reason = estimate_fix_size(
            "pep621 reference is outdated",
            "The documentation says the field is `project.name`, which is wrong.",
            [],
        )
        self.assertEqual(estimate, TRIVIAL)
        self.assertIn("documentation wording", reason)

    def test_a_wrong_error_message_reads_as_trivial(self) -> None:
        estimate, _ = estimate_fix_size(
            "Error message for an empty path is misleading", "", []
        )
        self.assertEqual(estimate, TRIVIAL)

    def test_a_version_pin_bump_reads_as_trivial(self) -> None:
        estimate, _ = estimate_fix_size(
            "Relax the upper bound on the packaging requirement", "", []
        )
        self.assertEqual(estimate, TRIVIAL)

    def test_a_typo_label_is_enough_on_its_own(self) -> None:
        estimate, reason = estimate_fix_size("Something is off", "", ["Typo"])
        self.assertEqual(estimate, TRIVIAL)
        self.assertEqual(reason, "labelled typo")

    def test_an_ordinary_defect_is_left_unknown(self) -> None:
        # The estimate never claims a change is large. It only names the ones
        # it can see are small.
        estimate, reason = estimate_fix_size(
            "Crash on empty input",
            "The parser raises IndexError when the file has no rows.",
            ["bug"],
        )
        self.assertEqual(estimate, UNKNOWN)
        self.assertIn("nothing in the issue", reason)


class IssueSymbolTests(unittest.TestCase):
    def test_reads_backticked_names_dotted_paths_and_files(self) -> None:
        body = (
            "The pipeline calls {b}_handle_upserts{b} and {b}_ahandle_upserts{b} in "
            "llama_index/core/ingestion/pipeline.py; {b}docstore{b} is a word. "
            "See {b}IngestionPipeline.run(){b}, {b}Pipeline._handle_upserts{b} "
            "and {b}x{b}, then {b}a_b{b}."
        ).format(b="`")
        # A dotted path reduces to its last segment, so the same function named
        # two ways is one symbol, and a plain word like `run` is not one.
        self.assertEqual(
            issue_symbols(body),
            ["_handle_upserts", "_ahandle_upserts", "a_b", "pipeline.py"],
        )

    def test_the_count_is_capped_because_each_symbol_is_a_search_call(self) -> None:
        body = " ".join(f"`name_{index}`" for index in range(20))
        self.assertEqual(len(issue_symbols(body)), 4)

    def test_prose_between_two_short_tokens_is_not_a_name(self) -> None:
        self.assertEqual(issue_symbols("`x` and `y` then `real_one`"), ["real_one"])


#: The stub `gh`, as a Python program rather than a shell script: it has to
#: route by sub-command and by API path, and one of those paths carries an `&`.
_GH_FIXTURE_SERVER = '''\
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARGUMENTS = sys.argv[1:]


def emit(name):
    path = HERE / name
    if not path.is_file():
        sys.stderr.write("no fixture: " + name + "\\n")
        raise SystemExit(1)
    sys.stdout.write(path.read_text(encoding="utf-8"))
    raise SystemExit(0)


def emit_first(*names):
    """The first fixture that exists, so a per-number one can override."""
    for name in names:
        if (HERE / name).is_file():
            emit(name)
    emit(names[-1])


if ARGUMENTS[:2] == ["issue", "view"]:
    emit_first("issue-view-" + ARGUMENTS[2] + ".json", "issue-payload.json")
if ARGUMENTS[:2] == ["search", "prs"] and "--merged" in ARGUMENTS:
    if (HERE / "merged-prs.json").is_file():
        emit("merged-prs.json")
    sys.stdout.write("[]")
    raise SystemExit(0)
if ARGUMENTS[:2] == ["pr", "view"]:
    slug = ARGUMENTS[ARGUMENTS.index("--repo") + 1] if "--repo" in ARGUMENTS else ""
    name = "pr-" + slug.replace("/", "__") + "-" + ARGUMENTS[2] + ".json"
    if (HERE / "pr-view-fails.txt").is_file():
        sys.stderr.write((HERE / "pr-view-fails.txt").read_text(encoding="utf-8"))
        raise SystemExit(1)
    if not (HERE / name).is_file():
        # What GitHub answers for an issue number or a heading anchor.
        sys.stderr.write(
            "GraphQL: Could not resolve to a PullRequest with the number of "
            + ARGUMENTS[2] + ". (repository.pullRequest)\\n"
        )
        raise SystemExit(1)
    emit(name)
if ARGUMENTS[:1] == ["api"]:
    path = ARGUMENTS[1]
    if path == "graphql":
        emit("mergers.json")
    if "/compare/" in path:
        emit("compare.json")
    if "/comments" in path:
        emit("comments.json")
    if "/timeline" in path:
        number = path.split("/issues/")[1].split("/")[0] if "/issues/" in path else ""
        emit_first("timeline-" + number + ".json", "timeline.json")
    if "/pulls/" in path:
        emit("pr-association-" + path.rsplit("/", 1)[-1] + ".json")
    emit("issue-api.json")
if (HERE / "corpus-pr.json").is_file():
    # A repository with real open pull requests, searched the way GitHub does:
    # every query word must appear, so a long title finds nothing.
    import json

    corpus = json.loads((HERE / "corpus-pr.json").read_text(encoding="utf-8"))
    if ARGUMENTS[:2] == ["pr", "list"] or ARGUMENTS[:2] == ["search", "prs"]:
        if ARGUMENTS[0] == "search":
            words = []
            for argument in ARGUMENTS[2:]:
                if argument.startswith("--"):
                    break
                words.append(argument)
        elif "--search" in ARGUMENTS:
            words = ARGUMENTS[ARGUMENTS.index("--search") + 1].split()
        else:
            limit = int(ARGUMENTS[ARGUMENTS.index("--limit") + 1])
            sys.stdout.write(json.dumps(corpus[:limit]))
            raise SystemExit(0)
        found = [
            entry
            for entry in corpus
            if all(
                word.lower()
                in (entry["title"] + " " + entry.get("body", "")).lower()
                for word in words
            )
        ]
        sys.stdout.write(json.dumps(found))
        raise SystemExit(0)
    sys.stdout.write("[]")
    raise SystemExit(0)
emit("payload.json")
'''


class PrescreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "runs"
        self.root.mkdir(parents=True)

    def stub(
        self,
        payload: str,
        issue_payload: dict | None = None,
        *,
        comments: list[dict] | None = None,
        timeline: list[dict] | None = None,
        timelines: dict[int, list[dict]] | None = None,
        pull_requests: dict[str, dict] | None = None,
        issue_api: dict | None = None,
        open_pull_requests: list[dict] | None = None,
        merged_pull_requests: list[dict] | None = None,
        issues: dict[int, dict] | None = None,
        compare: dict | None = None,
    ) -> str:
        """A `gh` that answers from fixture files, one per question asked.

        `pull_requests` is keyed `OWNER/REPO#N`. A number with no fixture is a
        pull request `gh` cannot find, which is how an issue number and a
        heading anchor arrive here.
        """
        directory = Path(self.temporary.name) / "bin"
        directory.mkdir(exist_ok=True)
        issue = issue_payload or {
            "number": 7,
            "title": "Crash on empty input",
            "body": "The command crashes on empty input.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        fixtures = {
            "payload.json": payload,
            "issue-payload.json": json.dumps(issue),
            "comments.json": json.dumps(comments or []),
            "timeline.json": json.dumps(timeline or []),
            "issue-api.json": json.dumps(
                {
                    "number": issue["number"],
                    "assignees": [],
                    "user": {"login": "reporter", "type": "User"},
                    "author_association": "NONE",
                    "body": issue.get("body", ""),
                    "created_at": "2026-09-01T00:00:00Z",
                    "html_url": issue.get("url"),
                    "state": "open",
                    "closed_at": None,
                    **(issue_api or {}),
                }
            ),
        }
        # One timeline per pull request, because who closed an attempt is read
        # from the attempt's own timeline and not from the issue's.
        # With open pull requests, searches and listings answer from them as
        # GitHub would, instead of every search returning `payload`.
        if open_pull_requests is not None:
            fixtures["corpus-pr.json"] = json.dumps(open_pull_requests)
        if compare is not None:
            fixtures["compare.json"] = json.dumps(compare)
        # `gh search prs --merged` answers from these, and nothing else does.
        if merged_pull_requests is not None:
            fixtures["merged-prs.json"] = json.dumps(merged_pull_requests)
        # `gh issue view N` for an issue the thread cites, not the one screened.
        for number, cited_issue in (issues or {}).items():
            fixtures[f"issue-view-{number}.json"] = json.dumps(cited_issue)
        for number, events in (timelines or {}).items():
            fixtures[f"timeline-{number}.json"] = json.dumps(events)
        for reference, pull in (pull_requests or {}).items():
            slug, _, number = reference.rpartition("#")
            fixtures[f"pr-{slug.replace('/', '__')}-{number}.json"] = json.dumps(pull)
            # `gh pr view` carries no author association, so a dormant attempt
            # costs one `gh api repos/.../pulls/N` call. Answer it from the
            # same fixture.
            fixtures[f"pr-association-{number}.json"] = json.dumps(
                pull.get("authorAssociation", "CONTRIBUTOR")
            )
        for name, text in fixtures.items():
            (directory / name).write_text(text, encoding="utf-8")
        (directory / "gh.py").write_text(_GH_FIXTURE_SERVER, encoding="utf-8")
        if sys.platform == "win32":
            stub = directory / "gh.cmd"
            stub.write_text(
                f'@echo off\r\n"{sys.executable}" "%~dp0gh.py" %*\r\n',
                encoding="utf-8",
            )
            return str(stub)
        stub = directory / "gh.sh"
        stub.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/gh.py" "$@"\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(stub)

    def test_a_clean_issue_passes_and_records_where_it_looked(self) -> None:
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertTrue(record["duplicate_search"]["success"])
        self.assertIn("init-run", record["next"])
        self.assertEqual(load_prescreen(self.root, "example/project", 7), record)
        self.assertEqual(record["issue"]["title"], "Crash on empty input")
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        self.assertEqual(search["query"], "Crash on empty input")

    def test_cli_binds_pre_run_screening_to_the_single_live_hunt(self) -> None:
        hunt = create_hunt(
            self.root,
            1,
            primary="codex",
            primary_model="fixture-primary",
            reviewer="claude",
            reviewer_model="fixture-reviewer",
        )
        hunt["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        save(hunt_path(self.root, hunt["hunt_id"]), hunt)
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            code = main(
                [
                    "prescreen",
                    "example/project#7",
                    "--executable",
                    self.stub("[]"),
                    "--data-root",
                    str(self.root),
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("deadline expired", stderr.getvalue())

    def test_a_feature_request_is_rejected_before_a_run_exists(self) -> None:
        issue = {
            "number": 7,
            "title": "Choose and add a new transport",
            "body": "Several routing designs are possible.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [{"name": "feature"}],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn("issue-not-bounded-fix", record["blocking"])
        self.assertNotIn("duplicate_search", record)
        # The thread is read, because an invitation there would lift the
        # block; the search is still skipped. Mailman #155.
        self.assertEqual(record["stages_skipped"], ["duplicate-search", "prior-art"])
        self.assertIn("no maintainer in the thread asked", record["next"])

    def labelled(self, label: str) -> dict:
        return {
            "number": 7,
            "title": "Support reading gzip files",
            "body": "It would help to read gzip-compressed input.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [{"name": label}],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }

    invitation = {
        "body": "Sounds reasonable, a PR would be welcome.",
        "author_association": "MEMBER",
        "created_at": "2026-09-02T00:00:00Z",
        "user": {"login": "maintainer", "type": "User"},
    }

    def test_a_maintainer_invited_enhancement_passes_with_a_warning(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]", self.labelled("enhancement"), comments=[self.invitation]
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertIn(INVITED_ENHANCEMENT, record["warnings"])
        self.assertEqual(record["claims"]["invitations"], 1)

    def test_a_help_wanted_label_invites_an_enhancement(self) -> None:
        issue = self.labelled("enhancement")
        issue["labels"].append({"name": "help wanted"})
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]", issue)
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(INVITED_ENHANCEMENT, record["warnings"])
        self.assertEqual(record["claims"]["invitations"], 0)

    def test_an_invitation_from_outside_does_not_lift_the_block(self) -> None:
        outsider = {**self.invitation, "author_association": "NONE"}
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.labelled("feature"), comments=[outsider]),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [ISSUE_NOT_BOUNDED_FIX])

    def test_an_invitation_does_not_lift_a_tracking_label(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]", self.labelled("tracking"), comments=[self.invitation]
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(ISSUE_NOT_BOUNDED_FIX, record["blocking"])
        self.assertIn("claims", record["stages_skipped"])

    def test_an_issue_under_discussion_is_rejected_before_any_search(self) -> None:
        # py-pdf/pypdf#4035 carried `needs-discussion`; a run was built,
        # reviewed and filed on it, and the maintainer asked why a PR was
        # opened on an undecided design. Mailman #124.
        issue = {
            "number": 7,
            "title": "Two-byte ToUnicode CMap on a simple font",
            "body": "PDFBox ignores it; poppler pads.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [{"name": "workflow-text-extraction"}, {"name": "needs-discussion"}],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]", issue)
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn("issue-under-discussion", record["blocking"])
        self.assertNotIn("duplicate_search", record)

    def test_a_label_described_as_awaiting_input_is_under_discussion(self) -> None:
        # zauberzeug/nicegui#6331 carried `analysis`, which nicegui describes
        # as "Status: Requires team/community input". The name matched
        # nothing, so it passed and reached filing with "PR or offer?" still
        # open. Mailman #280.
        issue = {
            "number": 7,
            "title": "Shutdown handler runs outside the client context",
            "body": "It raises on shutdown.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [
                {"name": "bug", "description": "Type/scope: Incorrect behavior"},
                {"name": "analysis", "description": "Status: Requires team/community input"},
            ],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]", issue)
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn("issue-under-discussion", record["blocking"])

    def test_a_plain_bug_label_description_does_not_block(self) -> None:
        issue = {
            "number": 7,
            "title": "Crash",
            "body": "It raises.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [{"name": "bug", "description": "Something isn't working"}],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]", issue)
        )

        self.assertNotIn("issue-under-discussion", record.get("blocking", []))

    def record_direct_push_share(self, share: float, verdict: str = "pass") -> None:
        """Write the screen record the pre-screen reads the habit out of."""
        path = screen_path(self.root, "example/project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "repository": "example/project",
                    "success": True,
                    "verdict": verdict,
                    "gates": [
                        {
                            "name": "direct-push",
                            "passed": True,
                            "blocking": False,
                            "detail": "",
                            "data": {"direct_push_share": share},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def typo_issue(self) -> dict:
        return {
            "number": 7,
            "title": "Typo in the installation docs",
            "body": "`pip instal` should read `pip install`.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }

    def test_a_trivial_fix_where_the_maintainer_pushes_directly_is_rejected(
        self,
    ) -> None:
        self.record_direct_push_share(0.8)
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(TRIVIAL_FIX_DIRECT_PUSH, record["blocking"])
        self.assertEqual(record["fix_size"]["estimate"], TRIVIAL)
        self.assertEqual(record["fix_size"]["direct_push_share"], 0.8)
        self.assertIn("before he reviews it", record["fix_size"]["detail"])
        self.assertIn("before he reviews it", record["next"])
        self.assertNotIn("duplicate_search", record)
        self.assertEqual(
            record["stages_skipped"], ["duplicate-search", "prior-art", "claims"]
        )

    def test_the_cli_exits_non_zero_on_a_trivial_fix_reject(self) -> None:
        self.record_direct_push_share(0.8)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(
                [
                    "prescreen",
                    "example/project#7",
                    "--executable",
                    self.stub("[]", self.typo_issue()),
                    "--data-root",
                    str(self.root),
                ]
            )

        self.assertEqual(code, 1)

    def test_an_issue_in_a_repository_whose_screen_failed_is_rejected(self) -> None:
        # alembic#1390 and podman-compose#1549 passed here while their
        # repositories' screens had failed. Mailman #152.
        self.record_direct_push_share(0.05, verdict="fail")
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(REPOSITORY_SCREEN_FAILED, record["blocking"])
        self.assertIn("screen-target example/project --refresh", record["next"])
        self.assertNotIn("duplicate_search", record)

    def test_a_trivial_fix_in_a_reviewed_repository_is_only_a_warning(self) -> None:
        self.record_direct_push_share(0.05)
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertIn(TRIVIAL_FIX, record["warnings"])
        self.assertEqual(record["fix_size"]["estimate"], TRIVIAL)
        self.assertIn("reads as trivial", record["fix_size"]["detail"])

    def test_an_unscreened_repository_leaves_the_habit_unknown(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(TRIVIAL_FIX, record["warnings"])
        self.assertIsNone(record["fix_size"]["direct_push_share"])
        self.assertIn("unrecorded", record["fix_size"]["detail"])

    def test_an_ordinary_defect_records_an_unknown_fix_size(self) -> None:
        self.record_direct_push_share(0.9)
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn(TRIVIAL_FIX, record["warnings"])
        self.assertEqual(record["fix_size"]["estimate"], UNKNOWN)
        self.assertEqual(record["fix_size"]["direct_push_share"], 0.9)

    def _aged(self, days: int) -> dict:
        return {
            "created_at": (datetime.now(UTC) - timedelta(days=days)).isoformat()
        }

    def test_an_old_outside_report_nobody_answered_warns_before_a_run(self) -> None:
        # urllib3#5053: 99 days old, reported from outside, no maintainer
        # reply. The untriaged question fired on the decision page, after the
        # run was spent. Mailman #116.
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue_api=self._aged(99)),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(UNACKNOWLEDGED_ISSUE, record["warnings"])
        self.assertTrue(record["acknowledgement"]["unacknowledged"])
        self.assertIn("no owner, member or collaborator", record["next"])
        self.assertLess(record["ranking"]["score"], 0)

    def test_a_comment_saying_main_is_fixed_warns_before_a_run(self) -> None:
        # pylint#10032 passed with "This no longer reproduces on current
        # `main`" as its last comment. Mailman #236.
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    {
                        "body": "This no longer reproduces on current `main`.",
                        "author_association": "NONE",
                        "created_at": "2026-09-09T00:00:00Z",
                        "user": {"login": "passerby", "type": "User"},
                    }
                ],
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(REPORTED_FIXED_ON_MAIN, record["warnings"])
        self.assertIn("no longer reproduces", record["next"])

    def test_a_maintainer_reply_clears_the_unacknowledged_warning(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                issue_api=self._aged(99),
                comments=[
                    {
                        "body": "Confirmed, thanks.",
                        "author_association": "MEMBER",
                        "created_at": "2026-09-02T00:00:00Z",
                        "user": {"login": "maintainer", "type": "User"},
                    }
                ],
            ),
        )

        self.assertNotIn(UNACKNOWLEDGED_ISSUE, record["warnings"])
        self.assertFalse(record["acknowledgement"]["unacknowledged"])
        self.assertGreaterEqual(record["ranking"]["score"], 0)

    def test_a_report_inside_the_grace_window_is_not_yet_unacknowledged(
        self,
    ) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue_api=self._aged(5)),
        )

        self.assertNotIn(UNACKNOWLEDGED_ISSUE, record["warnings"])

    def test_a_fresh_untriaged_report_warns_that_the_run_only_asks(self) -> None:
        # agentscope#3059 passed prescreen at five days old with no maintainer
        # word, then its run decided ASK and never counted toward the quota.
        # Mailman #287.
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue_api=self._aged(5)),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(UNTRIAGED_ASK_FIRST, record["warnings"])
        self.assertNotIn(UNACKNOWLEDGED_ISSUE, record["warnings"])
        self.assertTrue(record["acknowledgement"]["untriaged"])
        self.assertIn("ready_to_ask", record["next"])

    def test_a_maintainer_reply_clears_the_untriaged_warning(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                issue_api=self._aged(5),
                comments=[
                    {
                        "body": "Confirmed, thanks.",
                        "author_association": "MEMBER",
                        "created_at": "2026-09-28T00:00:00Z",
                        "user": {"login": "maintainer", "type": "User"},
                    }
                ],
            ),
        )

        self.assertNotIn(UNTRIAGED_ASK_FIRST, record["warnings"])
        self.assertFalse(record["acknowledgement"]["untriaged"])

    def test_each_symbol_in_the_issue_body_gets_its_own_narrow_search(self) -> None:
        # llama_index#22639: the body named the functions, two open rivals
        # carried them, neither mentioned the issue number, and the typed
        # symbols were prose words. One joined query of every term found
        # nothing, because GitHub ANDs the terms; one query per symbol found
        # both rivals. Mailman #96.
        issue = {
            "number": 7,
            "title": "Docstore delete fails on failed runs",
            "body": "`_handle_upserts` and `_ahandle_upserts` skip the delete.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            symbols=["docstore"],
            executable=self.stub("[]", issue),
        )
        self.assertEqual(record["symbols"], ["docstore"])
        self.assertEqual(
            record["issue_symbols"], ["_handle_upserts", "_ahandle_upserts"]
        )
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        self.assertEqual(search["symbols"], ["docstore"])
        self.assertEqual(
            search["issue_symbols"], ["_handle_upserts", "_ahandle_upserts"]
        )
        queries = {}
        for command in search["commands"]:
            if command["method"] != "narrow":
                continue
            argv = command["command"]
            queries.setdefault(argv[1], []).append(argv[argv.index("--search") + 1])
        self.assertEqual(
            queries["pr"],
            ["#7 docstore", "#7 _handle_upserts", "#7 _ahandle_upserts"],
        )
        self.assertEqual(queries["issue"], ["#7 docstore"])

    def test_a_narrow_hit_on_a_bare_number_does_not_reference_the_issue(self) -> None:
        # pretix#6327: GitHub's search tokenised `#6327` to `6327` and returned
        # a 2018 pull request whose comment log said `django.po:6327:`. The
        # narrow branch took every hit as a citation, check-target read it as
        # a maintainer-closed attempt and refused the run.
        payload = json.dumps(
            [
                {
                    "number": 924,
                    "title": "follow gettext convention on language tags",
                    "body": "Language tags should follow the gettext convention.",
                    "state": "CLOSED",
                    "url": "https://github.com/example/project/pull/924",
                    "createdAt": "2018-05-27T12:24:44Z",
                    "updatedAt": "2018-11-26T08:58:10Z",
                    "isDraft": False,
                },
                {
                    "number": 6328,
                    "title": "Fix mutable default argument",
                    "body": "Fixes #7",
                    "state": "CLOSED",
                    "url": "https://github.com/example/project/pull/6328",
                    "createdAt": "2026-07-01T11:55:00Z",
                    "updatedAt": "2026-07-01T11:56:50Z",
                    "isDraft": False,
                },
            ]
        )
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub(payload)
        )
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        narrow = {
            row["number"]: row["references_issue"]
            for row in search["matches"]
            if "narrow" in row.get("methods", [])
        }
        self.assertEqual(narrow.get(6328), True)
        self.assertEqual(narrow.get(924), False)
        # And the unrelated one is not a prior attempt for check-target to
        # refuse over.
        self.assertEqual(
            [row["number"] for row in record["stale_attempts"]], [6328]
        )

    def test_the_verdict_lands_beside_the_repository_screens(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "example__project__7.json")

    def test_no_run_directory_is_created(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        runs = [entry for entry in self.root.iterdir() if entry.name != "issue-screens"]
        self.assertEqual(runs, [])

    def test_an_open_rival_rejects_the_issue_before_a_run_exists(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        search["matches"] = [
            {
                "number": 99,
                "title": "Fix the thing",
                "state": "open",
                "pull_request": True,
                "references_issue": True,
                "matched_by": ["#7"],
            }
        ]
        (directory / "duplicate-search.json").write_text(
            json.dumps(search), encoding="utf-8"
        )
        assessment = assess_target(directory)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertIn(OPEN_PULL_REQUEST, DECIDABLE)

    def test_title_search_rejects_an_open_semantic_rival_before_setup(self) -> None:
        rival = [
            {
                "number": 99,
                "title": "Fix crash on empty input",
                "state": "open",
                "url": "https://github.com/example/project/pull/99",
                "createdAt": "2026-09-02T00:00:00Z",
                "body": "Handle the empty-input crash.",
                "headRefName": "fix-empty-input",
            }
        ]

        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub(json.dumps(rival))
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(OPEN_PULL_REQUEST, record["blocking"])
        self.assertEqual(record["open_attempts"], [99])

    def test_a_verdict_never_rests_on_something_this_stage_cannot_know(self) -> None:
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )
        # There is no clone here, so `assess_target` always wants a
        # reproduction and target intel. Neither is this stage's question.
        directory = prescreen_directory(self.root, "example/project", 7)
        self.assertIn(NO_REPRODUCTION, assess_target(directory).blocking)
        self.assertNotIn(NO_REPRODUCTION, record["blocking"])
        self.assertNotIn(NO_TARGET_INTEL, record["blocking"])

    def test_check_refuses_an_issue_with_no_pre_screen(self) -> None:
        record, refusal = check(self.root, "example/project#7")
        self.assertIsNone(record)
        self.assertIn("no pre-screen", refusal)

    def test_check_refuses_a_rejected_issue(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.update(verdict="reject", blocking=["open-pull-request"])
        path.write_text(json.dumps(stored), encoding="utf-8")
        _, refusal = check(self.root, "example/project#7")
        self.assertIn("open-pull-request", refusal)

    def test_check_refuses_a_stale_pre_screen(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["screened_at"] = (
            datetime.now(UTC) - timedelta(hours=PRESCREEN_HOURS + 1)
        ).isoformat()
        path.write_text(json.dumps(stored), encoding="utf-8")
        self.assertFalse(is_fresh(stored))
        _, refusal = check(self.root, "example/project#7")
        self.assertIn("older than", refusal)

    def test_check_refuses_a_superseded_schema(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["schema_version"] = 1
        path.write_text(json.dumps(stored), encoding="utf-8")

        _, refusal = check(self.root, "example/project#7")

        self.assertIn("predates a screening question", refusal)

    def test_check_clears_a_fresh_pass(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        record, refusal = check(self.root, "example/project#7")
        self.assertIsNone(refusal)
        self.assertEqual(record["verdict"], "pass")


class CompactQueryTests(unittest.TestCase):
    """A long title ANDs every word, so the rival that matters goes unseen.

    marimo#9974 passed pre-screen while open pull request #10915 fixed it: the
    full-title search found nothing, and the listing matched 7 of its 11 words.
    https://github.com/wolfgang-aura/Mailman/issues/201
    """

    # Borrowed rather than inherited, so PrescreenTests' own tests run once.
    setUp = PrescreenTests.setUp
    stub = PrescreenTests.stub

    TITLE = (
        "`mo.state` setter doesn't update the getters if the notebook is "
        "embedded and the setter is called from an anywidget"
    )
    RIVAL = {
        "number": 10915,
        "title": "fix: mark owner cells stale when state setter crosses embedded app",
        "state": "open",
        "url": "https://github.com/marimo-team/marimo/pull/10915",
        "createdAt": "2026-09-20T00:00:00Z",
        "body": (
            "When a `mo.state` setter runs inside an embedded notebook, the "
            "getter cells in the owner app were never marked stale."
        ),
        "headRefName": "fix-embedded-state",
        "author": {"login": "rival"},
    }

    def issue(self) -> dict:
        return {
            "number": 9974,
            "title": self.TITLE,
            "body": "Calling the setter from an anywidget leaves getters stale.",
            "state": "OPEN",
            "url": "https://github.com/marimo-team/marimo/issues/9974",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }

    @staticmethod
    def unrelated(count: int, start: int = 11000) -> list[dict]:
        """Open pull requests that use the title's common words, not its rare ones."""
        return [
            {
                "number": start + index,
                "title": f"Improve embedded notebook anywidget layout {index}",
                "state": "open",
                "url": f"https://github.com/marimo-team/marimo/pull/{start + index}",
                "createdAt": "2026-09-10T00:00:00Z",
                "body": "The notebook renders the anywidget in an embedded frame.",
                "headRefName": f"layout-{index}",
                "author": {"login": "someone"},
            }
            for index in range(count)
        ]

    def test_a_rival_matching_only_the_rare_title_words_rejects(self) -> None:
        corpus = [*self.unrelated(12), self.RIVAL]
        record = prescreen_issue(
            self.root,
            "marimo-team/marimo#9974",
            executable=self.stub(
                "[]", self.issue(), open_pull_requests=corpus
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(OPEN_PULL_REQUEST, record["blocking"])
        self.assertEqual(record["open_attempts"], [10915])
        self.assertEqual(
            record["duplicate_search"]["compact_terms"],
            ["mo.state", "setter", "getter"],
        )

    def test_a_rival_past_the_listing_limit_costs_one_search(self) -> None:
        corpus = [*self.unrelated(100), self.RIVAL]
        record = prescreen_issue(
            self.root,
            "marimo-team/marimo#9974",
            executable=self.stub(
                "[]", self.issue(), open_pull_requests=corpus
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["open_attempts"], [10915])
        directory = prescreen_directory(self.root, "marimo-team/marimo", 9974)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        compact = [
            command for command in search["commands"] if command["method"] == "compact"
        ]
        self.assertEqual(len(compact), 1)

    def test_common_title_words_alone_do_not_block(self) -> None:
        record = prescreen_issue(
            self.root,
            "marimo-team/marimo#9974",
            executable=self.stub(
                "[]", self.issue(), open_pull_requests=self.unrelated(12)
            ),
        )

        self.assertEqual(record["open_attempts"], [])
        self.assertNotIn(OPEN_PULL_REQUEST, record["blocking"])


class CitedPullRequestTests(PrescreenTests):
    """The fix is named in the issue's own thread, and the search never saw it.

    Three real screens from hunt 20260916T165859Z-0d3481, one per way a thread
    names a pull request. All three passed the pre-screen and all three died
    when a person read the thread.
    https://github.com/wolfgang-aura/Mailman/issues/98
    """

    def issue(self, number: int, slug: str, body: str, title: str) -> dict:
        return {
            "number": number,
            "title": title,
            "body": body,
            "state": "OPEN",
            "url": f"https://github.com/{slug}/issues/{number}",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }

    def test_a_draft_implementation_linked_in_the_body_rejects_the_issue(self) -> None:
        # deepset-ai/haystack#12777 ends with "**Draft implementation:**
        # [#12775](https://github.com/deepset-ai/haystack/pull/12775)". The
        # broad search ranked #12775 among 88 matches and the narrow one never
        # found it, because #12775 does not cite the issue back.
        issue = self.issue(
            12777,
            "deepset-ai/haystack",
            "Run-time `generation_kwargs['tools']` overwrites the Agent's own "
            "tools.\n\n**Draft implementation:** "
            "[#12775](https://github.com/deepset-ai/haystack/pull/12775)",
            "Agent drops its own tools when generation_kwargs carries tools",
        )
        record = prescreen_issue(
            self.root,
            "deepset-ai/haystack#12777",
            executable=self.stub(
                "[]",
                issue,
                pull_requests={
                    "deepset-ai/haystack#12775": {
                        "number": 12775,
                        "state": "OPEN",
                        "title": "fix: merge run-time tools with the agent's own",
                        "url": "https://github.com/deepset-ai/haystack/pull/12775",
                        "mergedAt": None,
                        "mergeCommit": None,
                    }
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [OPEN_PULL_REQUEST])
        decided = record["cited_pull_requests"]["decided_by"]
        self.assertEqual(decided["number"], 12775)
        self.assertEqual(decided["repository"], "deepset-ai/haystack")
        self.assertEqual(decided["state"], "OPEN")
        self.assertIn("12775", decided["reference"])
        # Nothing was searched. The thread had already answered the question.
        self.assertNotIn("duplicate_search", record)
        self.assertEqual(record["stages_skipped"], ["duplicate-search", "prior-art"])
        self.assertIn("12775", record["next"])

    def test_a_merged_fix_that_never_cites_the_issue_rejects_it(self) -> None:
        # aws/sagemaker-python-sdk#5495. #6294 fixed the stale `hmac_key`
        # call two weeks before the run and never named the issue; the title
        # search ANDed every word and found nothing. Mailman #257.
        title = (
            "TypeError: StoredFunction.__init__() got an unexpected keyword "
            "argument 'hmac_key'"
        )
        self.assertEqual(identifier_terms(title), ["storedfunction", "hmac_key"])
        record = prescreen_issue(
            self.root,
            "aws/sagemaker-python-sdk#5495",
            executable=self.stub(
                "[]",
                self.issue(
                    5495,
                    "aws/sagemaker-python-sdk",
                    "Calling a remote function fails with the traceback below.",
                    title,
                ),
                merged_pull_requests=[
                    {
                        "number": 6294,
                        "title": "fix: remote function serialization",
                        "body": (
                            "StoredFunction no longer takes hmac_key; drop it "
                            "from the caller."
                        ),
                        "url": "https://github.com/aws/sagemaker-python-sdk/pull/6294",
                        "closedAt": "2026-09-23T00:00:00Z",
                    },
                    {
                        "number": 6100,
                        "title": "docs: StoredFunction",
                        "body": "Explains the stored function layout.",
                        "url": "https://github.com/aws/sagemaker-python-sdk/pull/6100",
                        "closedAt": "2026-09-10T00:00:00Z",
                    },
                ],
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [ALREADY_FIXED_UPSTREAM])
        self.assertEqual(
            [row["number"] for row in record["uncited_merged_fixes"]["matches"]],
            [6294],
        )
        self.assertIn("#6294", record["next"])

    def test_one_identifier_in_the_title_is_too_thin_to_search(self) -> None:
        self.assertEqual(identifier_terms("Crash in read_csv on empty input"), ["read_csv"])
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(7, "example/project", "It crashes.", "Crash in read_csv"),
                merged_pull_requests=[
                    {
                        "number": 9,
                        "title": "read_csv tweak",
                        "body": "",
                        "url": "https://github.com/example/project/pull/9",
                        "closedAt": "2026-09-05T00:00:00Z",
                    }
                ],
            ),
        )
        self.assertFalse(record["uncited_merged_fixes"]["searched"])
        self.assertNotIn(ALREADY_FIXED_UPSTREAM, record.get("blocking") or [])

    def test_an_issue_the_body_defers_to_closed_as_done_rejects_it(self) -> None:
        # huggingface/lerobot#2302 says "see #2283"; #2283 was closed
        # COMPLETED after its fix merged. The resolver skipped it because it
        # is an issue, not a pull request. Mailman #257.
        def cited(number: int, reason: str, closed: str | None) -> dict:
            return {
                "number": number,
                "state": "CLOSED" if closed else "OPEN",
                "stateReason": reason,
                "closedAt": closed,
                "title": f"issue {number}",
                "url": f"https://github.com/huggingface/lerobot/issues/{number}",
            }

        record = prescreen_issue(
            self.root,
            "huggingface/lerobot#2302",
            executable=self.stub(
                "[]",
                self.issue(
                    2302,
                    "huggingface/lerobot",
                    "Same failure as #2283, and unlike #2200 it happens on CPU.",
                    "Policy crashes on reset",
                ),
                issues={
                    2283: cited(2283, "COMPLETED", "2026-09-05T00:00:00Z"),
                    2200: cited(2200, "NOT_PLANNED", "2026-09-06T00:00:00Z"),
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [ALREADY_FIXED_UPSTREAM])
        self.assertEqual(
            [row["number"] for row in record["completed_cited_issues"]], [2283]
        )
        self.assertIn("#2283", record["next"])

    def test_an_issue_closed_before_this_one_opened_is_context(self) -> None:
        record = prescreen_issue(
            self.root,
            "huggingface/lerobot#2302",
            executable=self.stub(
                "[]",
                self.issue(
                    2302, "huggingface/lerobot", "Regressed since #2283.", "Crash"
                ),
                issues={
                    2283: {
                        "number": 2283,
                        "state": "CLOSED",
                        "stateReason": "COMPLETED",
                        "closedAt": "2026-06-01T00:00:00Z",
                        "title": "old",
                        "url": "https://github.com/huggingface/lerobot/issues/2283",
                    }
                },
            ),
        )
        self.assertEqual(record["completed_cited_issues"], [])
        self.assertNotIn(ALREADY_FIXED_UPSTREAM, record.get("blocking") or [])

    def test_a_merged_pull_request_named_in_a_comment_rejects_the_issue(self) -> None:
        # PrefectHQ/prefect#22956: both comments say the fix is on main as
        # #22722, which closed #22721 and so never linked to this issue.
        comment = {
            "body": (
                "This is already fixed on `main` - the fix just hasn't been "
                "released yet. PR #22722 (closing #22721) was merged on "
                "2026-08-05 in `fe0aa235f2c5a2fe6387e412aea67e57738f0ae3`."
            ),
            "author_association": "NONE",
            "created_at": "2026-09-10T00:00:00Z",
            "user": {"login": "someone", "type": "User"},
        }
        record = prescreen_issue(
            self.root,
            "PrefectHQ/prefect#22956",
            executable=self.stub(
                "[]",
                self.issue(
                    22956,
                    "PrefectHQ/prefect",
                    "`prefect-redis` reconnects forever with `no such key`.",
                    "Redis consumer retries a missing stream forever",
                ),
                comments=[comment],
                pull_requests={
                    "PrefectHQ/prefect#22722": {
                        "number": 22722,
                        "state": "MERGED",
                        "title": (
                            "Don't treat a missing Redis stream as a connection "
                            "error when trimming"
                        ),
                        "url": "https://github.com/PrefectHQ/prefect/pull/22722",
                        "mergedAt": "2026-08-05T00:00:00Z",
                        "mergeCommit": {
                            "oid": "fe0aa235f2c5a2fe6387e412aea67e57738f0ae3"
                        },
                    }
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [ALREADY_FIXED_UPSTREAM])
        decided = record["cited_pull_requests"]["decided_by"]
        self.assertEqual(decided["number"], 22722)
        self.assertEqual(decided["state"], "MERGED")
        self.assertEqual(
            decided["merge_commit"], "fe0aa235f2c5a2fe6387e412aea67e57738f0ae3"
        )
        # #22721 is an issue. `gh` says so by failing, and that is the whole
        # answer: it is recorded as skipped and decides nothing.
        self.assertEqual(
            [row["number"] for row in record["cited_pull_requests"]["skipped"]],
            [22721],
        )
        self.assertIn("no clone", record["cited_pull_requests"]["detail"])

    def test_a_merged_pull_request_in_another_repository_is_not_a_fix(
        self,
    ) -> None:
        # pydata/xarray#10269 and #10247, 2026-09-29: a downstream project's
        # merged workaround, linked in a comment, refused both as already
        # fixed upstream. A merge elsewhere changes nothing in this tree.
        comment = {
            "body": (
                "We worked around this in "
                "https://github.com/TGSAI/mdio-python/pull/630 for now."
            ),
            "author_association": "NONE",
            "created_at": "2026-09-10T00:00:00Z",
            "user": {"login": "someone", "type": "User"},
        }
        record = prescreen_issue(
            self.root,
            "pydata/xarray#10269",
            executable=self.stub(
                "[]",
                self.issue(
                    10269,
                    "pydata/xarray",
                    "`use_zarr_fill_value_as_mask=True` is ignored in `open_zarr`.",
                    "use_zarr_fill_value_as_mask=True is ignored in open_zarr",
                ),
                comments=[comment],
                pull_requests={
                    "TGSAI/mdio-python#630": {
                        "number": 630,
                        "state": "MERGED",
                        "title": "Work around xarray fill value masking",
                        "url": "https://github.com/TGSAI/mdio-python/pull/630",
                        "mergedAt": "2026-08-05T00:00:00Z",
                        "mergeCommit": {"oid": "b" * 40},
                    }
                },
            ),
        )

        self.assertNotIn(ALREADY_FIXED_UPSTREAM, record["blocking"])
        self.assertEqual(record["verdict"], "pass")

    def test_a_merged_pull_request_named_in_the_body_is_context_not_a_fix(
        self,
    ) -> None:
        # zauberzeug/nicegui#6339: a maintainer opened the issue with "Found
        # while reviewing #6294, where it is out of scope", and the stage
        # refused it as already fixed upstream. A merged pull request the
        # reporter names in the body is what the report is about, not its
        # fix; a fix arrives later, in a comment.
        record = prescreen_issue(
            self.root,
            "zauberzeug/nicegui#6339",
            executable=self.stub(
                "[]",
                self.issue(
                    6339,
                    "zauberzeug/nicegui",
                    "`Layer.current_leaflet` keeps the last `ui.leaflet` alive. "
                    "Found while reviewing #6294, where it is out of scope.",
                    "Layer.current_leaflet keeps the last ui.leaflet alive",
                ),
                pull_requests={
                    "zauberzeug/nicegui#6294": {
                        "number": 6294,
                        "state": "MERGED",
                        "title": "Fix leakage of tasks awaiting initialization",
                        "url": "https://github.com/zauberzeug/nicegui/pull/6294",
                        "mergedAt": "2026-09-16T00:00:00Z",
                        "mergeCommit": {"oid": "a" * 40},
                    }
                },
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn(ALREADY_FIXED_UPSTREAM, record["blocking"])
        self.assertIn(CITED_MERGED_IN_BODY, record["warnings"])
        self.assertEqual(record["cited_pull_requests"]["merged_in_body"], [6294])

    def test_a_maintainer_remark_in_a_cross_referencing_issue_warns(self) -> None:
        # pyinstaller#9224 carried the maintainers' preferred fix for #9121,
        # and nothing in the run read it. The stub answers every comments
        # path with the same rows, so this one stands for #9224's. Mailman #187.
        comment = {
            "body": "We could side-step #9121 by always passing --best, "
            "without having explicit fallbacks.",
            "author_association": "MEMBER",
            "created_at": "2025-08-30T10:40:44Z",
            "user": {"login": "rokm", "type": "User"},
        }
        record = prescreen_issue(
            self.root,
            "pyinstaller/pyinstaller#9121",
            executable=self.stub(
                "[]",
                self.issue(
                    9121,
                    "pyinstaller/pyinstaller",
                    "UPX leaves small binaries uncompressed.",
                    "upx NotCompressibleException",
                ),
                comments=[comment],
                timeline=[
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "html_url": (
                                    "https://github.com/pyinstaller/"
                                    "pyinstaller/issues/9224"
                                )
                            }
                        },
                    }
                ],
            ),
        )

        self.assertIn(MAINTAINER_REMARK_ELSEWHERE, record["warnings"])
        self.assertIn(
            "without having explicit fallbacks",
            record["claims"]["remarks_elsewhere"][0]["quote"],
        )

    def test_a_merge_shipped_long_before_the_issue_is_context_not_a_fix(
        self,
    ) -> None:
        # holoviz/panel#8335: a maintainer wrote "Possibly related to #1543",
        # merged in 2020, four years before the issue. That pull request
        # introduced the behaviour; it did not fix it. Mailman #147.
        comment = {
            "body": "Possibly related to https://github.com/holoviz/panel/pull/1543",
            "author_association": "MEMBER",
            "created_at": "2026-09-10T00:00:00Z",
            "user": {"login": "maintainer", "type": "User"},
        }
        record = prescreen_issue(
            self.root,
            "holoviz/panel#8335",
            executable=self.stub(
                "[]",
                self.issue(
                    8335,
                    "holoviz/panel",
                    "Embedding links widgets that only share a name.",
                    "embed merges widgets with the same name",
                ),
                comments=[comment],
                pull_requests={
                    "holoviz/panel#1543": {
                        "number": 1543,
                        "state": "MERGED",
                        "title": "Link widgets with same name during embed",
                        "url": "https://github.com/holoviz/panel/pull/1543",
                        "mergedAt": "2020-08-01T00:00:00Z",
                        "mergeCommit": {"oid": "c" * 40},
                    }
                },
            ),
        )

        self.assertNotIn(ALREADY_FIXED_UPSTREAM, record["blocking"])
        self.assertIn(CITED_MERGED_BEFORE_ISSUE, record["warnings"])
        self.assertEqual(
            record["cited_pull_requests"]["merged_before_issue"], [1543]
        )

    def test_a_cross_repository_pull_request_rejects_the_issue(self) -> None:
        # python-jsonschema/jsonschema#1497: the fix is open in the sibling
        # repository, `python-jsonschema/referencing#367`, and nothing in the
        # thread's text names a number.
        record = prescreen_issue(
            self.root,
            "python-jsonschema/jsonschema#1497",
            executable=self.stub(
                "[]",
                self.issue(
                    1497,
                    "python-jsonschema/jsonschema",
                    "`$dynamicRef` ignores `$dynamicAnchor` overrides in a root "
                    "schema with no `$id`.",
                    "$dynamicRef doesn't find $dynamicAnchor overrides",
                ),
                comments=[
                    {
                        "body": (
                            "Hi @Julian, can you please take a look at this issue "
                            "and the issue/PR in referencing that address it."
                        ),
                        "author_association": "NONE",
                        "created_at": "2026-09-10T00:00:00Z",
                        "user": {"login": "someone", "type": "User"},
                    }
                ],
                timeline=[
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "html_url": (
                                    "https://github.com/python-jsonschema/"
                                    "referencing/issues/366"
                                )
                            }
                        },
                    },
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "html_url": (
                                    "https://github.com/python-jsonschema/"
                                    "referencing/pull/367"
                                )
                            }
                        },
                    },
                ],
                pull_requests={
                    "python-jsonschema/referencing#367": {
                        "number": 367,
                        "state": "OPEN",
                        "title": "fix: resolve $dynamicRef for anonymous root schemas",
                        "url": (
                            "https://github.com/python-jsonschema/referencing/pull/367"
                        ),
                        "mergedAt": None,
                        "mergeCommit": None,
                    }
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [OPEN_PULL_REQUEST])
        decided = record["cited_pull_requests"]["decided_by"]
        self.assertEqual(decided["repository"], "python-jsonschema/referencing")
        self.assertEqual(decided["number"], 367)

    def _sibling_merge(self, closes: list[dict]) -> dict:
        # posit-dev/py-shiny#2497: shinyreact#306 merged a local workaround
        # and cross-referenced the issue; it changed only shinyreact's files.
        return prescreen_issue(
            self.root,
            "posit-dev/py-shiny#2497",
            executable=self.stub(
                "[]",
                self.issue(
                    2497,
                    "posit-dev/py-shiny",
                    "Returning `dict[str, int]` from a renderer fails pyright.",
                    "`Jsonifiable`'s `dict`/`list` arms are invariant",
                ),
                timeline=[
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "html_url": (
                                    "https://github.com/posit-dev/shinyreact/pull/306"
                                )
                            }
                        },
                    }
                ],
                pull_requests={
                    "posit-dev/shinyreact#306": {
                        "number": 306,
                        "state": "MERGED",
                        "title": "fix(py): type-check tests and examples",
                        "url": "https://github.com/posit-dev/shinyreact/pull/306",
                        "createdAt": "2026-09-12T00:00:00Z",
                        "mergedAt": "2026-09-12T22:50:24Z",
                        "mergeCommit": {"oid": "d" * 40},
                        "closingIssuesReferences": closes,
                    }
                },
            ),
        )

    def test_a_sibling_workaround_that_closes_nothing_here_only_warns(self) -> None:
        # Mailman #206.
        record = self._sibling_merge([])
        self.assertNotIn(ALREADY_FIXED_UPSTREAM, record["blocking"])
        self.assertIn(CITED_MERGED_ELSEWHERE, record["warnings"])

    def test_a_sibling_merge_that_closes_this_issue_still_blocks(self) -> None:
        record = self._sibling_merge(
            [
                {
                    "number": 2497,
                    "repository": {"name": "py-shiny", "owner": {"login": "posit-dev"}},
                    "url": "https://github.com/posit-dev/py-shiny/issues/2497",
                }
            ]
        )
        self.assertIn(ALREADY_FIXED_UPSTREAM, record["blocking"])

    def test_the_pull_request_this_run_filed_is_not_a_rival(self) -> None:
        # 495b9e8 taught the duplicate search that a filed run's own pull
        # request answers its own search. The same exclusion, one stage up.
        directory = prescreen_directory(self.root, "example/project", 7)
        (directory / "submission").mkdir(parents=True)
        (directory / "submission" / "provenance.json").write_text(
            json.dumps({"pull_request": 4242}), encoding="utf-8"
        )
        issue = self.issue(
            7,
            "example/project",
            "The command crashes on empty input. Filed as #4242.",
            "Crash on empty input",
        )
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                issue,
                pull_requests={
                    "example/project#4242": {
                        "number": 4242,
                        "state": "OPEN",
                        "title": "fix: handle empty input",
                        "url": "https://github.com/example/project/pull/4242",
                        "mergedAt": None,
                        "mergeCommit": None,
                    }
                },
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["cited_pull_requests"]["references"], [])

    def test_a_thread_naming_nothing_leaves_the_search_to_decide(self) -> None:
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["cited_pull_requests"]["references"], [])
        self.assertIn("none of them", record["cited_pull_requests"]["detail"])
        self.assertTrue(record["duplicate_search"]["success"])


class StalePriorAttemptTests(PrescreenTests):
    """A dormant attempt is prior art, not a claim on the issue.

    The operator decided this on 2026-09-17, after a hunt lost 40 of 66
    pre-screens to `open-pull-request`.
    """

    def issue(self, number: int = 7) -> dict:
        return {
            "number": number,
            "title": "Crash on empty input",
            "body": (
                "The command crashes on empty input. See "
                f"[#{number + 1}](https://github.com/example/project/pull/"
                f"{number + 1})."
            ),
            "state": "OPEN",
            "url": f"https://github.com/example/project/issues/{number}",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }

    def cited(self, *, days_old: int, association: str = "CONTRIBUTOR") -> dict:
        touched = datetime.now(UTC) - timedelta(days=days_old)
        return {
            "number": 8,
            "state": "OPEN",
            "title": "Guard the empty-input path",
            "url": "https://github.com/example/project/pull/8",
            "mergedAt": None,
            "mergeCommit": None,
            "createdAt": (touched - timedelta(days=5)).isoformat(),
            "updatedAt": touched.isoformat(),
            "isDraft": False,
            "authorAssociation": association,
        }

    def test_a_cited_attempt_untouched_for_months_no_longer_claims_it(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.cited(days_old=200)},
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn(OPEN_PULL_REQUEST, record["blocking"])
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["cited_pull_requests"]["open"], [])
        self.assertEqual(record["cited_pull_requests"]["stale"], [8])
        stale = record["stale_attempts"][0]
        self.assertEqual(stale["number"], 8)
        self.assertEqual(stale["state"], "open")
        self.assertGreaterEqual(stale["days_stale"], 199)
        self.assertIn("supersedes it", record["cited_pull_requests"]["detail"])

    def test_a_cited_attempt_still_moving_rejects_the_issue(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.cited(days_old=30)},
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [OPEN_PULL_REQUEST])
        self.assertEqual(record["cited_pull_requests"]["stale"], [])

    def test_a_maintainers_dormant_branch_still_rejects_the_issue(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={
                    "example/project#8": self.cited(
                        days_old=400, association="MEMBER"
                    )
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [OPEN_PULL_REQUEST])

    def test_a_cited_attempt_closed_without_merging_is_stale(self) -> None:
        closed = {
            **self.cited(days_old=3),
            "state": "CLOSED",
            "title": "An attempt the maintainers closed",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]", self.issue(), pull_requests={"example/project#8": closed}
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(
            record["stale_attempts"][0]["state"], "closed unmerged"
        )


class ReservedForHumansTests(PrescreenTests):
    """beetbox/beets#6984: the maintainer reserved it for human contributors."""

    def test_a_reserved_issue_is_rejected_before_a_run_exists(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    {
                        "body": (
                            "Marked as `good first issue` for **human** "
                            "contributors. Fully automated PRs from agents may "
                            "be rejected."
                        ),
                        "author_association": "CONTRIBUTOR",
                        "created_at": "2026-09-03T00:00:00Z",
                        "user": {"login": "semohr", "type": "User"},
                    }
                ],
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [ISSUE_RESERVED_FOR_HUMANS])
        self.assertIn("semohr", record["next"])
        self.assertEqual(len(record["claims"]["agent_exclusions"]), 1)
        self.assertNotIn("duplicate_search", record)

    def test_an_ordinary_thread_is_not_reserved(self) -> None:
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )

        self.assertNotIn(ISSUE_RESERVED_FOR_HUMANS, record.get("blocking", []))
        self.assertEqual(record["claims"]["agent_exclusions"], [])


class DesignUndecidedTests(PrescreenTests):
    """Mailman #124: zarr-python#2706 and marimo#6250 passed with the design open."""

    def maintainer(self, body: str, created_at: str) -> dict:
        return {
            "body": body,
            "author_association": "MEMBER",
            "created_at": created_at,
            "user": {"login": "d-v-b", "type": "User"},
        }

    def test_an_open_design_question_rejects_the_issue(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    self.maintainer(
                        "For the remaining work I'm not sure how we should "
                        "handle nested filesystems.",
                        "2026-09-03T00:00:00Z",
                    )
                ],
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [DESIGN_UNDECIDED])
        self.assertIn("d-v-b", record["next"])
        self.assertIn("not sure how we should", record["next"])
        self.assertEqual(len(record["claims"]["design_undecided"]), 1)
        self.assertNotIn("duplicate_search", record)

    def test_a_settled_design_does_not_block(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    self.maintainer(
                        "One option is to warn, another would be to raise.",
                        "2026-09-03T00:00:00Z",
                    ),
                    self.maintainer(
                        "Let's go with raising. PR welcome.", "2026-09-04T00:00:00Z"
                    ),
                ],
            ),
        )

        self.assertNotIn(DESIGN_UNDECIDED, record.get("blocking", []))
        self.assertEqual(record["claims"]["design_undecided"], [])


class MaintainerDeclinedTests(PrescreenTests):
    """Mailman #174: fsspec#1741 passed after a member called it correct."""

    maintainer = DesignUndecidedTests.maintainer

    def test_a_maintainer_turning_the_report_down_rejects_the_issue(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    self.maintainer(
                        "This is functioning correctly, with behaviour copied "
                        "from command-line `cp`.",
                        "2026-09-03T00:00:00Z",
                    )
                ],
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [MAINTAINER_DECLINED])
        self.assertIn("turned the report down", record["next"])
        self.assertEqual(len(record["claims"]["declined"]), 1)


class MaintainerDisputedTests(PrescreenTests):
    """Mailman #193: one batch passed ten threads and none was workable."""

    maintainer = DesignUndecidedTests.maintainer

    def verdict(self, *comments: dict) -> dict:
        return prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", comments=list(comments)),
        )

    def test_a_request_for_logs_rejects_the_issue(self) -> None:
        # huggingface_hub#3974 and #3871, verbatim.
        for body in (
            "Can you attach the \"Full crash report\" and the \"Key stack "
            "trace\" as stated in the description?",
            "Could you share the logs located at `~/.cache/huggingface/xet/logs` "
            "corresponding to a failed upload?",
        ):
            with self.subTest(body=body[:30]):
                record = self.verdict(self.maintainer(body, "2026-09-03T00:00:00Z"))

                self.assertEqual(record["verdict"], "reject")
                self.assertEqual(record["blocking"], [MAINTAINER_DISPUTED])
                self.assertIn("latest word", record["next"])

    def test_a_redirect_to_another_project_rejects_the_issue(self) -> None:
        # huggingface_hub#3795, verbatim.
        record = self.verdict(self.maintainer(
            "I'd recommend opening an issue in the "
            "https://github.com/huggingface/xet-core repository, the Xet team "
            "will be able to help.",
            "2026-09-03T00:00:00Z",
        ))

        self.assertEqual(record["blocking"], [MAINTAINER_DISPUTED])

    def test_a_later_confirmation_ends_the_dispute(self) -> None:
        record = self.verdict(
            self.maintainer("Could you share the logs?", "2026-09-03T00:00:00Z"),
            self.maintainer("Thanks, confirmed on main.", "2026-09-05T00:00:00Z"),
        )

        self.assertNotIn(MAINTAINER_DISPUTED, record.get("blocking", []))
        self.assertIsNone(record["claims"]["disputed"])

    def test_an_upstream_or_needs_verification_label_rejects_the_issue(self) -> None:
        # plotnine#975 and celery#9901.
        # commitizen#1315 `issue-status: wait-for-response`. Mailman #231.
        for label in (
            "upstream-bug",
            "Status: Needs Verification \u2718",
            "issue-status: wait-for-response",
        ):
            with self.subTest(label=label):
                issue = {
                    "number": 7,
                    "title": "Figure options in Quarto",
                    "body": "The size is ignored.",
                    "state": "OPEN",
                    "url": "https://github.com/example/project/issues/7",
                    "author": {"login": "reporter"},
                    "labels": [{"name": "bug"}, {"name": label}],
                    "createdAt": "2026-09-01T00:00:00Z",
                    "updatedAt": "2026-09-01T00:00:00Z",
                }
                record = prescreen_issue(
                    self.root, "example/project#7", executable=self.stub("[]", issue)
                )

                self.assertEqual(record["verdict"], "reject")
                self.assertIn(ISSUE_NOT_TRIAGED_HERE, record["blocking"])

    def test_wait_for_implementation_is_not_an_untriaged_label(self) -> None:
        # commitizen: "maintainers agree on the bug / feature". Mailman #231.
        from mailman.prescreen import _NOT_TRIAGED_LABEL

        self.assertIsNone(
            _NOT_TRIAGED_LABEL.search("issue-status: wait-for-implementation")
        )


    def test_a_template_requiring_a_label_the_issue_lacks_rejects_it(self) -> None:
        # mlflow#26266: the template says PRs need a maintainer-applied `ready`.
        body = (
            "> [!WARNING]\n> Before submitting a PR, please make sure that:\n"
            "> - A maintainer has triaged this issue and applied the `ready` label\n"
            "> - This issue has no assignee\n\n"
            "PRs not meeting these requirements may be automatically closed.\n\n"
            "The gateway ignores tool_choice."
        )
        for labels, blocked in (([{"name": "bug"}], True),
                                ([{"name": "bug"}, {"name": "ready"}], False)):
            with self.subTest(labels=[label["name"] for label in labels]):
                issue = {
                    "number": 8,
                    "title": "tool_choice ignored",
                    "body": body,
                    "state": "OPEN",
                    "url": "https://github.com/example/project/issues/8",
                    "author": {"login": "reporter"},
                    "labels": labels,
                    "createdAt": "2026-09-01T00:00:00Z",
                    "updatedAt": "2026-09-01T00:00:00Z",
                }
                record = prescreen_issue(
                    self.root, "example/project#8", executable=self.stub("[]", issue)
                )

                if blocked:
                    self.assertEqual(record["verdict"], "reject")
                    self.assertIn(ISSUE_LACKS_REQUIRED_LABEL, record["blocking"])
                    self.assertIn("ready", record["next"])
                else:
                    self.assertNotIn(
                        ISSUE_LACKS_REQUIRED_LABEL, record.get("blocking", [])
                    )

class PriorDiscussionTests(PrescreenTests):
    """A repository that closes a pull request whose issue nobody answered.

    getsentry/sentry-python's automation does exactly that, and a hunt spent
    its only run on a reviewer-approved patch for an issue in this state.
    https://github.com/wolfgang-aura/Mailman/issues/99
    """

    QUOTE = (
        "**Prior discussion required.** The referenced issue must show a "
        "conversation between you and a maintainer."
    )

    def record_prior_discussion(self) -> None:
        """The repository screen the pre-screen reads the rule out of."""
        path = screen_path(self.root, "example/project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "repository": "example/project",
                    "success": True,
                    "verdict": "pass",
                    "gates": [
                        {
                            "name": "policy",
                            "passed": True,
                            "blocking": True,
                            "detail": "",
                            "data": {
                                "requires_prior_discussion": True,
                                "constraints": [
                                    {
                                        "kind": "prior-discussion",
                                        "quote": self.QUOTE,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_an_unanswered_issue_is_rejected_before_a_run_exists(self) -> None:
        self.record_prior_discussion()
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    {
                        "body": "I see this too on 3.12.",
                        "author_association": "NONE",
                        "created_at": "2026-09-02T00:00:00Z",
                        "user": {"login": "another", "type": "User"},
                    }
                ],
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [NO_MAINTAINER_REPLY])
        self.assertIn(NO_MAINTAINER_REPLY, DECIDABLE)
        self.assertTrue(record["prior_discussion"]["required"])
        self.assertEqual(record["prior_discussion"]["quote"], self.QUOTE)
        self.assertFalse(record["prior_discussion"]["maintainer_replied"])
        self.assertIn("Prior discussion required", record["next"])
        self.assertNotIn("duplicate_search", record)

    def test_a_maintainer_reply_clears_the_rule(self) -> None:
        self.record_prior_discussion()
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[
                    {
                        "body": "Thanks, that is a bug. A fix is welcome.",
                        "author_association": "MEMBER",
                        "created_at": "2026-09-02T00:00:00Z",
                        "user": {"login": "maintainer", "type": "User"},
                    }
                ],
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertTrue(record["prior_discussion"]["maintainer_replied"])

    def record_shortlist_row(self, **flags) -> None:
        """Add the screen's shortlist row for #7, with the given flags."""
        path = screen_path(self.root, "example/project")
        screen = json.loads(path.read_text(encoding="utf-8"))
        screen["gates"].append(
            {"name": "saturation", "data": {"shortlist": [{"number": 7, **flags}]}}
        )
        path.write_text(json.dumps(screen), encoding="utf-8")

    def _unanswered(self) -> dict:
        return prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )

    def test_a_shortlist_reply_flag_is_triage_evidence(self) -> None:
        # Mailman #135: the screen already read the thread. A reply it saw
        # counts even when this read of the thread does not show one.
        self.record_prior_discussion()
        self.record_shortlist_row(maintainer_filed=False, maintainer_replied=True)

        record = self._unanswered()

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn(NO_MAINTAINER_REPLY, record["blocking"])
        self.assertTrue(record["prior_discussion"]["shortlist_engaged"])
        self.assertFalse(record["prior_discussion"]["maintainer_replied"])
        self.assertTrue(record["shortlist_engagement"]["engaged"])

    def test_a_shortlist_filed_flag_is_triage_evidence(self) -> None:
        self.record_prior_discussion()
        self.record_shortlist_row(maintainer_filed=True, maintainer_replied=False)

        record = self._unanswered()

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn(NO_MAINTAINER_REPLY, record["blocking"])

    def test_a_shortlist_row_without_flags_keeps_the_gate(self) -> None:
        # A screen written before f94d449: no flags, so the thread decides.
        self.record_prior_discussion()
        self.record_shortlist_row()

        record = self._unanswered()

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [NO_MAINTAINER_REPLY])
        self.assertFalse(record["shortlist_engagement"]["engaged"])

    def test_false_or_unread_shortlist_flags_keep_the_gate(self) -> None:
        self.record_prior_discussion()
        self.record_shortlist_row(maintainer_filed=False, maintainer_replied=None)

        record = self._unanswered()

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [NO_MAINTAINER_REPLY])

    def test_a_shortlist_flag_clears_the_unacknowledged_warning(self) -> None:
        self.record_prior_discussion()
        self.record_shortlist_row(maintainer_filed=False, maintainer_replied=True)
        aged = {"created_at": (datetime.now(UTC) - timedelta(days=99)).isoformat()}

        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue_api=aged),
        )

        self.assertNotIn(UNACKNOWLEDGED_ISSUE, record["warnings"])
        self.assertFalse(record["acknowledgement"]["unacknowledged"])
        self.assertTrue(record["acknowledgement"]["shortlist_engaged"])

    def test_without_a_shortlist_flag_the_old_report_still_warns(self) -> None:
        self.record_prior_discussion()
        self.record_shortlist_row(maintainer_filed=False, maintainer_replied=False)
        aged = {"created_at": (datetime.now(UTC) - timedelta(days=99)).isoformat()}

        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue_api=aged),
        )

        self.assertIn(UNACKNOWLEDGED_ISSUE, record["warnings"])

    def test_a_repository_without_the_rule_does_not_need_a_reply(self) -> None:
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertFalse(record["prior_discussion"]["required"])
        self.assertFalse(record["prior_discussion"]["maintainer_replied"])


class MaintainerClosedAttemptTests(StalePriorAttemptTests):
    """Who closed the earlier attempt decides what its closure meant.

    skfolio#307 and wagtail#14384 were closed by maintainers who rejected the
    change, and both passed this stage with a stale-prior-attempt warning.
    tqdm#1816 and #1818 were closed by their own authors, which is the shape
    the stale rule is for.
    """

    def closed(self, *, author: str = "outsider") -> dict:
        return {
            **self.cited(days_old=120),
            "state": "CLOSED",
            "title": "Add a guard to the empty-input path",
            "author": {"login": author},
        }

    def closing_event(self, *, actor: str, association: str) -> list[dict]:
        return [
            {
                "event": "commented",
                "actor": {"login": actor},
                "author_association": association,
                "body": "This is not the direction we want.",
            },
            {"event": "closed", "actor": {"login": actor}},
        ]

    def test_a_maintainer_closure_is_a_rejection_not_a_stale_attempt(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.closed()},
                timelines={
                    8: self.closing_event(actor="maintainer", association="MEMBER")
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [MAINTAINER_CLOSED_ATTEMPT])
        self.assertIn(MAINTAINER_CLOSED_ATTEMPT, DECIDABLE)
        self.assertEqual(record["stale_attempts"], [])
        self.assertNotIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["cited_pull_requests"]["maintainer_closed"], [8])
        rejected = record["maintainer_closed_attempts"][0]
        self.assertEqual(rejected["number"], 8)
        self.assertEqual(rejected["closed_by"]["login"], "maintainer")
        self.assertEqual(rejected["closed_by"]["association"], "MEMBER")
        self.assertTrue(rejected["closed_by"]["maintainer"])
        self.assertIn("said no", record["next"])

    def _closed_then_labelled(self, labelled_at: str) -> dict:
        closure = [
            {"event": "closed", "actor": {"login": "maintainer"},
             "author_association": "MEMBER", "created_at": "2026-09-29T17:12:38Z"}
        ]
        label = [
            {"event": "labeled", "actor": {"login": "maintainer"},
             "label": {"name": "confirmed"}, "created_at": labelled_at}
        ]
        return prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                timeline=label,
                pull_requests={"example/project#8": self.closed()},
                timelines={8: closure},
            ),
        )

    def test_a_confirmed_label_after_the_closure_turns_the_block_into_a_warning(
        self,
    ) -> None:
        # waylan closed Python-Markdown#1645 for special-casing `<picture>`,
        # then labelled #1643 `confirmed` 29 minutes later. Mailman #378.
        record = self._closed_then_labelled("2026-09-29T17:41:35Z")

        self.assertNotIn(MAINTAINER_CLOSED_ATTEMPT, record["blocking"])
        self.assertIn(MAINTAINER_CLOSED_ATTEMPT_REAFFIRMED, record["warnings"])
        self.assertEqual(record["maintainer_closed_attempts"], [])
        self.assertEqual(record["reaffirmed_closed_attempts"][0]["number"], 8)

    def test_a_label_from_before_the_closure_does_not_reaffirm(self) -> None:
        record = self._closed_then_labelled("2026-09-29T16:00:00Z")

        self.assertIn(MAINTAINER_CLOSED_ATTEMPT, record["blocking"])
        self.assertEqual(record["reaffirmed_closed_attempts"], [])

    def test_a_closed_attempt_only_the_search_found_is_read_for_its_closer(
        self,
    ) -> None:
        # stanza#1677: #1678 and #1681 were closed by the maintainer, found
        # only by the duplicate search, and recorded with `closed_by: null`.
        # https://github.com/wolfgang-aura/Mailman/issues/268
        issue = {**self.issue(), "body": "The command crashes on empty input."}
        found = json.dumps(
            [
                {
                    "number": 8,
                    "title": "Guard the empty-input path",
                    "body": "Fixes #7",
                    "state": "CLOSED",
                    "url": "https://github.com/example/project/pull/8",
                    "createdAt": "2026-09-10T00:00:00Z",
                    "updatedAt": "2026-09-11T00:00:00Z",
                    "isDraft": False,
                }
            ]
        )
        attempt = {
            **self.closed(),
            "createdAt": "2026-09-10T00:00:00Z",
            "updatedAt": "2026-09-11T00:00:00Z",
            "closedAt": "2026-09-11T00:00:00Z",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                found,
                issue,
                pull_requests={"example/project#8": attempt},
                timelines={
                    8: self.closing_event(actor="maintainer", association="MEMBER")
                },
            ),
        )

        self.assertEqual(record["prior_art"]["requested"], [8])
        self.assertEqual(record["verdict"], "reject")
        self.assertIn(MAINTAINER_CLOSED_ATTEMPT, record["blocking"])
        self.assertNotIn(STALE_PRIOR_ATTEMPT, record["warnings"])

    def test_an_author_closing_their_own_attempt_is_still_stale(self) -> None:
        # tqdm#1816 and #1818. Nobody judged the change.
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.closed(author="outsider")},
                timelines={
                    8: self.closing_event(actor="outsider", association="CONTRIBUTOR")
                },
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["maintainer_closed_attempts"], [])
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["stale_attempts"][0]["number"], 8)
        self.assertEqual(
            record["stale_attempts"][0]["closed_by"]["login"], "outsider"
        )

    def test_a_withdrawal_after_maintainers_questioned_ai_authorship_is_a_rejection(
        self,
    ) -> None:
        # towncrier#756: members asked whether an LLM wrote it and cited
        # GPTZero; the author then closed it. Mailman #304.
        attempt = {
            **self.closed(author="outsider"),
            "comments": [
                {
                    "author": {"login": "glyph"},
                    "authorAssociation": "MEMBER",
                    "body": "Hi - was any LLM used to generate this PR or its description?",
                },
                {
                    "author": {"login": "outsider"},
                    "authorAssociation": "NONE",
                    "body": "Closing this one out.",
                },
            ],
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": attempt},
                timelines={
                    8: self.closing_event(actor="outsider", association="CONTRIBUTOR")
                },
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(MAINTAINER_CLOSED_ATTEMPT, record["blocking"])
        self.assertNotIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        rejected = record["maintainer_closed_attempts"][0]
        self.assertIn("AI authorship", rejected["closed_by"]["detail"])

    def test_a_maintainer_comment_without_ai_doubt_leaves_a_withdrawal_stale(
        self,
    ) -> None:
        attempt = {
            **self.closed(author="outsider"),
            "comments": [
                {
                    "author": {"login": "glyph"},
                    "authorAssociation": "MEMBER",
                    "body": "Could you add a test for the create command?",
                }
            ],
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": attempt},
                timelines={
                    8: self.closing_event(actor="outsider", association="CONTRIBUTOR")
                },
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])

    def test_an_unreadable_closure_keeps_the_old_behaviour_and_says_so(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.closed()},
                timelines={8: []},
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        closure = record["stale_attempts"][0]["closed_by"]
        self.assertIsNone(closure["login"])
        self.assertFalse(closure["maintainer"])
        self.assertIn("could not be determined", closure["detail"])


class MaintainerOwnedFixTests(StalePriorAttemptTests):
    """A maintainer's own parked fix, where GitHub hides that he is one.

    marimo-team/marimo#9862: mscolnick's draft said "Fixes #9808", the stale
    bot closed it, and his organisation membership is private, so GitHub
    called him CONTRIBUTOR and the issue passed with a stale-attempt warning.
    The repository screen records who merges; that set decides. Mailman #203.
    """

    def parked(self, *, body: str = "Fixes #7") -> dict:
        return {
            **self.cited(days_old=59, association="CONTRIBUTOR"),
            "state": "CLOSED",
            "isDraft": True,
            "title": "fix: guard the empty-input path",
            "author": {"login": "mscolnick"},
            "body": body,
        }

    def record_maintainers(self, logins: list[str]) -> None:
        path = screen_path(self.root, "example/project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "repository": "example/project",
                    "success": True,
                    "verdict": "pass",
                    "gates": [],
                    "maintainer_logins": logins,
                }
            ),
            encoding="utf-8",
        )

    def prescreen(self, pull: dict) -> dict:
        return prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": pull},
                timelines={
                    8: [{"event": "closed", "actor": {"login": "github-actions[bot]"}}]
                },
            ),
        )

    def test_a_private_maintainers_parked_fix_rejects_the_issue(self) -> None:
        self.record_maintainers(["MScolnick", "akshayka"])
        record = self.prescreen(self.parked())

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [MAINTAINER_OWNED_FIX])
        self.assertIn(MAINTAINER_OWNED_FIX, DECIDABLE)
        self.assertEqual(record["stale_attempts"], [])
        self.assertNotIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["cited_pull_requests"]["maintainer_owned"], [8])
        owned = record["maintainer_owned_attempts"][0]
        self.assertEqual(owned["number"], 8)
        self.assertEqual(owned["author"], "mscolnick")
        self.assertIn("maintainer", record["next"])

    def test_a_screen_from_before_the_merger_record_is_read_live(self) -> None:
        # marimo's screen dated from 2026-09-28, the day before #203; its lead
        # maintainer's closed fix read as an outsider's. Mailman #263.
        path = screen_path(self.root, "example/project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"repository": "example/project", "success": True,
                                    "verdict": "pass", "gates": []}), encoding="utf-8")
        executable = self.stub("[]", self.issue(),
                               pull_requests={"example/project#8": self.parked()},
                               timelines={8: [{"event": "closed",
                                               "actor": {"login": "github-actions[bot]"}}]})
        answer = {"data": {"repository": {"pullRequests": {"nodes": [
            {"mergedBy": {"login": "mscolnick", "__typename": "User"}}]}}}}
        (Path(executable).parent / "mergers.json").write_text(json.dumps(answer), encoding="utf-8")

        record = prescreen_issue(self.root, "example/project#7", executable=executable)

        self.assertEqual(record["blocking"], [MAINTAINER_OWNED_FIX])
        self.assertEqual(record["maintainer_logins_known"], 1)

    def test_without_a_screen_record_the_old_warning_stands(self) -> None:
        record = self.prescreen(self.parked())

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["stale_attempts"][0]["number"], 8)
        self.assertEqual(record["cited_pull_requests"]["maintainer_owned"], [])

    def test_a_maintainers_attempt_that_does_not_close_the_issue_passes(
        self,
    ) -> None:
        # Read as a MEMBER's closed attempt always was: not a claim.
        self.record_maintainers(["mscolnick"])
        record = self.prescreen(self.parked(body="Related to #7, a first look."))

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["cited_pull_requests"]["maintainer_owned"], [])

    def test_a_fix_for_another_issue_does_not_count(self) -> None:
        self.record_maintainers(["mscolnick"])
        record = self.prescreen(self.parked(body="Fixes #70"))

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["cited_pull_requests"]["maintainer_owned"], [])

    def test_a_member_with_public_membership_is_caught_too(self) -> None:
        pull = {**self.parked(), "authorAssociation": "MEMBER"}
        record = self.prescreen(pull)

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [MAINTAINER_OWNED_FIX])


class MaintainerPendingFixTests(StalePriorAttemptTests):
    """A project voice's closed fix the duplicate search found is parked work.

    marimo#9808 passed with a `stale-prior-attempt` warning over #9862,
    mscolnick's own fix closed by github-actions for inactivity: the prior-art
    record carried the author but not the association. Mailman #199.
    """

    record_maintainers = MaintainerOwnedFixTests.record_maintainers

    def found(self) -> str:
        return json.dumps(
            [
                {
                    "number": 8,
                    "title": "fix: guard the empty-input path",
                    "body": "Fixes #7",
                    "state": "CLOSED",
                    "url": "https://github.com/example/project/pull/8",
                    "createdAt": "2026-09-10T00:00:00Z",
                    "updatedAt": "2026-09-11T00:00:00Z",
                    "isDraft": False,
                }
            ]
        )

    def attempt(self, *, author: str, association: str) -> dict:
        return {
            **self.cited(days_old=120, association=association),
            "state": "CLOSED",
            "title": "fix: guard the empty-input path",
            "author": {"login": author},
            "body": "Fixes #7",
            "createdAt": "2026-09-10T00:00:00Z",
            "updatedAt": "2026-09-11T00:00:00Z",
            "closedAt": "2026-09-11T00:00:00Z",
        }

    def search_prescreen(
        self, pull: dict, *, closer: str = "github-actions[bot]",
        closer_association: str = "",
    ) -> dict:
        # No citation in the body: only the duplicate search finds #8.
        issue = {**self.issue(), "body": "The command crashes on empty input."}
        events: list[dict] = [{"event": "closed", "actor": {"login": closer}}]
        if closer_association:
            events.insert(
                0,
                {
                    "event": "commented",
                    "actor": {"login": closer},
                    "author_association": closer_association,
                    "body": "Not the direction we want.",
                },
            )
        return prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                self.found(),
                issue,
                pull_requests={"example/project#8": pull},
                timelines={8: events},
            ),
        )

    def prior_attempt(self) -> dict:
        directory = prescreen_directory(self.root, "example/project", 7)
        record = json.loads((directory / "prior-art.json").read_text(encoding="utf-8"))
        return record["attempts"][0]

    def test_a_members_bot_closed_fix_blocks_instead_of_warning(self) -> None:
        record = self.search_prescreen(
            self.attempt(author="keeper", association="MEMBER")
        )

        self.assertEqual(record["prior_art"]["requested"], [8])
        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [MAINTAINER_PENDING_FIX])
        self.assertIn(MAINTAINER_PENDING_FIX, DECIDABLE)
        self.assertNotIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["stale_attempts"], [])
        pending = record["maintainer_pending_attempts"][0]
        self.assertEqual(pending["number"], 8)
        self.assertEqual(pending["author"], "keeper")
        self.assertIn("keeper", record["next"])
        self.assertEqual(self.prior_attempt()["author_association"], "MEMBER")

    def test_a_collaborators_self_closed_fix_blocks(self) -> None:
        record = self.search_prescreen(
            self.attempt(author="helper", association="COLLABORATOR"),
            closer="helper",
        )

        self.assertEqual(record["blocking"], [MAINTAINER_PENDING_FIX])
        self.assertTrue(self.prior_attempt()["author_is_project_voice"])

    def test_a_screened_maintainer_reported_as_contributor_blocks(self) -> None:
        # mscolnick's membership is private; the screen's merger set decides.
        self.record_maintainers(["mscolnick"])
        record = self.search_prescreen(
            self.attempt(author="mscolnick", association="CONTRIBUTOR")
        )

        self.assertEqual(record["blocking"], [MAINTAINER_PENDING_FIX])
        attempt = self.prior_attempt()
        self.assertEqual(attempt["author_association"], "CONTRIBUTOR")
        self.assertTrue(attempt["author_is_project_voice"])

    def test_an_outsiders_bot_closed_attempt_is_still_stale(self) -> None:
        record = self.search_prescreen(
            self.attempt(author="outsider", association="CONTRIBUTOR")
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(record["maintainer_pending_attempts"], [])
        self.assertFalse(self.prior_attempt()["author_is_project_voice"])

    def test_another_maintainer_closing_it_is_a_rejection_not_pending(self) -> None:
        record = self.search_prescreen(
            self.attempt(author="keeper", association="MEMBER"),
            closer="lead",
            closer_association="OWNER",
        )

        self.assertEqual(record["blocking"], [MAINTAINER_CLOSED_ATTEMPT])
        self.assertEqual(record["maintainer_pending_attempts"], [])


class DuplicatePolicyTests(StalePriorAttemptTests):
    """A repository that rejects a second pull request for an issue, unread.

    urllib3's contributing guide says so in one sentence, nothing read it, and
    the stale-attempt rule walked into it: a dormant attempt there is still the
    pull request the maintainers count.
    """

    QUOTE = (
        "Duplicate pull requests for the same issue, including alternative "
        "solutions, will be rejected without review unless a maintainer has "
        "approved opening an alternative pull request in advance."
    )

    def record_duplicate_rule(self) -> None:
        """The repository screen the pre-screen reads the rule out of."""
        path = screen_path(self.root, "example/project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "repository": "example/project",
                    "success": True,
                    "verdict": "pass",
                    "gates": [
                        {
                            "name": "policy",
                            "passed": True,
                            "blocking": True,
                            "detail": "",
                            "data": {
                                "forbids_duplicate_pull_requests": True,
                                "constraints": [
                                    {
                                        "kind": "no-duplicate-pull-requests",
                                        "quote": self.QUOTE,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_a_dormant_attempt_cannot_be_superseded_here(self) -> None:
        self.record_duplicate_rule()
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.cited(days_old=200)},
            ),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertEqual(record["blocking"], [DUPLICATE_FORBIDDEN_OPEN_ATTEMPT])
        self.assertIn(DUPLICATE_FORBIDDEN_OPEN_ATTEMPT, DECIDABLE)
        self.assertTrue(record["duplicate_policy"]["forbidden"])
        self.assertEqual(record["duplicate_policy"]["quote"], self.QUOTE)
        self.assertEqual(record["duplicate_blocked_attempts"][0]["number"], 8)
        # It is no longer offered as prior art to supersede.
        self.assertEqual(record["stale_attempts"], [])
        self.assertNotIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertIn("nothing to supersede", record["next"])

    def test_a_closed_unmerged_attempt_is_still_only_prior_art(self) -> None:
        self.record_duplicate_rule()
        closed = {
            **self.cited(days_old=200),
            "state": "CLOSED",
            "title": "An attempt its author withdrew",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]", self.issue(), pull_requests={"example/project#8": closed}
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertEqual(record["duplicate_blocked_attempts"], [])
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])
        self.assertEqual(
            record["stale_attempts"][0]["state"], "closed unmerged"
        )

    def test_without_the_rule_the_dormant_attempt_is_superseded_as_before(
        self,
    ) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                self.issue(),
                pull_requests={"example/project#8": self.cited(days_old=200)},
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertFalse(record["duplicate_policy"]["forbidden"])
        self.assertEqual(record["duplicate_blocked_attempts"], [])
        self.assertIn(STALE_PRIOR_ATTEMPT, record["warnings"])


class InitRunGateTests(PrescreenTests):
    def init_run(self, *extra: str) -> tuple[int, str]:
        out, err = StringIO(), StringIO()
        arguments = [
            "init-run",
            "--repository",
            "https://github.com/example/project.git",
            "--issue",
            "https://github.com/example/project/issues/7",
            "--base-commit",
            "a" * 40,
            "--primary",
            "codex",
            "--reviewer",
            "claude",
            "--primary-model",
            "m",
            "--reviewer-model",
            "m",
            "--data-root",
            str(self.root),
            *extra,
        ]
        with redirect_stdout(out), redirect_stderr(err):
            code = main(arguments)
        return code, out.getvalue() + err.getvalue()

    def test_init_run_refuses_an_issue_that_was_never_pre_screened(self) -> None:
        code, output = self.init_run()
        self.assertEqual(code, 2)
        self.assertIn("no pre-screen", output)

    def test_init_run_proceeds_after_a_passing_pre_screen(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        code, output = self.init_run()
        self.assertEqual(code, 0)
        self.assertIn("run_id", output)
        run_id = json.loads(output)["run_id"]
        self.assertTrue((self.root / run_id / "prescreen.json").is_file())

    def test_skipping_the_pre_screen_records_the_reason(self) -> None:
        code, output = self.init_run("--no-prescreen", "operator asked for this one")
        self.assertEqual(code, 0)
        run_id = json.loads(output)["run_id"]
        skipped = json.loads(
            (self.root / run_id / "prescreen-skipped.json").read_text(encoding="utf-8")
        )
        self.assertEqual(skipped["reason"], "operator asked for this one")


class RankingTests(unittest.TestCase):
    """The pre-screen record repeats the shortlist's score and its reasons.

    A coordinator reading two passes needs to see which one a maintainer
    asked for. Not a subclass of `PrescreenTests`: the base tests would run
    again for nothing. https://github.com/wolfgang-aura/Mailman/issues/102
    """

    stub = PrescreenTests.stub

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "runs"
        self.root.mkdir(parents=True)

    def _reply(self, body: str, association: str) -> dict:
        return {
            "body": body,
            "author_association": association,
            "created_at": datetime.now(UTC).isoformat(),
            "user": {"login": "somebody", "type": "User"},
        }

    def test_a_maintainer_invitation_is_recorded_with_its_reasons(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[self._reply("Happy to accept a PR for this.", "MEMBER")],
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["claims"]["invitations"], 1)
        self.assertEqual(
            record["ranking"]["reasons"],
            [MAINTAINER_INVITED, RECENT, NO_LINKED_PR],
        )
        self.assertEqual(load_prescreen(self.root, "example/project", 7), record)

    def test_the_same_words_from_an_outsider_rank_nothing(self) -> None:
        # The fixture issue was opened on 2026-09-01, outside the recent
        # window, and an outsider's reply does not move the maintainer clock.
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub(
                "[]",
                comments=[self._reply("Happy to accept a PR for this.", "NONE")],
            ),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["claims"]["invitations"], 0)
        # Nor does it acknowledge the report: the issue is past the grace
        # window with no maintainer reply, so it is demoted. Mailman #116.
        self.assertEqual(
            record["ranking"]["reasons"], [NO_LINKED_PR, UNACKNOWLEDGED]
        )




class PullRequestBaseTests(unittest.TestCase):
    """stanza takes pull requests only against `dev`. Mailman #258."""

    setUp = PrescreenTests.setUp
    stub = PrescreenTests.stub

    record = {
        "repository": "stanfordnlp/stanza",
        "pull_request_base": {
            "branch": "dev",
            "quote": "create a pull request **against the `dev` branch**.",
            "default_branch": "main",
        },
    }

    def test_a_base_commit_behind_the_named_branch_is_refused(self) -> None:
        refusal = base_branch_refusal(
            self.record,
            base_commit="a0a64ca",
            executable=self.stub("[]", compare={"status": "ahead", "ahead_by": 91}),
        )
        self.assertIsNotNone(refusal)
        self.assertIn("`dev`", refusal)
        self.assertIn("91", refusal)

    def test_the_head_of_the_named_branch_is_accepted(self) -> None:
        for compare in (
            {"status": "identical", "ahead_by": 0},
            {"status": "ahead", "ahead_by": 2},
        ):
            with self.subTest(compare=compare):
                self.assertIsNone(
                    base_branch_refusal(
                        self.record,
                        base_commit="e0767aa",
                        executable=self.stub("[]", compare=compare),
                    )
                )

    def test_a_commit_off_the_named_branch_is_refused(self) -> None:
        self.assertIsNotNone(
            base_branch_refusal(
                self.record,
                base_commit="a0a64ca",
                executable=self.stub("[]", compare={"status": "diverged", "ahead_by": 3}),
            )
        )

    def test_no_named_branch_costs_no_call(self) -> None:
        self.assertIsNone(
            base_branch_refusal(
                {"repository": "example/project", "pull_request_base": None},
                base_commit="abc",
                executable="no-such-gh",
            )
        )


class ScopedLabelTests(unittest.TestCase):
    def test_a_scoped_label_is_read_by_its_name(self) -> None:
        # pypdf#4105: `status: needs discussion`, `type: enhancement` and
        # `kind/feature` matched no exact name and nothing later caught them.
        # Mailman #334.
        from mailman.prescreen import _issue_blocking

        def blocking(*labels: str) -> list[str]:
            return _issue_blocking(
                {"success": True, "state": "OPEN", "labels": list(labels)}
            )

        for label, code in (
            ("status: needs discussion", "issue-under-discussion"),
            ("Status: Needs-Discussion", "issue-under-discussion"),
            ("needs_discussion", "issue-under-discussion"),
            ("type: enhancement", ISSUE_NOT_BOUNDED_FIX),
            ("kind/feature", ISSUE_NOT_BOUNDED_FIX),
            ("Type: Feature Request", ISSUE_NOT_BOUNDED_FIX),
        ):
            with self.subTest(label=label):
                self.assertIn(code, blocking(label))
        for label in (
            "type: bug",
            "kind/bug",
            "area/design",
            "component: project",
            "topic: discussion forum",
        ):
            with self.subTest(label=label):
                self.assertEqual(blocking(label), [])



class UnreadCitedPullRequestTests(unittest.TestCase):
    """A cited pull request `gh` could not read may be an open rival. Mailman
    #344. Not a `PrescreenTests` subclass, so its base tests do not run again."""

    setUp = PrescreenTests.setUp
    stub = PrescreenTests.stub

    def test_an_unread_cited_pull_request_holds_the_issue(self) -> None:
        executable = self.stub(
            "[]",
            comments=[
                {
                    "id": 1,
                    "user": {"login": "someone", "type": "User"},
                    "author_association": "NONE",
                    "body": "There is a fix for this in #12.",
                    "created_at": "2026-09-02T00:00:00Z",
                }
            ],
        )
        (Path(executable).parent / "pr-view-fails.txt").write_text(
            "HTTP 403: API rate limit exceeded\n", encoding="utf-8"
        )

        record = prescreen_issue(self.root, "example/project#7", executable=executable)

        self.assertEqual(record["verdict"], "reject")
        self.assertIn("cited-pull-request-unread", record["blocking"])
        self.assertIn("could not be read", record["next"])

    def test_an_issue_number_cited_in_the_thread_does_not_hold_it(self) -> None:
        executable = self.stub(
            "[]",
            comments=[
                {
                    "id": 1,
                    "user": {"login": "someone", "type": "User"},
                    "author_association": "NONE",
                    "body": "Same as #12.",
                    "created_at": "2026-09-02T00:00:00Z",
                }
            ],
        )

        record = prescreen_issue(self.root, "example/project#7", executable=executable)

        self.assertNotIn("cited-pull-request-unread", record["blocking"])
        self.assertEqual(
            [row["number"] for row in record["cited_pull_requests"]["skipped"]], [12]
        )


class ReporterIsMaintainerTests(unittest.TestCase):
    """A reporter the screen knows as a maintainer is the project speaking,
    whatever association GitHub shows on the issue. Mailman #345."""

    def test_a_maintainer_reporter_is_neither_unacknowledged_nor_untriaged(
        self,
    ) -> None:
        from mailman.prescreen import _acknowledgement

        found = _acknowledgement(
            {
                "success": True,
                "reporter_association": "NONE",
                "reporter_is_maintainer": True,
                "maintainer_replied": False,
                "maintainer_labelled": False,
                "invitations": [],
                "issue_created_at": "2026-01-01T00:00:00Z",
            }
        )

        self.assertFalse(found["unacknowledged"])
        self.assertFalse(found["untriaged"])


class FailedClaimCheckTests(unittest.TestCase):
    """A claim check that never ran is no evidence the issue is free. It
    passed, with no ask-first, and orchestrate later failed on the missing
    claims.json. Mailman #342. Not a `PrescreenTests` subclass, so its base
    tests do not run again."""

    setUp = PrescreenTests.setUp
    stub = PrescreenTests.stub

    def test_a_failed_claim_check_holds_the_issue(self) -> None:
        from unittest.mock import patch

        failed = {"success": False, "detail": "API rate limit exceeded"}
        with patch("mailman.prescreen.read_claims", return_value=failed):
            record = prescreen_issue(
                self.root, "example/project#7", executable=self.stub("[]")
            )

        self.assertNotEqual(record["verdict"], "pass")
        self.assertIn("no-claim-check", record["blocking"])
        self.assertIn("prescreen example/project#7", record["next"])


if __name__ == "__main__":
    unittest.main()
