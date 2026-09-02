from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.issue import load_issue_record
from mailman.models import RunRecord, RunStatus


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
        payload = json.loads(path.read_text(encoding="utf-8"))
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
    if any(segment in {"test", "tests", "testing"} for segment in segments[:-1]):
        return True
    return name.startswith("test_") or name.endswith(("_test.py", "_tests.py"))


def analyze_diff(diff: str) -> dict[str, Any]:
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
        findings.append(
            Finding(
                code="no-test-change",
                blocking=True,
                detail=(
                    "no test file changed, so nothing proves the fix matters or "
                    "stays fixed"
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
    return [record for record in records if isinstance(record, dict)]


def _issue_number(issue_record: dict[str, Any] | None) -> int | None:
    reference = (issue_record or {}).get("reference")
    if isinstance(reference, dict) and isinstance(reference.get("number"), int):
        return reference["number"]
    return None


def _policy_findings(
    run: RunRecord,
    *,
    policy: TargetPolicy,
    issue_number: int | None,
    changed_paths: list[str],
    duplicate_search: dict[str, Any] | None,
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
    if policy.requires_duplicate_search and not duplicate_search:
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
    if run.status is not RunStatus.READY_FOR_HUMAN_REVIEW:
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


def _pull_request_markdown(
    run: RunRecord,
    *,
    policy: TargetPolicy,
    issue_number: int | None,
    branch: str,
    title: str,
    verifications: list[dict[str, Any]],
) -> str:
    reference = f"Closes #{issue_number}." if issue_number else f"Refs {run.issue}."
    disclosure = (
        f"This change was drafted with AI assistance ({run.primary.agent} wrote the "
        f"patch, {run.reviewer.agent} reviewed it). I have read, tested, and take "
        "responsibility for every line of it."
    )
    passing = [record for record in verifications if record.get("exit_code") == 0]
    command = " ".join(passing[-1].get("command", [])) if passing else "not recorded"
    lines = [
        "# Draft pull request",
        "",
        "Nothing here has been sent. A human decides whether any of it is used,",
        "and rewrites the body in their own words before it is.",
        "",
        f"- Target: {policy.name}",
        f"- Policy: {policy.policy_url}",
        f"- Suggested branch: `{branch}`",
        f"- Suggested title: {title}",
        "",
        "## Body draft",
        "",
        reference,
        "",
        "_Describe the cause and the fix here, in your own words._",
        "",
        "### How this was tested",
        "",
        f"`{command}` passes in a clean checkout at `{run.base_commit[:12]}`.",
        "",
        "### AI disclosure",
        "",
    ]
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


def prepare_submission(
    run: RunRecord,
    run_directory: Path,
    *,
    diff: str,
    policy: TargetPolicy,
    destination: Path,
    branch: str,
    title: str,
) -> dict[str, Any]:
    """Assemble everything a human needs before opening a pull request.

    This never contacts the upstream repository. It reports whether the change
    and the run's evidence meet the target's own written rules, and refuses to
    call a submission ready when they do not.
    """
    issue_record = load_issue_record(run_directory)
    issue_number = _issue_number(issue_record)
    hygiene = analyze_diff(diff)
    changed_paths = [entry["path"] for entry in hygiene["files"]]
    verifications = _verification_rows(run_directory)
    duplicate_search_path = run_directory / "duplicate-search.json"
    duplicate_search = (
        json.loads(duplicate_search_path.read_text(encoding="utf-8"))
        if duplicate_search_path.is_file()
        else None
    )

    findings = [Finding(**entry) for entry in hygiene["findings"]]
    findings.extend(
        _policy_findings(
            run,
            policy=policy,
            issue_number=issue_number,
            changed_paths=changed_paths,
            duplicate_search=duplicate_search,
        )
    )
    findings.extend(_evidence_findings(run, verifications))
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
        "duplicate_search_recorded": duplicate_search is not None,
        "files": ["pull-request.md", "accountability.md", "submission.json"],
    }
    (destination_path / "submission.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record
