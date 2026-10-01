from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.issue import issue_opened_at, load_issue_record, predates_issue
from mailman.models import RunRecord, RunStatus
from mailman.reproduction import (
    REPRODUCTION_FILENAME,
    merge_is_in_base,
    not_reproductions,
)
from mailman.toolchain import resolve_tool
from mailman.workspace import WORKSPACE_DIRECTORY
from mailman.touched_tests import (
    BASELINE_NODE_LIMIT,
    TOUCHED_TESTS_CAP,
    TOUCHED_TESTS_CODE_VERSION,
    deselects_for,
    load_touched_tests,
    resolve_workspace,
    run_touched_tests,
    select_test_files,
    touched_tests_verdict,
)


SUBMISSION_SCHEMA_VERSION = 1

_TRAILER_CHOICES = frozenset({"forbidden", "optional", "encouraged", "required"})
_STANCE_CHOICES = frozenset(
    {"permitted", "permitted_with_disclosure", "restricted", "forbidden", "unknown"}
)


@dataclass(frozen=True)
class TargetPolicy:
    """What one upstream project asks of a contributor.

    Every field here has to come from that project's own written policy. An
    unread policy is `unknown`, which blocks submission preparation rather than
    guessing a permissive default.
    """

    name: str
    policy_url: str
    stance: str = "unknown"
    policy_read_on: str | None = None
    disclosure_required: bool = False
    ai_trailer: str = "optional"
    ai_trailer_form: str | None = None
    requires_linked_issue: bool = False
    requires_maintainer_assignment: bool = False
    requires_duplicate_search: bool = False
    #: The project asks that comments, issues and pull request descriptions be
    #: written in the author's own words. Mailman's draft body is model-written,
    #: so under this rule the body is the violation however good the patch is.
    #: See https://github.com/wolfgang-aura/Mailman/issues/43.
    requires_own_words: bool = False
    #: Set once a person has rewritten the body themselves, which is the only
    #: thing that can satisfy `requires_own_words`.
    own_words_confirmed: bool = False
    changelog_directory: str | None = None
    changelog_filename_template: str | None = None
    checklist: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a target policy needs a name")
        if self.stance not in _STANCE_CHOICES:
            raise ValueError(
                f"stance must be one of {sorted(_STANCE_CHOICES)}, not {self.stance!r}"
            )
        if self.ai_trailer not in _TRAILER_CHOICES:
            raise ValueError(
                f"ai_trailer must be one of {sorted(_TRAILER_CHOICES)}, "
                f"not {self.ai_trailer!r}"
            )
        if self.ai_trailer == "required" and not self.ai_trailer_form:
            raise ValueError(
                "ai_trailer_required needs ai_trailer_form, for example "
                "'Assisted-by: {agent}'"
            )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TargetPolicy:
        known = {f for f in cls.__dataclass_fields__ if f != "schema_version"}
        unknown = sorted(set(payload) - known - {"schema_version"})
        if unknown:
            raise ValueError(f"unknown target policy fields: {', '.join(unknown)}")
        return cls(**{key: value for key, value in payload.items() if key in known})

    @classmethod
    def load(cls, path: Path) -> TargetPolicy:
        # utf-8-sig: PowerShell 5.1 writes a BOM when the operator edits it. #218
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("a target policy file must contain a JSON object")
        return cls.from_dict(payload)


@dataclass(frozen=True)
class Finding:
    code: str
    blocking: bool
    detail: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "blocking": self.blocking,
            "detail": self.detail,
            "path": self.path,
        }


