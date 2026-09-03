from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult, execute
from mailman.issue import load_issue_record
from mailman.models import RunRecord, RunStatus
from mailman.reproduction import not_reproductions
from mailman.toolchain import resolve_tool


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
    merged = [row for row in strong if duplicate_strength(row) == "merged"]
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
    open_rivals = [row for row in strong if row not in merged]
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

    findings = [Finding(**entry) for entry in hygiene["findings"]]
    findings.extend(
        _policy_findings(
            run,
            policy=policy,
            issue_number=issue_number,
            changed_paths=changed_paths,
            duplicate_search=duplicate_search,
            acknowledgement=acknowledgement,
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
        "duplicate_search_recorded": bool(duplicate_search)
        and duplicate_search.get("success") is True,
        "duplicate_candidates": _duplicate_candidate_counts(
            duplicate_search, acknowledgement, issue_number
        ),
        "files": ["pull-request.md", "accountability.md", "submission.json"],
    }
    (destination_path / "submission.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


DUPLICATE_SEARCH_FILENAME = "duplicate-search.json"
DUPLICATE_ACKNOWLEDGEMENT_FILENAME = "duplicate-acknowledgement.json"
NO_TEST_ACKNOWLEDGEMENT_FILENAME = "no-test-acknowledgement.json"

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
    # An older record has no `methods`, and its `matched_by` held the method
    # name for index hits. Read both so a run recorded before #31 still judges.
    methods = [str(method) for method in row.get("methods") or []] or reasons
    if any(method in _INDEX_METHODS for method in methods):
        return True
    matched = row.get("matched_terms") or []
    term_count = row.get("term_count") or 0
    return bool(term_count and len(matched) >= term_count)


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


def record_duplicate_acknowledgement(
    run_directory: Path, *, note: str
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
    record = {
        "schema_version": 1,
        "acknowledged_at": datetime.now(UTC).isoformat(),
        "note": note.strip(),
        "searched_at": search.get("searched_at"),
        "reviewed": sorted(_duplicate_key(row) for row in weak),
        "strong_at_acknowledgement": sorted(_duplicate_key(row) for row in strong),
    }
    _write_json(run_directory / DUPLICATE_ACKNOWLEDGEMENT_FILENAME, record)
    return record

# `gh pr list --search` is repo-scoped and works where the global `gh search`
# index refuses a repository, which it does for encode/starlette.
_SEARCH_FIELDS = "number,title,state,url,createdAt"
# The unfiltered listing needs the text a local match reads. An issue has no
# head ref, and asking for one makes `gh issue list` refuse the whole call.
_LISTING_FIELDS = {
    "pr": "number,title,state,url,createdAt,body,headRefName",
    "issue": "number,title,state,url,createdAt,body",
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
) -> dict[str, Any]:
    """Search a target's pull requests and issues, and record what came back.

    Starlette treats a duplicate pull request as a ban-level offence, and no
    project welcomes one. The search is cheap; forgetting it is not. The record
    is evidence that it happened, with the query that was used, so a human can
    judge whether it was a real search or a token one.
    """
    if not query.strip():
        raise ValueError("a duplicate search needs a query")
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
        "matches": [],
        "methods": {},
        "failed_methods": [],
        "commands": [],
    }
    for kind in ("pr", "issue"):
        # Two searches disagree in useful ways. The global index finds a pull
        # request whose body says "Fixes #14324"; the repo-scoped list works on
        # repositories the global index refuses, as it does for encode/starlette.
        attempts = [
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
                    _SEARCH_FIELDS,
                ],
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
                    _SEARCH_FIELDS,
                ],
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
            ),
        ]
        # Every method runs. Stopping at the first that exits zero is what let a
        # `--search` fallback return `[]` and stand in for a search that never
        # happened. See issue #30.
        succeeded: list[str] = []
        for method, command in attempts:
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
            else:
                rows = _match_rows(
                    payload, pull_request=kind == "pr", method=method
                )
            for row in rows:
                if (
                    issue_number is not None
                    and not row["pull_request"]
                    and row["number"] == issue_number
                ):
                    # An index search returns the run's own issue. Issue #31.
                    continue
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
                    continue
                # The same row found twice is stronger, not redundant. Keep
                # every method and reason so the strength reads correctly.
                for field in ("methods", "matched_by", "matched_terms"):
                    merged = list(existing.get(field) or [])
                    merged.extend(
                        item for item in row.get(field) or [] if item not in merged
                    )
                    existing[field] = merged
                existing["term_count"] = max(
                    existing.get("term_count") or 0, row.get("term_count") or 0
                )
                existing["references_issue"] = bool(
                    existing.get("references_issue") or row.get("references_issue")
                )
            succeeded.append(method)
        record["methods"][kind] = succeeded
        if not succeeded:
            record["detail"] = f"every {kind} search failed"
            record["match_count"] = len(record["matches"])
            _write_json(run_directory / DUPLICATE_SEARCH_FILENAME, record)
            return record

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