def _split_file_diffs(diff: str) -> list[tuple[str, list[str]]]:
    """Split a unified diff into (path, lines) pairs, newest name wins."""
    files: list[tuple[str, list[str]]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current_path is not None:
                files.append((current_path, current_lines))
            current_lines = []
            parts = line.split(" b/", 1)
            current_path = parts[1].strip() if len(parts) == 2 else "unknown"
            continue
        if current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        files.append((current_path, current_lines))
    return files


def _changed_payload(lines: list[str]) -> tuple[list[str], list[str]]:
    added = [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]
    removed = [
        line[1:] for line in lines if line.startswith("-") and not line.startswith("---")
    ]
    return added, removed


def _is_test_path(path: str) -> bool:
    """Decide whether a path is a test file, by segment rather than substring.

    `src/_pytest/raises.py` contains "test" and is production code. A substring
    match called it a test and let a source-only change look covered.
    """
    segments = path.replace("\\", "/").lower().split("/")
    name = segments[-1]
    # typeshed keeps its stub test cases in `@tests/test_cases` (#311).
    if any(segment in {"test", "tests", "testing", "@tests", "test_cases"}
           for segment in segments[:-1]):
        return True
    return name.startswith("test_") or name.endswith(("_test.py", "_tests.py"))


def analyze_diff(
    diff: str, *, no_test_acknowledgement: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Report what a diff actually contains, before a maintainer has to.

    The autosound export carried a trailing-newline change nobody asked for and
    neither agent mentioned. Noise like that is what a reviewer sees first, so
    it is named here rather than discovered upstream.
    """
    files: list[dict[str, Any]] = []
    findings: list[Finding] = []
    for path, lines in _split_file_diffs(diff):
        added, removed = _changed_payload(lines)
        is_binary = any(line.startswith("Binary files ") for line in lines)
        newline_marker = any(r"\ No newline at end of file" in line for line in lines)
        substantive_added = [line for line in added if line.strip()]
        substantive_removed = [line for line in removed if line.strip()]
        whitespace_only = bool(added or removed) and [
            line.strip() for line in added
        ] == [line.strip() for line in removed]
        newline_only = newline_marker and (
            whitespace_only or (not substantive_added and not substantive_removed)
        )
        is_test = _is_test_path(path)
        files.append(
            {
                "path": path,
                "added_lines": len(added),
                "removed_lines": len(removed),
                "binary": is_binary,
                "whitespace_only": whitespace_only,
                "newline_only": newline_only,
                "test": is_test,
            }
        )
        if is_binary:
            findings.append(
                Finding(
                    code="binary-file",
                    blocking=True,
                    detail="the diff changes a binary file",
                    path=path,
                )
            )
        if newline_only:
            findings.append(
                Finding(
                    code="newline-only-change",
                    blocking=True,
                    detail=(
                        "the only change to this file is its trailing newline, "
                        "which is unrelated noise in a bug fix"
                    ),
                    path=path,
                )
            )
        elif whitespace_only:
            findings.append(
                Finding(
                    code="whitespace-only-change",
                    blocking=True,
                    detail="this file changes only in whitespace",
                    path=path,
                )
            )
    if files and not any(entry["test"] for entry in files):
        covered = set((no_test_acknowledgement or {}).get("covered_paths") or [])
        acknowledged = bool(covered) and covered == {entry["path"] for entry in files}
        findings.append(
            Finding(
                code="no-test-change",
                blocking=not acknowledged,
                detail=(
                    "no test file changed. A human read why one would not add "
                    "coverage and recorded it in "
                    f"{NO_TEST_ACKNOWLEDGEMENT_FILENAME}"
                    if acknowledged
                    else "no test file changed, so nothing proves the fix matters "
                    "or stays fixed"
                ),
            )
        )
    if not files:
        findings.append(
            Finding(code="empty-diff", blocking=True, detail="the diff is empty")
        )
    return {
        "files": files,
        "findings": [finding.to_dict() for finding in findings],
        "blocking": any(finding.blocking for finding in findings),
    }


def _verification_rows(run_directory: Path) -> list[dict[str, Any]]:
    path = run_directory / "verification.json"
    if not path.is_file():
        return []
    records = json.loads(path.read_text(encoding="utf-8"))
    return not_reproductions(
        [record for record in records if isinstance(record, dict)]
    )


def _issue_number(issue_record: dict[str, Any] | None) -> int | None:
    reference = (issue_record or {}).get("reference")
    if isinstance(reference, dict) and isinstance(reference.get("number"), int):
        return reference["number"]
    return None


def _duplicate_candidate_counts(
    duplicate_search: dict[str, Any] | None,
    acknowledgement: dict[str, Any] | None,
    issue_number: int | None,
) -> dict[str, Any]:
    strong, weak = partition_duplicates(
        (duplicate_search or {}).get("matches"), issue_number=issue_number
    )
    reviewed = set((acknowledgement or {}).get("reviewed") or [])
    return {
        "strong": [_duplicate_key(row) for row in strong],
        "weak": [_duplicate_key(row) for row in weak],
        "unreviewed": [
            _duplicate_key(row) for row in weak if _duplicate_key(row) not in reviewed
        ],
        "acknowledged_at": (acknowledgement or {}).get("acknowledged_at"),
    }


def _policy_findings(
    run: RunRecord,
    *,
    policy: TargetPolicy,
    issue_number: int | None,
    changed_paths: list[str],
    duplicate_search: dict[str, Any] | None,
    acknowledgement: dict[str, Any] | None = None,
    superseded_numbers: frozenset[int] = frozenset(),
    issue_opened: datetime | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    if policy.stance == "unknown":
        findings.append(
            Finding(
                code="policy-unread",
                blocking=True,
                detail=(
                    f"{policy.name} has no recorded contribution stance. Read the "
                    "project's policy and record it before preparing a submission."
                ),
            )
        )
    if policy.requires_own_words and not policy.own_words_confirmed:
        findings.append(
            Finding(
                code="policy-requires-own-words",
                blocking=True,
                detail=(
                    f"{policy.name} requires issues, comments and pull request "
                    "descriptions to be in the author's own words. The generated "
                    "draft cannot satisfy that. Rewrite the body yourself, then "
                    "set `own_words_confirmed` in the policy file."
                ),
            )
        )
    if policy.stance == "forbidden":
        findings.append(
            Finding(
                code="policy-forbids-ai",
                blocking=True,
                detail=(
                    f"{policy.name} does not accept AI-assisted contributions. "
                    f"See {policy.policy_url}."
                ),
            )
        )
    if policy.stance == "restricted":
        findings.append(
            Finding(
                code="policy-restricted",
                blocking=True,
                detail=(
                    f"{policy.name} accepts AI-assisted work only under conditions "
                    f"a human has to satisfy first. See {policy.policy_url}."
                ),
            )
        )
    if policy.requires_linked_issue and issue_number is None:
        findings.append(
            Finding(
                code="missing-linked-issue",
                blocking=True,
                detail=(
                    f"{policy.name} requires a linked issue and this run has no "
                    "issue number recorded"
                ),
            )
        )
    if policy.requires_maintainer_assignment:
        findings.append(
            Finding(
                code="needs-maintainer-assignment",
                blocking=True,
                detail=(
                    f"{policy.name} closes outside pull requests whose issue a "
                    "maintainer has not assigned. Ask on the issue first."
                ),
            )
        )
    searched = bool(duplicate_search) and duplicate_search.get("success") is True
    if policy.requires_duplicate_search and not searched:
        findings.append(
            Finding(
                code="missing-duplicate-search",
                blocking=True,
                detail=(
                    f"{policy.name} bans duplicate pull requests. Record a search "
                    "of open and closed pull requests and issues in "
                    "duplicate-search.json before preparing a submission."
                ),
            )
        )
    elif policy.requires_duplicate_search and duplicate_search.get("complete") is not True:
        failed = duplicate_search.get("failed_methods") or []
        named = ", ".join(
            f"{entry.get('kind')} {entry.get('method')}" for entry in failed
        )
        findings.append(
            Finding(
                code="degraded-duplicate-search",
                blocking=True,
                detail=(
                    f"{policy.name} bans duplicate pull requests and this search "
                    f"did not complete: {named or 'a method failed'}. An empty "
                    "result from a search that partly failed is not evidence that "
                    "no duplicate exists. Re-run mailman duplicate-search."
                ),
            )
        )
    strong, weak = partition_duplicates(
        (duplicate_search or {}).get("matches"), issue_number=issue_number
    )
    merged = [
        row
        for row in strong
        if duplicate_strength(row) == "merged"
        and row.get("number") not in superseded_numbers
    ]
    if merged:
        findings.append(
            Finding(
                code="already-fixed-upstream",
                blocking=True,
                detail=(
                    "a merged pull request matches this work, so the change is "
                    "already upstream: "
                    + ", ".join(_duplicate_key(row) for row in merged)
                ),
            )
        )
    superseded = [
        row
        for row in strong
        if duplicate_strength(row) == "merged"
        and row.get("number") in superseded_numbers
    ]
    if superseded:
        findings.append(
            Finding(
                code="merged-fix-already-in-base",
                blocking=False,
                detail=(
                    "a merged pull request matches this work, but its merge "
                    "commit is already an ancestor of the base commit and the "
                    "reproduction failed at that same commit, so it is not "
                    "this change: "
                    + ", ".join(_duplicate_key(row) for row in superseded)
                ),
            )
        )
    # A superseded row is neither a rival nor a fix. Leaving it here reported a
    # merged pull request as open and blocked the run the evidence just
    # cleared. See https://github.com/wolfgang-aura/Mailman/issues/46.
    #
    # Imported here rather than at the top: `targeting` reads this module's
    # duplicate helpers, so the dependency only goes one way at import time.
    from mailman.targeting import STALE_ATTEMPT_DAYS, stale_attempt_row

    rivals = [row for row in strong if row not in merged and row not in superseded]
    # The same rule `check-target` applied hours earlier. A run cleared to
    # start against a dormant attempt must not be refused at the filing gate by
    # that same attempt. See targeting.STALE_ATTEMPT_DAYS. A closed unmerged
    # attempt never reaches `rivals` at all, so the whole search is read again.
    stale = stale_prior_attempts(
        (duplicate_search or {}).get("matches"),
        issue_number=issue_number,
        superseded_numbers=superseded_numbers,
        issue_opened=issue_opened,
    )
    open_rivals = [row for row in rivals if row not in stale]
    cleared = (acknowledgement or {}).get("not_duplicates") or {}
    named_clear = [
        row
        for row in open_rivals
        if cleared.get(_duplicate_key(row)) is not None
        and cleared[_duplicate_key(row)] == row.get("head_sha")
        and not row.get("references_issue")
    ]
    if named_clear:
        open_rivals = [row for row in open_rivals if row not in named_clear]
        findings.append(
            Finding(
                code="rival-read-not-duplicate",
                blocking=False,
                detail=(
                    "read by hand and recorded as a different change at its "
                    "current head: "
                    + ", ".join(_duplicate_key(row) for row in named_clear)
                ),
            )
        )
    if stale:
        named = ", ".join(
            f"{_duplicate_key(row)} ({summary['state']}"
            + (
                f", {summary['days_stale']} days since its last activity)"
                if summary["days_stale"] is not None
                else ")"
            )
            for row, summary in ((row, stale_attempt_row(row)) for row in stale)
        )
        findings.append(
            Finding(
                code="stale-prior-attempt",
                blocking=False,
                detail=(
                    f"{len(stale)} earlier pull request(s) stopped claiming this "
                    "issue: open and untouched for at least "
                    f"{STALE_ATTEMPT_DAYS} days, or closed without merging. The "
                    "pull request body must say it supersedes them: " + named
                ),
            )
        )
    if open_rivals:
        findings.append(
            Finding(
                code="possible-duplicate",
                blocking=True,
                detail=(
                    f"{len(open_rivals)} open pull requests or issues name this "
                    "issue or matched the whole query. Read every one before "
                    "filing: "
                    + ", ".join(_duplicate_key(row) for row in open_rivals)
                ),
            )
        )
    if weak:
        reviewed = set((acknowledgement or {}).get("reviewed") or [])
        unreviewed = [row for row in weak if _duplicate_key(row) not in reviewed]
        if unreviewed:
            findings.append(
                Finding(
                    code="unreviewed-duplicate-candidates",
                    blocking=True,
                    detail=(
                        f"{len(unreviewed)} pull requests or issues share wording "
                        "with this work and nothing here can tell overlap from a "
                        "duplicate. Read them and record it with "
                        "`mailman acknowledge-duplicates`: "
                        + ", ".join(_duplicate_key(row) for row in unreviewed)
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    code="duplicate-candidates-reviewed",
                    blocking=False,
                    detail=(
                        f"{len(weak)} weak duplicate candidates were read and "
                        "cleared by hand. See duplicate-acknowledgement.json."
                    ),
                )
            )
    if policy.changelog_directory:
        prefix = policy.changelog_directory.rstrip("/") + "/"
        if not any(path.startswith(prefix) for path in changed_paths):
            findings.append(
                Finding(
                    code="missing-changelog-entry",
                    blocking=True,
                    detail=(
                        f"{policy.name} expects a changelog entry under "
                        f"{policy.changelog_directory}, and the diff adds none"
                    ),
                )
            )
    return findings


def _evidence_findings(
    run: RunRecord, verifications: list[dict[str, Any]]
) -> list[Finding]:
    findings: list[Finding] = []
    if run.status not in (RunStatus.ENGINEERING_COMPLETE, RunStatus.READY_FOR_HUMAN_REVIEW):
        findings.append(
            Finding(
                code="run-not-ready",
                blocking=True,
                detail=(
                    f"run {run.run_id} is {run.status}. Only a run that reached "
                    "READY_FOR_HUMAN_REVIEW carries the evidence a submission needs."
                ),
            )
        )
    passing = [record for record in verifications if record.get("exit_code") == 0]
    if not passing:
        findings.append(
            Finding(
                code="no-passing-verification",
                blocking=True,
                detail="no verification Mailman ran itself has exited zero",
            )
        )
    return findings


def _touched_selection_changed(
    record: dict[str, Any], workspace: Path | None, changed_paths: list[str]
) -> bool:
    """Whether Mailman would now choose other test files than the record ran."""
    if workspace is None or not workspace.is_dir() or "selected" not in record:
        return False
    recorded = [entry.get("path") for entry in record.get("selected") or []]
    cap = record.get("cap") or TOUCHED_TESTS_CAP
    fresh = select_test_files(workspace, changed_paths, cap=cap)
    return [entry["path"] for entry in fresh["selected"]] != recorded


def _touched_deselects_changed(record: dict[str, Any], run_directory: Path) -> bool:
    """Whether Mailman would now deselect other tests than the record did (#161)."""
    ran = [entry.get("path") for entry in record.get("selected") or []]
    return deselects_for(run_directory, ran) != (record.get("deselected") or [])


def _touched_tests_findings(record: dict[str, Any] | None) -> list[Finding]:
    """The touched-tests stage as findings: not run and failed both block."""
    findings: list[Finding] = []
    code, detail = touched_tests_verdict(record)
    if code is not None:
        findings.append(Finding(code=code, blocking=True, detail=detail))
    if record and record.get("capped"):
        findings.append(
            Finding(
                code="touched-tests-capped",
                blocking=False,
                detail=(
                    f"{record.get('candidates')} test files reference the changed "
                    f"modules; only the first {record.get('cap')} ran. The omitted "
                    "files are listed under touched_tests.omitted."
                ),
            )
        )
    deselected = (record or {}).get("deselected") or []
    if deselected:
        findings.append(
            Finding(
                code="touched-tests-deselected",
                blocking=False,
                detail=(
                    "deselected because the run's frozen verification command "
                    "deselects them for the baseline on this host: "
                    + ", ".join(deselected)
                ),
            )
        )
    omitted_reasons = (record or {}).get("omitted_reasons") or {}
    if omitted_reasons and (record or {}).get("reason") != "all-selected-omitted":
        findings.append(
            Finding(
                code="touched-tests-omitted",
                blocking=False,
                detail=(
                    "left out after failing to collect, and the rest ran again: "
                    + "; ".join(f"{path}: {why}" for path, why in omitted_reasons.items())
                ),
            )
        )
    return findings


def _trailer_guidance(policy: TargetPolicy, run: RunRecord) -> str:
    agent = run.primary.agent
    if policy.ai_trailer == "forbidden":
        return (
            f"Do not add any AI co-author trailer. {policy.name} closes pull "
            "requests that carry one."
        )
    if policy.ai_trailer == "required":
        form = (policy.ai_trailer_form or "").format(agent=agent)
        return f"The commit message must carry `{form}`."
    if policy.ai_trailer == "encouraged":
        return (
            f"{policy.name} appreciates crediting the tool. A `Co-authored-by:` "
            f"trailer naming {agent} is welcome but optional."
        )
    return (
        f"{policy.name} says nothing about AI trailers. Leaving them out is safest."
    )


def _agent_credit(role: object) -> str:
    """Name an agent and the model it reported, when one was recorded."""
    agent = getattr(role, "agent", "an agent")
    model = getattr(role, "model", None)
    return f"{agent} (`{model}`)" if model else str(agent)


def _pull_request_markdown(
    run: RunRecord,
    *,
    policy: TargetPolicy,
    issue_number: int | None,
    branch: str,
    title: str,
    verifications: list[dict[str, Any]],
    stale_attempts: list[dict[str, Any]] | None = None,
) -> str:
    if issue_number:
        reference = f"Closes #{issue_number}."
    elif run.issue is not None:
        reference = f"Refs {run.issue}."
    else:
        reference = (
            "There is no upstream issue for this. The defect and how to "
            "reproduce it are described below."
        )
    disclosure = (
        f"This change was drafted with AI assistance ({_agent_credit(run.primary)} "
        f"wrote the patch, {_agent_credit(run.reviewer)} reviewed it), running "
        "under Mailman. The test results above come from the harness executing "
        "the commands itself, not from either agent's account of its own work. "
        "I have read, tested, and take responsibility for every line of it."
    )
    passing = [record for record in verifications if record.get("exit_code") == 0]
    command = " ".join(passing[-1].get("command", [])) if passing else "not recorded"
    hosts = sorted(
        {
            f"{environment['operating_system']} / Python "
            f"{environment['python_version']}"
            for record in verifications
            for environment in [record.get("environment") or {}]
            if environment.get("operating_system") and environment.get("python_version")
        }
    )
    lines = [
        "# Draft pull request",
        "",
        "Nothing here has been sent. A human decides whether any of it is used,",
        "and rewrites the body in their own words before it is.",
        "",
        f"- Target: {policy.name}",
        f"- Policy: {policy.policy_url}",
        f"- Suggested branch: `{branch}`",
        f"- Working title: {title}",
        "",
        f"The standard this draft follows is in docs/pull-request-standard.md.",
        "",
        "## Title",
        "",
        "The working title above is a placeholder. Replace it with one that",
        "states the change and why it matters, never the bug, and that matches",
        "the house style of recently merged pull requests:",
        "",
        "```bash",
        f"gh pr list --repo {policy.name} --state merged --limit 15 --json number,title",
        "```",
        "",
        "## Body draft",
        "",
        reference,
        "",
        "_The problem, in the reporter's terms, with the observable symptom._",
        "",
        "_The cause._",
        "",
        "_The fix, in a sentence or two, with its size. Do not open with an",
        "inventory of what was touched._",
        "",
        "### How this was tested",
        "",
        f"`{command}` passes in a clean checkout at `{run.base_commit[:12]}`.",
        "",
        "_Give the before and after counts, and say that the new test fails",
        "without the source change._",
        "",
    ]
    if hosts:
        lines.extend(
            [
                "State the limits of that verification. Every result recorded here "
                "came from:",
                "",
            ]
        )
        lines.extend(f"- {host}" for host in hosts)
        lines.extend(
            [
                "",
                "Say so in the body. Concealing it costs credibility when CI "
                "disagrees; stating it turns CI into the check you asked for.",
                "",
            ]
        )
    if stale_attempts:
        lines.extend(
            [
                "### Prior attempts this supersedes",
                "",
                "Each of these stopped claiming the issue: it is open and",
                "untouched, or closed without merging. Name every one of them in",
                "the body, link it, and say in one sentence how this change",
                "differs. A body that ignores them reads to a maintainer as a",
                "second contributor racing the first.",
                "",
            ]
        )
        for attempt in stale_attempts:
            days = attempt.get("days_stale")
            since = f", {days} days since its last activity" if days is not None else ""
            title_text = attempt.get("title") or "no recorded title"
            link = attempt.get("url") or f"#{attempt.get('number')}"
            lines.append(
                f"- #{attempt.get('number')} ({attempt.get('state')}{since}): "
                f"{title_text} — {link}"
            )
        lines.append("")
    lines.extend(
        [
            "### An alternative I did not take",
            "",
            "_Name the design you rejected and the trade-off. This is the section "
            "that earns a reply: a maintainer who sees a stated trade-off has "
            "something to answer. Omit it only if there was genuinely no choice._",
            "",
            "### AI disclosure",
            "",
        ]
    )
    if policy.disclosure_required:
        lines.extend([disclosure, ""])
    else:
        lines.extend(
            [
                f"{policy.name} does not require a disclosure line. Including one "
                "anyway costs nothing and matches how the change was made:",
                "",
                f"> {disclosure}",
                "",
            ]
        )
    lines.extend(["### Commit trailers", "", _trailer_guidance(policy, run), ""])
    if policy.checklist:
        lines.extend(["### Project checklist", ""])
        lines.extend(f"- [ ] {item}" for item in policy.checklist)
        lines.append("")
    lines.extend(
        [
            "## Before filing",
            "",
            "- [ ] No pull request already proposes this change",
            "- [ ] The base commit is level with the target's default branch",
            "- [ ] You have read the diff yourself against that branch",
            "- [ ] Opened as a real pull request, not a draft, so CI runs",
            "",
            "Write the final body to its own file, then hand it over with",
            "`mailman handoff`. That prints the whole body immediately above the",
            "`gh` command that posts it and refuses once the file changes, so",
            "nothing goes out under your name that you have not just read.",
            "",
        ]
    )
    return "\n".join(lines)


def _accountability_markdown(run: RunRecord, *, policy: TargetPolicy) -> str:
    return "\n".join(
        [
            "# Before you open this pull request",
            "",
            f"{policy.name} will judge whether a person stands behind this change.",
            "Mailman cannot do that part. Answer these in your own words before",
            "anything is opened. If an answer would have to come from the agent,",
            "the change is not ready to submit.",
            "",
            "1. What was broken, in one sentence, without rereading the issue?",
            "2. Why does the fix work, and what would break if it were wrong?",
            "3. Why this fix rather than the other obvious one?",
            "4. What does the new test assert, and does it fail without the fix?",
            "5. Which part of the change are you least sure about?",
            "",
            "The reviewer report and the verification records are evidence, not",
            "answers. A maintainer asking question 2 expects you, not a transcript.",
            "",
        ]
    )


def _superseded_merges(run_directory: Path) -> frozenset[int]:
    """Merged pull requests already in the tree the run started from."""
    prior_art = run_directory / "prior-art.json"
    payload: Any = None
    if prior_art.is_file():
        try:
            payload = json.loads(
                prior_art.read_text(encoding="utf-8", errors="replace")
            )
        except json.JSONDecodeError:
            payload = None
    attempts = payload.get("attempts") if isinstance(payload, dict) else None
    if not isinstance(attempts, list):
        attempts = []
    reproduction_path = run_directory / REPRODUCTION_FILENAME
    try:
        reproduction = json.loads(
            reproduction_path.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(reproduction, dict):
        return frozenset()
    def landed(*commits: object) -> bool:
        # GitHub's merge sha is missing from a rewritten history; the branch
        # head commit, when it is an ancestor, is the same change landed.
        # beets pr#10 (2011) blocked a reproduced 2026 bug. Mailman #298.
        return any(
            isinstance(commit, str)
            and merge_is_in_base(run_directory, {"merge_commit": commit}, reproduction)
            for commit in commits
        )

    superseded = {
        attempt["number"]
        for attempt in attempts
        if isinstance(attempt, dict)
        and isinstance(attempt.get("number"), int)
        and attempt.get("outcome") == "merged"
        and landed(attempt.get("merge_commit"), attempt.get("head_sha"))
    }
    # A merged row only the duplicate search found carries no merge commit.
    # Its squash commit, subject ending "(#N)", is the one to test. cloud-init
    # pr#744 blocked a finished run this way. Mailman #289.
    search = load_duplicate_search(run_directory) or {}
    for row in search.get("matches") or []:
        number = row.get("number") if isinstance(row, dict) else None
        if (
            not isinstance(number, int)
            or number in superseded
            or str(row.get("state") or "").lower() != "merged"
        ):
            continue
        if landed(row.get("merge_commit")) or landed(
            _squash_commit(run_directory, number)
        ) or landed(row.get("head_sha")):
            superseded.add(number)
    return frozenset(superseded)


def _squash_commit(run_directory: Path, number: int) -> str | None:
    """The base-history commit that landed pull request `number`, if any.

    A squash subject ends with `(#N)`; a merge commit starts with `Merge pull
    request #N from `. spack pr#208 was merged the second way in 2015, and
    GitHub's recorded sha is not in the rewritten history. Mailman #291.
    """
    workspace = run_directory / WORKSPACE_DIRECTORY
    if not (workspace / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            [
                "git", "-C", str(workspace), "log", "--format=%H %s",
                "--fixed-strings", f"--grep=(#{number})",
                f"--grep=Merge pull request #{number} from ", "HEAD",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        if subject.rstrip().endswith(f"(#{number})") or subject.startswith(
            f"Merge pull request #{number} from "
        ):
            return sha
    return None


def prepare_submission(
    run: RunRecord,
    run_directory: Path,
    *,
    diff: str,
    policy: TargetPolicy,
    destination: Path,
    branch: str,
    title: str,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Assemble everything a human needs before opening a pull request.

    This never contacts the upstream repository. It reports whether the change
    and the run's evidence meet the target's own written rules, and refuses to
    call a submission ready when they do not.

    The one thing it does run is the touched-tests stage: every test file that
    imports or names a module the diff changed, in the run's own environment.
    That stage runs once per export and its record travels with the
    submission, so `handoff-check` can refuse a filing that skipped it.
    """
    issue_record = load_issue_record(run_directory)
    issue_number = _issue_number(issue_record)
    no_test_path = run_directory / NO_TEST_ACKNOWLEDGEMENT_FILENAME
    no_test_acknowledgement = (
        json.loads(no_test_path.read_text(encoding="utf-8"))
        if no_test_path.is_file()
        else None
    )
    hygiene = analyze_diff(diff, no_test_acknowledgement=no_test_acknowledgement)
    changed_paths = [entry["path"] for entry in hygiene["files"]]
    verifications = _verification_rows(run_directory)
    duplicate_search_path = run_directory / DUPLICATE_SEARCH_FILENAME
    duplicate_search = (
        json.loads(duplicate_search_path.read_text(encoding="utf-8"))
        if duplicate_search_path.is_file()
        else None
    )

    acknowledgement_path = run_directory / DUPLICATE_ACKNOWLEDGEMENT_FILENAME
    acknowledgement = (
        json.loads(acknowledgement_path.read_text(encoding="utf-8"))
        if acknowledgement_path.is_file()
        else None
    )

    # A merged pull request whose merge commit is already an ancestor of the
    # base commit, on a run whose reproduction failed at that same commit, is
    # not this change. `check-target` reaches the same conclusion from the same
    # two records. See https://github.com/wolfgang-aura/Mailman/issues/46.
    superseded_numbers = _superseded_merges(run_directory)

    findings = [Finding(**entry) for entry in hygiene["findings"]]
    findings.extend(
        _policy_findings(
            run,
            policy=policy,
            issue_number=issue_number,
            changed_paths=changed_paths,
            duplicate_search=duplicate_search,
            acknowledgement=acknowledgement,
            superseded_numbers=superseded_numbers,
            issue_opened=issue_opened_at(run_directory),
        )
    )
    findings.extend(_evidence_findings(run, verifications))
    diff_digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    touched_tests = load_touched_tests(run_directory)
    touched_workspace = resolve_workspace(run_directory, workspace)
    # A record for another diff, or one that never got to run, is retried; a
    # failure is not, because the diff it failed on is the one being filed.
    # Nor is a record whose file selection Mailman would now make differently:
    # nilearn#6607 kept failing on gallery scripts after #145 stopped
    # selecting them, because the stored failure was for the same diff.
    if (
        touched_tests is None
        or touched_tests.get("diff_sha256") != diff_digest
        or not touched_tests.get("ran")
        # A failure recorded by other touched-tests code (#216).
        or (
            touched_tests.get("exit_code") not in (0, None)
            and touched_tests.get("code_version") != TOUCHED_TESTS_CODE_VERSION
        )
        or _touched_selection_changed(touched_tests, touched_workspace, changed_paths)
        or _touched_deselects_changed(touched_tests, run_directory)
        # A failure recorded before failures were compared with the base
        # commit (#180), or past the old 50-node limit that skipped the
        # comparison (#202), is run again so it gets that comparison.
        or (
            touched_tests.get("exit_code") == 1
            and bool(run.base_commit)
            and (
                "baseline" not in touched_tests
                or (
                    touched_tests.get("baseline") is None
                    and touched_tests.get("runner") == "pytest"
                    and (touched_tests.get("failed") or 0)
                    + (touched_tests.get("errors") or 0)
                    > BASELINE_NODE_LIMIT
                )
            )
        )
    ):
        touched_tests = run_touched_tests(
            run_directory,
            diff=diff,
            changed_paths=changed_paths,
            workspace=touched_workspace,
            base_commit=run.base_commit,
        )
    findings.extend(_touched_tests_findings(touched_tests))
    # The target's own CI checks on changed files, so CI is not the first to
    # run them (#137, #120).
    from mailman.target_checks import (
        load_lint_acknowledgement,
        run_lint,
        run_offline_audit,
    )

    check_workspace = resolve_workspace(run_directory, workspace)
    offline_audit, audit_findings = run_offline_audit(
        run_directory, workspace=check_workspace, changed_paths=changed_paths
    )
    findings.extend(Finding(**entry) for entry in audit_findings)
    lint, lint_findings = run_lint(
        run_directory,
        workspace=check_workspace,
        changed_paths=changed_paths,
        acknowledged=load_lint_acknowledgement(run_directory, diff_digest),
        base_commit=run.base_commit,
    )
    findings.extend(Finding(**entry) for entry in lint_findings)
    from mailman.targeting import stale_attempt_row

    stale_rows = [
        stale_attempt_row(row)
        for row in stale_prior_attempts(
            (duplicate_search or {}).get("matches"),
            issue_number=issue_number,
            superseded_numbers=superseded_numbers,
            issue_opened=issue_opened_at(run_directory),
        )
    ]
    from mailman.completion import check_authorship
    try:
        check_authorship(run_directory)
    except (OSError, ValueError) as error:
        findings.append(Finding(code="author-identity", detail=str(error), blocking=True))
    blocking = [finding for finding in findings if finding.blocking]

    destination_path = destination.resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    (destination_path / "pull-request.md").write_text(
        _pull_request_markdown(
            run,
            policy=policy,
            issue_number=issue_number,
            branch=branch,
            title=title,
            verifications=verifications,
            stale_attempts=stale_rows,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (destination_path / "accountability.md").write_text(
        _accountability_markdown(run, policy=policy),
        encoding="utf-8",
        newline="\n",
    )
    record = {
        "schema_version": SUBMISSION_SCHEMA_VERSION,
        "diff_sha256": diff_digest,
        "run_id": run.run_id,
        "prepared_at": datetime.now(UTC).isoformat(),
        "target": policy.name,
        "policy_url": policy.policy_url,
        "policy_stance": policy.stance,
        "policy_read_on": policy.policy_read_on,
        "branch": branch,
        "title": title,
        "issue_number": issue_number,
        "changed_files": changed_paths,
        "hygiene": hygiene,
        "findings": [finding.to_dict() for finding in findings],
        "ready": not blocking,
        "blocking_codes": sorted({finding.code for finding in blocking}),
        "duplicate_search_recorded": bool(duplicate_search)
        and duplicate_search.get("success") is True,
        "duplicate_candidates": _duplicate_candidate_counts(
            duplicate_search, acknowledgement, issue_number
        ),
        "touched_tests": touched_tests,
        "offline_audit": offline_audit,
        "lint": lint,
        "files": ["pull-request.md", "accountability.md", "submission.json"],
    }
    (destination_path / "submission.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


DUPLICATE_SEARCH_FILENAME = "duplicate-search.json"
DUPLICATE_ACKNOWLEDGEMENT_FILENAME = "duplicate-acknowledgement.json"
NO_TEST_ACKNOWLEDGEMENT_FILENAME = "no-test-acknowledgement.json"

def load_duplicate_search(run_directory: Path) -> dict[str, Any] | None:
    """The run's recorded duplicate search, when it has a readable one."""
    path = run_directory / DUPLICATE_SEARCH_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


# A row GitHub's own index returned is worth more than a locally matched one.
# Both `gh search` and `gh <kind> list --search` AND every term server-side, so
# a hit from either means the whole query matched, not one common word.
_INDEX_METHODS = frozenset({"search", "list"})


def duplicate_is_related(row: dict[str, Any]) -> bool:
    """Say whether a row is about this issue, ignoring whether it is still open.

    Relevance and blocking are different questions. A closed attempt is as
    relevant as an open one, which is the whole point of reading prior art, but
    only an open or merged one is a reason to hold the run back.
    """
    reasons = [str(reason) for reason in row.get("matched_by") or []]
    if row.get("references_issue") or any(
        reason.startswith("#") for reason in reasons
    ):
        return True
    if row.get("compact_match"):
        # Every distinctive title term was read in this row's own text, and
        # one of them in its title. That is the listing's full-match standard
        # on a query short enough for a rival to meet. The title-length query
        # scored marimo#10915 as a 7-of-11 partial. #201.
        return True
    # An older record has no `methods`, and its `matched_by` held the method
    # name for index hits. Read both so a run recorded before #31 still judges.
    methods = [str(method) for method in row.get("methods") or []] or reasons
    matched = row.get("matched_terms") or []
    term_count = row.get("term_count") or 0
    if any(method in _INDEX_METHODS for method in methods):
        # The index matches text Mailman never sees. When our own listing read
        # this row's title and body and found only part of the query, that is
        # the better evidence: anndata#2596 shared "arrayview" with a query
        # for "arrayview dataframe" and blocked #2348 with no override. #196.
        # A listing that read the row and matched nothing emits no row of its
        # own, so it marks the index row instead: toga#4279 matched "startup
        # exception" only in text the listing never saw and "claimed" #3628.
        read_locally = "listing" in methods or row.get("listing_read")
        partial = read_locally and term_count and len(matched) < term_count
        return not partial
    if not (term_count and len(matched) >= term_count):
        return False
    # The compact rule's anchor (#201): a full match needs one term in the
    # title. xarray#11633 moved the backend tests, carried every term of a
    # four-word query in its body and blocked #10639 with no override. #219.
    title = str(row.get("title") or "").lower()
    return not title or any(str(term).lower() in title for term in matched)


def related_duplicates(
    matches: list[dict[str, Any]] | None,
    *,
    issue_number: int | None = None,
) -> list[dict[str, Any]]:
    """Every recorded row that is about this issue, whatever its state."""
    return [
        row
        for row in matches or []
        if isinstance(row, dict)
        and duplicate_is_related(row)
        and not (
            issue_number is not None
            and not row.get("pull_request")
            and row.get("number") == issue_number
        )
    ]


def duplicate_strength(row: dict[str, Any]) -> str:
    """Say whether a matched row looks like the same change or like noise.

    Two signals stand on their own: the row names this run's issue, or an
    index-backed search returned it. Everything else is a local listing hit,
    and on kernc/backtesting.py twenty-one of twenty-two of those were topic
    overlap on the word "price". Calling those duplicates makes the gate
    useless; ignoring them is how encode/starlette #30 nearly shipped a fifth
    copy of an open pull request. So they are neither: they are weak, and a
    human has to read them.
    """
    state = str(row.get("state") or "").lower()
    if state == "merged" and duplicate_is_related(row):
        # The change is already upstream. Nothing about this run is worth
        # filing, whatever else the row matched on.
        return "merged"
    if state == "closed":
        # A closed attempt is prior art, not a rival in flight. `prior-art`
        # reads it into both prompts; the gate only has to make a human look.
        return "weak"
    if not row.get("pull_request"):
        # An open issue that names this one is a thread to read, not a patch
        # racing ours. skfolio#312, a tracking issue, blocked #307 as a rival.
        return "weak"
    return "strong" if duplicate_is_related(row) else "weak"


def _duplicate_key(row: dict[str, Any]) -> str:
    kind = "pr" if row.get("pull_request") else "issue"
    return f"{kind}#{row.get('number')}"


def partition_duplicates(
    matches: list[dict[str, Any]] | None,
    *,
    issue_number: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split recorded matches into what blocks and what a human must read."""
    strong: list[dict[str, Any]] = []
    weak: list[dict[str, Any]] = []
    for row in matches or []:
        if not isinstance(row, dict):
            continue
        if (
            issue_number is not None
            and not row.get("pull_request")
            and row.get("number") == issue_number
        ):
            continue
        strength = duplicate_strength(row)
        if strength in ("strong", "merged"):
            strong.append(row)
        else:
            weak.append(row)
    return strong, weak


def stale_prior_attempts(
    matches: list[dict[str, Any]] | None,
    *,
    issue_number: int | None = None,
    superseded_numbers: frozenset[int] = frozenset(),
    issue_opened: datetime | None = None,
) -> list[dict[str, Any]]:
    """Every dormant prior attempt on this issue, open or closed unmerged.

    `duplicate_strength` calls a closed pull request weak, because a closed
    attempt is not a rival in flight. It is still a prior attempt the body has
    to supersede: tqdm#1812 carried two closed unmerged ones and the prepared
    submission named neither. Read both partitions here and let the staleness
    rule, not the strength, decide.
    """
    from mailman.targeting import is_stale_attempt

    strong, weak = partition_duplicates(matches, issue_number=issue_number)
    return [
        row
        for row in (*strong, *weak)
        if row.get("pull_request")
        and row.get("number") not in superseded_numbers
        and duplicate_is_related(row)
        and not predates_issue(row, issue_opened)
        and is_stale_attempt(row)
    ]


def record_duplicate_acknowledgement(
    run_directory: Path, *, note: str, not_duplicates: list[str] | None = None
) -> dict[str, Any]:
    """Record that a human read this run's weak duplicate candidates.

    The record pins the exact rows that were read. A later search that turns up
    anything new is not covered by it, so this cannot become a standing waiver.
    """
    if not note.strip():
        raise ValueError("an acknowledgement needs a note saying what was read")
    search_path = run_directory / DUPLICATE_SEARCH_FILENAME
    if not search_path.is_file():
        raise ValueError(
            "no duplicate search to acknowledge. Run `mailman duplicate-search` first."
        )
    search = json.loads(search_path.read_text(encoding="utf-8"))
    issue_number = _issue_number(load_issue_record(run_directory))
    strong, weak = partition_duplicates(
        search.get("matches"), issue_number=issue_number
    )
    # A strong row that names no issue matched on wording alone. Once read,
    # it can be cleared by name, pinned to its head so a push re-blocks
    # (pandas-stubs#1900, #306). One that names the issue never can.
    cleared: dict[str, str | None] = {}
    by_key = {_duplicate_key(row): row for row in strong}
    for key in not_duplicates or []:
        row = by_key.get(key)
        if row is None:
            raise ValueError(f"{key} is not a strong match in this run's search")
        if duplicate_strength(row) == "merged" or row.get("references_issue") or any(
            str(reason).startswith("#") for reason in row.get("matched_by") or []
        ):
            raise ValueError(
                f"{key} names the issue or is merged, so no acknowledgement clears it"
            )
        if not row.get("head_sha"):
            # Only `gh search prs` found it, and that carries no head. A None
            # pin matched None on every later check, so a push never re-blocked.
            raise ValueError(
                f"{key} has no recorded head to pin; rerun `mailman duplicate-search` "
                "so its head is read"
            )
        cleared[key] = row.get("head_sha")
    record = {
        "schema_version": 1,
        "acknowledged_at": datetime.now(UTC).isoformat(),
        "note": note.strip(),
        "searched_at": search.get("searched_at"),
        "reviewed": sorted(_duplicate_key(row) for row in weak),
        "strong_at_acknowledgement": sorted(
            key for key in by_key if key not in cleared
        ),
        "not_duplicates": cleared,
    }
    _write_json(run_directory / DUPLICATE_ACKNOWLEDGEMENT_FILENAME, record)
    return record

# `gh pr list --search` is repo-scoped and works where the global `gh search`
# index refuses a repository, which it does for encode/starlette.
#
# The field sets differ by subcommand, so they are written out per subcommand
# rather than shared: `gh pr list` has no author association and `gh issue
# list` has neither that nor `isDraft`, and asking either for a field it does
# not know makes it refuse the whole call. `updatedAt` is what decides whether
# an open attempt still claims the issue; see targeting.STALE_ATTEMPT_DAYS.
_SEARCH_FIELDS = {
    "pr": "number,title,body,state,url,createdAt,updatedAt,isDraft,headRefOid,mergeCommit",
    "issue": "number,title,body,state,url,createdAt,updatedAt",
}
# `gh search prs` and `gh search issues` are the only two that carry the author
# association, which is what keeps a maintainer's own dormant branch blocking.
_INDEX_FIELDS = {
    "pr": "number,title,state,url,createdAt,updatedAt,isDraft,authorAssociation",
    "issue": "number,title,state,url,createdAt,updatedAt,authorAssociation",
}
# The unfiltered listing needs the text a local match reads. An issue has no
# head ref, and asking for one makes `gh issue list` refuse the whole call.
_LISTING_FIELDS = {
    "pr": "number,title,state,url,createdAt,updatedAt,isDraft,body,headRefName,headRefOid",
    "issue": "number,title,state,url,createdAt,updatedAt,body",
}
_MINIMUM_TERM_LENGTH = 4


def _match_rows(
    payload: object,
    *,
    pull_request: bool,
    reasons: list[str] | None = None,
    method: str = "search",
    matched_terms: list[str] | None = None,
    term_count: int = 0,
    references_issue: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return rows
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "number": entry.get("number"),
                "title": entry.get("title"),
                "state": entry.get("state"),
                "url": entry.get("url"),
                "created_at": entry.get("createdAt"),
                # What the staleness rule reads. `updatedAt` is the last
                # activity; the association says whether a dormant branch
                # belongs to somebody who speaks for the project.
                "updated_at": entry.get("updatedAt"),
                "is_draft": entry.get("isDraft"),
                "author_association": entry.get("authorAssociation"),
                # What `_superseded_merges` tests against base. Mailman #298.
                "merge_commit": (entry.get("mergeCommit") or {}).get("oid")
                if isinstance(entry.get("mergeCommit"), dict)
                else None,
                "head_sha": entry.get("headRefOid"),
                "pull_request": pull_request,
                "matched_by": list(reasons or ["search"]),
                # How a row was found decides what it is worth. GitHub's index
                # ANDs every query term. The local listing matcher does not.
                "methods": [method],
                "matched_terms": list(matched_terms or []),
                "term_count": term_count,
                "references_issue": references_issue,
            }
        )
    return rows


def _query_terms(query: str) -> list[str]:
    """The words worth matching on their own, lowercased."""
    return [
        word.lower()
        for word in re.findall(r"[A-Za-z0-9_]+", query)
        if len(word) >= _MINIMUM_TERM_LENGTH
    ]


# Words a bug title carries whatever the bug is. A compact query built from
# them matches every other report in the repository.
_COMPACT_STOPWORDS = frozenset(
    """
    about above after again against also always another any are aren been
    before being below between both but called calls cannot case cases cause
    causes caused could does doesn didn doing done during each either else even
    ever every fail fails failed failing failure first from further get gets had
    has hasn have having here how incorrect incorrectly instead into isn its
    itself just like make makes many might more most much must need needs never
    not now once only other ought our over own properly same should shouldn
    since some still such than that the their them then there these they this
    those through too trying under unexpected unexpectedly until use used uses
    using very was wasn way were weren what when where whether which while who
    whom why will with within without won work works working would wrong your
    add adds added allow allows bug bugs error errors exception fix fixes fixed
    issue issues missing new option options raise raises raised result results
    return returns returned support supports supported update updates updated
    value values behavior behaviour handle handles handled correctly broken
    """.split()
)
#: How many terms a compact query keeps. GitHub ANDs them, so every extra one
#: is another word a rival pull request has to happen to use.
COMPACT_TERM_LIMIT = 3
#: A term is distinctive when at most this share of the listed open items
#: contains it, and never fewer than `COMPACT_TERM_FLOOR` of them, because the
#: rival itself is one. On 293 stored prescreens a 10% share added four
#: blocks, three of them real rivals, and found 4 of 9 known rivals; 20% found
#: one more rival and added one more unrelated block.
COMPACT_TERM_SHARE = 0.1
COMPACT_TERM_FLOOR = 2


def _compact_stem(word: str) -> str:
    """Drop a plural `s`, so `getters` in a title matches `getter` in a body."""
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def compact_terms(
    title: str,
    corpus: Sequence[object],
    *,
    repository: str = "",
) -> list[str]:
    """The two or three title terms a rival pull request would also use.

    The duplicate search sends the whole title, and GitHub ANDs every word.
    marimo#9974's eleven-word title found nothing, while the three words
    `setter getter mo.state` all appear in open rival #10915. This keeps the title's
    code spans and content words, drops the ones common in the repository's
    own open items (`corpus`, the listing the search already read), and keeps
    the rarest few. Fewer than two distinctive terms is no query at all: one
    word decides nothing, and a common one blocks at random.
    """
    candidates: list[str] = []
    for span in re.findall(r"`([^`\n]+)`", title or ""):
        span = span.strip().strip("/").removesuffix("()")
        if re.fullmatch(r"[A-Za-z_][\w.]*", span) and len(span) >= 3:
            candidates.append(span.lower())
        else:
            candidates.extend(
                word.lower() for word in re.findall(r"[A-Za-z_]\w{3,}", span)
            )
    prose = re.sub(r"`[^`\n]+`", " ", title or "")
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_']*", prose):
        word = word.lower().split("'")[0]
        if len(word) >= _MINIMUM_TERM_LENGTH and word not in _COMPACT_STOPWORDS:
            candidates.append(_compact_stem(word))
    # The project's own name is in half its pull requests.
    project = set(re.findall(r"[a-z0-9]+", repository.lower()))
    candidates = [
        term
        for term in dict.fromkeys(candidates)
        if term not in project and term not in _COMPACT_STOPWORDS
    ]
    texts = [
        f"{entry.get('title') or ''} {entry.get('body') or ''}".lower()
        for entry in corpus
        if isinstance(entry, dict)
    ]
    if not candidates or not texts:
        return []
    limit = max(COMPACT_TERM_FLOOR, COMPACT_TERM_SHARE * len(texts))
    frequency = {term: sum(term in text for text in texts) for term in candidates}
    distinctive = sorted(
        (term for term in candidates if frequency[term] <= limit),
        key=lambda term: (frequency[term], candidates.index(term)),
    )[:COMPACT_TERM_LIMIT]
    return distinctive if len(distinctive) >= 2 else []


def _compact_matches(
    payload: object, terms: Sequence[str], *, pull_request: bool
) -> list[dict[str, Any]]:
    """Rows whose own text carries every compact term, one of them in the title.

    GitHub's index also matches comments Mailman never reads, so an index hit
    alone is not the evidence. The title anchor is what kept the stored
    prescreens from blocking on long template bodies: without it a 10% share
    added eleven blocks, most of them a dependency bump's changelog.
    """
    if not terms or not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").lower()
        text = " ".join(
            str(entry.get(field) or "") for field in ("title", "body", "headRefName")
        ).lower()
        matched = [term for term in terms if term in text]
        if len(matched) < len(terms) or not any(term in title for term in terms):
            continue
        for row in _match_rows(
            [entry],
            pull_request=pull_request,
            method="compact",
            reasons=["compact"],
            matched_terms=matched,
            term_count=len(terms),
        ):
            row["compact_match"] = True
            rows.append(row)
    return rows


def _references_issue(text: str, issue_number: int | None) -> bool:
    """Does this text cite the issue, as a reference and not as a bare number?

    GitHub's search tokenises `#6327` to `6327`, so the narrow query for
    pretix#6327 returned a 2018 pull request whose comment log carried
    `django.po:6327:`. A line number, a byte count or a version is not a
    citation. Only the forms people write for one are: `#N`, `GH-N`,
    `issues/N`, `pull/N`, `issue N`.
    """
    if issue_number is None or not text:
        return False
    return bool(
        re.search(
            rf"(?:#|GH-|issues/|pull/|issue\s+|pull\s+request\s+){issue_number}(?![0-9])",
            text,
            re.IGNORECASE,
        )
    )


def _local_matches(
    payload: object,
    *,
    pull_request: bool,
    query: str,
    issue_number: int | None,
    minimum_terms: int = 1,
) -> list[dict[str, Any]]:
    """Match an unfiltered listing locally, because GitHub's search may not.

    `gh pr list --search` returns nothing on encode/starlette even for a single
    token that four open pull request titles contain. Reading the listing and
    matching here is the only method that found them.
    """
    if not isinstance(payload, list):
        return []
    terms = _query_terms(query)
    rows: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if (
            issue_number is not None
            and not pull_request
            and entry.get("number") == issue_number
        ):
            # The run's own issue is not a duplicate of itself. Issue #31.
            continue
        haystack = " ".join(
            str(entry.get(field) or "")
            for field in ("title", "body", "headRefName")
        ).lower()
        matched = [term for term in terms if term in haystack]
        references_issue = issue_number is not None and bool(
            re.search(
                rf"\b{issue_number}\b", haystack
            )
        )
        if not references_issue and len(matched) < max(minimum_terms, 1):
            continue
        reasons = list(matched)
        if references_issue:
            reasons.append(f"#{issue_number}")
        rows.extend(
            _match_rows(
                [entry],
                pull_request=pull_request,
                reasons=reasons,
                method="listing",
                matched_terms=matched,
                term_count=len(terms),
                references_issue=references_issue,
            )
        )
    return rows


def _mark_listing_read(
    record: dict[str, Any],
    listing: object,
    *,
    rows: list[dict[str, Any]],
    pull_request: bool,
    term_count: int,
) -> None:
    """Mark index rows whose own text the listing read without a match.

    `_local_matches` drops an entry that matches no query term, so without
    this an index hit on comment text alone stands as related. #243.
    """
    if not isinstance(listing, list) or not term_count:
        return
    read = {
        entry.get("number") for entry in listing if isinstance(entry, dict)
    }
    matched = {row.get("number") for row in rows}
    for row in record.get("matches") or []:
        if (
            row.get("pull_request") == pull_request
            and row.get("number") in read
            and row.get("number") not in matched
        ):
            row["listing_read"] = True
            row["term_count"] = max(row.get("term_count") or 0, term_count)


# Each read is one core API call; screen batches already strain that limit.
UNLISTED_ROW_READS = 10


def _read_unlisted_rows(
    record: dict[str, Any],
    run_directory: Path,
    *,
    slug: str,
    executable: str,
    query: str,
    issue_number: int | None,
    timeout_seconds: float,
) -> None:
    """Read the own text of open index hits the listing never reached (#292).

    spack pr#48947, "Override package directives", matched "resource
    directive package hash" somewhere GitHub indexes, and spack has more open
    pull requests than the listing reads. It stood as a rival nothing could
    clear. Its title and body are read and judged as the listing would have.
    A failed read leaves the row standing.
    """
    term_count = len(_query_terms(query))
    if not term_count:
        return
    candidates = [
        row
        for row in record.get("matches") or []
        if row.get("pull_request")
        and str(row.get("state") or "").lower() == "open"
        and not row.get("listing_read")
        and not row.get("references_issue")
        and not row.get("compact_match")
        and set(row.get("methods") or []) <= _INDEX_METHODS
    ]
    for row in candidates[:UNLISTED_ROW_READS]:
        result = execute(
            [
                executable, "pr", "view", str(row.get("number")), "--repo", slug,
                "--json", "number,title,body,headRefName,headRefOid",
            ],
            working_directory=run_directory,
            timeout_seconds=timeout_seconds,
        )
        record.setdefault("commands", []).append(
            {"method": "unlisted-read", **result.to_dict()}
        )
        if result.timed_out or result.exit_code != 0:
            continue
        try:
            entry = json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        listing = [entry]
        rows = _local_matches(
            listing, pull_request=True, query=query, issue_number=issue_number
        )
        for matched in rows:
            _add_match(record, matched, issue_number=issue_number)
        _mark_listing_read(
            record, listing, rows=rows, pull_request=True, term_count=term_count
        )


def _add_match(
    record: dict[str, Any], row: dict[str, Any], *, issue_number: int | None
) -> None:
    """Record one matched row, or fold it into the row already found."""
    if (
        issue_number is not None
        and not row["pull_request"]
        and row["number"] == issue_number
    ):
        # An index search returns the run's own issue. Issue #31.
        return
    existing = next(
        (
            candidate
            for candidate in record["matches"]
            if candidate["number"] == row["number"]
            and candidate["pull_request"] == row["pull_request"]
        ),
        None,
    )
    if existing is None:
        record["matches"].append(row)
        return
    # The same row found twice is stronger, not redundant. Keep
    # every method and reason so the strength reads correctly.
    for field in ("methods", "matched_by", "matched_terms"):
        merged = list(existing.get(field) or [])
        merged.extend(
            item for item in row.get(field) or [] if item not in merged
        )
        existing[field] = merged
    # Only `gh search` returns an author association, and only the
    # pull request methods return `isDraft`, so the row that got
    # here first may be missing what the staleness rule needs.
    for field_name in (
        "updated_at", "is_draft", "author_association", "merge_commit", "head_sha"
    ):
        if existing.get(field_name) is None:
            existing[field_name] = row.get(field_name)
    existing["term_count"] = max(
        existing.get("term_count") or 0, row.get("term_count") or 0
    )
    existing["references_issue"] = bool(
        existing.get("references_issue") or row.get("references_issue")
    )
    existing["compact_match"] = bool(
        existing.get("compact_match") or row.get("compact_match")
    )


def _compact_search(
    record: dict[str, Any],
    run_directory: Path,
    *,
    title: str,
    listed: dict[str, list[Any]],
    slug: str,
    executable: str,
    issue_number: int | None,
    timeout_seconds: float,
    limit: int,
    listing_limit: int,
) -> None:
    """Search open pull requests with the title's few distinctive terms.

    The listing has already been read, so the terms are judged against it and
    matched in it for free. The one index call is only spent when the listing
    stopped at its limit and an open rival could sit past it. See
    https://github.com/wolfgang-aura/Mailman/issues/201.
    """
    corpus = [
        *listed.get("pr", []),
        *[
            entry
            for entry in listed.get("issue", [])
            if not (isinstance(entry, dict) and entry.get("number") == issue_number)
        ],
    ]
    terms = compact_terms(title, corpus, repository=slug)
    record["compact_terms"] = terms
    if not terms:
        return
    rows = _compact_matches(listed.get("pr"), terms, pull_request=True)
    if len(listed.get("pr") or []) >= listing_limit:
        command = [
            executable,
            "search",
            "prs",
            *terms,
            "--repo",
            slug,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            _INDEX_FIELDS["pr"] + ",body",
        ]
        result: CommandResult = execute(
            command, working_directory=run_directory, timeout_seconds=timeout_seconds
        )
        record["commands"].append({"method": "compact", **result.to_dict()})
        payload: object = None
        if not result.timed_out and result.exit_code == 0:
            try:
                payload = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            # The listing already decided whether the search is complete; a
            # failed extra query is recorded, not a reason to distrust it.
            record["failed_methods"].append(
                {
                    "kind": "pr",
                    "method": "compact",
                    "detail": "timed out"
                    if result.timed_out
                    else next(
                        iter((result.stderr or "").strip().splitlines()),
                        "unreadable output",
                    ),
                }
            )
        else:
            rows.extend(_compact_matches(payload, terms, pull_request=True))
    for row in rows:
        _add_match(record, row, issue_number=issue_number)


def record_duplicate_search(
    run_directory: Path,
    *,
    repository: str,
    query: str,
    issue_number: int | None = None,
    executable: str | None = None,
    timeout_seconds: float = 60,
    limit: int = 30,
    listing_limit: int = 100,
    symbols: Sequence[str] = (),
    issue_symbols: Sequence[str] = (),
    title: str | None = None,
) -> dict[str, Any]:
    """Search a target's pull requests and issues, and record what came back.

    Starlette treats a duplicate pull request as a ban-level offence, and no
    project welcomes one. The search is cheap; forgetting it is not. The record
    is evidence that it happened, with the query that was used, so a human can
    judge whether it was a real search or a token one.
    """
    if not query.strip():
        raise ValueError("a duplicate search needs a query")
    if title is None:
        # The run stage and the hunt's pre-filing refresh pass no title; the
        # captured issue has it. Without it they skip the compact query. #201.
        captured = load_issue_record(run_directory) or {}
        title = str(captured.get("title") or "")
    slug = repository.removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        slug = slug.removeprefix(prefix)
    command_executable = executable or resolve_tool(run_directory, "gh")
    searched_at = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {
        "schema_version": 1,
        "searched_at": searched_at,
        "repository": slug,
        "query": query,
        "issue_number": issue_number,
        "success": False,
        "complete": False,
        "symbols": list(symbols),
        "issue_symbols": list(issue_symbols),
        "decided_by": None,
        "compact_terms": [],
        "matches": [],
        "methods": {},
        "failed_methods": [],
        "commands": [],
    }
    listed: dict[str, list[Any]] = {}
    for kind in ("pr", "issue"):
        # Two searches disagree in useful ways. The global index finds a pull
        # request whose body says "Fixes #14324"; the repo-scoped list works on
        # repositories the global index refuses, as it does for encode/starlette.
        # Narrow first. The cheapest query that can settle this is the issue
        # number and the symbols the change touches; the hundred-item listing
        # is the expensive one. Ordering them this way is what stops discovery
        # paying full price on a target it is about to reject.
        # https://github.com/wolfgang-aura/Mailman/issues/68
        issue_term = [f"#{issue_number}"] if issue_number is not None else []
        narrow_terms = [
            *issue_term,
            *[symbol for symbol in symbols if symbol.strip()],
        ]
        # GitHub joins search terms with AND, so the symbols read out of the
        # issue body each get their own query rather than lengthening this
        # one: on llama_index#22639 a seven-term query found nothing while
        # `_handle_upserts` alone found both open rivals. Pull requests only;
        # a rival is a pull request, and each query is a search API call.
        # https://github.com/wolfgang-aura/Mailman/issues/96
        narrow_queries = [narrow_terms] if narrow_terms else []
        if kind == "pr":
            narrow_queries.extend(
                [*issue_term, symbol] for symbol in issue_symbols if symbol.strip()
            )
        attempts: list[tuple[str, list[str], list[str] | None]] = [
            *[
                (
                    "narrow",
                    [
                        command_executable,
                        kind,
                        "list",
                        "--repo",
                        slug,
                        "--search",
                        " ".join(terms),
                        "--state",
                        "all",
                        "--limit",
                        str(limit),
                        "--json",
                        _SEARCH_FIELDS[kind],
                    ],
                    terms,
                )
                for terms in narrow_queries
            ],
            (
                "search",
                [
                    command_executable,
                    "search",
                    "prs" if kind == "pr" else "issues",
                    # Separate terms. One argument is quoted into an exact
                    # phrase that GitHub's search API rejects outright.
                    *(_query_terms(query) or [query]),
                    "--repo",
                    slug,
                    "--limit",
                    str(limit),
                    "--json",
                    _INDEX_FIELDS[kind],
                ],
                None,
            ),
            (
                "list",
                [
                    command_executable,
                    kind,
                    "list",
                    "--repo",
                    slug,
                    "--search",
                    query,
                    "--state",
                    "all",
                    "--limit",
                    str(limit),
                    "--json",
                    _SEARCH_FIELDS[kind],
                ],
                None,
            ),
            (
                "listing",
                [
                    command_executable,
                    kind,
                    "list",
                    "--repo",
                    slug,
                    "--state",
                    "open",
                    "--limit",
                    str(listing_limit),
                    "--json",
                    _LISTING_FIELDS[kind],
                ],
                None,
            ),
        ]
        # Every method runs. Stopping at the first that exits zero is what let a
        # `--search` fallback return `[]` and stand in for a search that never
        # happened. See issue #30.
        succeeded: list[str] = []
        for method, command, terms in attempts:
            result: CommandResult = execute(
                command,
                working_directory=run_directory,
                timeout_seconds=timeout_seconds,
            )
            record["commands"].append({"method": method, **result.to_dict()})
            if result.timed_out or result.exit_code != 0:
                record["failed_methods"].append(
                    {
                        "kind": kind,
                        "method": method,
                        "detail": "timed out"
                        if result.timed_out
                        else (result.stderr or "").strip().splitlines()[:1],
                    }
                )
                continue
            try:
                payload = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                record["failed_methods"].append(
                    {"kind": kind, "method": method, "detail": "unreadable output"}
                )
                continue
            if method == "listing":
                listed[kind] = payload if isinstance(payload, list) else []
                rows = _local_matches(
                    payload,
                    pull_request=kind == "pr",
                    query=query,
                    issue_number=issue_number,
                    # An open issue sharing one common word with the query is
                    # not a duplicate of a fix; an open pull request sharing one
                    # might be, which is how encode/starlette's four were found.
                    minimum_terms=1 if kind == "pr" else 2,
                )
            elif method == "narrow":
                # The narrow query is the issue number and the symbols the
                # change touches. GitHub matched the number as a bare token,
                # so the citation is checked here on the title and body
                # before a hit counts as referencing the issue.
                rows = []
                for entry in payload if isinstance(payload, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    text = " ".join(
                        str(entry.get(field) or "") for field in ("title", "body")
                    )
                    cited = _references_issue(text, issue_number)
                    # A hit that does not cite the issue matched on the
                    # symbols alone, so the `#N` term is not among its
                    # matched terms and `duplicate_is_related` reads it as
                    # a partial match rather than a full one.
                    matched_terms = [
                        term
                        for term in (terms or [])
                        if cited or not term.startswith("#")
                    ]
                    rows.extend(
                        _match_rows(
                            [entry],
                            pull_request=kind == "pr",
                            method="narrow",
                            reasons=["narrow"],
                            matched_terms=matched_terms,
                            term_count=len(terms or []),
                            references_issue=cited,
                        )
                    )
            else:
                rows = _match_rows(
                    payload, pull_request=kind == "pr", method=method
                )
            for row in rows:
                _add_match(record, row, issue_number=issue_number)
            if method == "listing":
                _mark_listing_read(
                    record,
                    payload,
                    rows=rows,
                    pull_request=kind == "pr",
                    term_count=len(_query_terms(query)),
                )
            succeeded.append(method)
            if method == "narrow" and kind == "pr":
                definite = [
                    row
                    for row in record["matches"]
                    if row["pull_request"] and row.get("references_issue")
                ]
                if definite:
                    # A confirmed duplicate is a final answer, so the broad
                    # methods cannot change it. `complete` exists to say an
                    # *empty* result can be trusted; this result is not empty.
                    record["methods"][kind] = succeeded
                    record["success"] = True
                    record["complete"] = True
                    record["decided_by"] = "narrow"
                    record["detail"] = (
                        f"{len(definite)} open or closed pull request(s) already "
                        f"reference issue {issue_number}"
                    )
                    record["match_count"] = len(record["matches"])
                    _write_json(run_directory / DUPLICATE_SEARCH_FILENAME, record)
                    return record
        record["methods"][kind] = succeeded
        if not succeeded:
            record["detail"] = f"every {kind} search failed"
            record["match_count"] = len(record["matches"])
            _write_json(run_directory / DUPLICATE_SEARCH_FILENAME, record)
            return record

    if title:
        _compact_search(
            record,
            run_directory,
            title=title,
            listed=listed,
            slug=slug,
            executable=command_executable,
            issue_number=issue_number,
            timeout_seconds=timeout_seconds,
            limit=limit,
            listing_limit=listing_limit,
        )

    _read_unlisted_rows(
        record,
        run_directory,
        slug=slug,
        executable=command_executable,
        query=query,
        issue_number=issue_number,
        timeout_seconds=timeout_seconds,
    )
    record["success"] = True
    # The unfiltered listing reads every open pull request and issue and matches
    # locally, so it is the method that decides whether the search is worth
    # trusting. The two index-backed methods add closed items and body hits, and
    # GitHub refuses them outright on some repositories, encode/starlette among
    # them. Their failure is recorded and reported but does not block; a listing
    # that failed does, because then an empty result means nothing.
    #
    # The residual blind spot is closed duplicates, which the listing does not
    # cover. `mailman prior-art` is where those are read.
    record["complete"] = all(
        "listing" in methods for methods in record["methods"].values()
    )
    record["decided_by"] = "broad"
    record["match_count"] = len(record["matches"])
    _write_json(run_directory / DUPLICATE_SEARCH_FILENAME, record)
    return record


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def record_no_test_acknowledgement(
    run_directory: Path, *, note: str, diff: str
) -> dict[str, Any]:
    """Record why this change ships without a test, against its exact diff.

    The `no-test-change` gate is right almost always, and a run that argues
    past it has to say why in writing. Run 20260903T052426Z-ad8196 is the case
    it is wrong for: the reviewer exported encode/starlette at the base commit
    and showed that `filterwarnings = ["error"]` already turns the reported
    regression into a conftest import failure across the whole suite, so a
    dedicated test can never be the thing that catches it, and it required the
    added test be removed.

    The record pins the paths the diff touched. A later diff that touches
    anything else is not covered by it, so this cannot become a standing
    waiver.
    """
    if not note.strip():
        raise ValueError("an acknowledgement needs a note saying why no test changed")
    hygiene = analyze_diff(diff)
    files = hygiene["files"]
    if not files:
        raise ValueError("the diff is empty, so there is nothing to acknowledge")
    if any(entry["test"] for entry in files):
        raise ValueError(
            "this diff already changes a test file, so there is nothing to "
            "acknowledge"
        )
    record = {
        "schema_version": 1,
        "acknowledged_at": datetime.now(UTC).isoformat(),
        "note": note.strip(),
        "covered_paths": sorted(entry["path"] for entry in files),
    }
    _write_json(run_directory / NO_TEST_ACKNOWLEDGEMENT_FILENAME, record)
    return record
