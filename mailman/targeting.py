"""Decide whether an issue is worth starting a run against.

The duplicate search used to run at submission time, after two agents and a
full environment build had already been spent. That is backwards. An issue
with an open pull request against it is a bad target however good the patch
turns out to be, and that is knowable before anything is cloned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.base_snippets import BASE_SNIPPET_CHECK_FILENAME
from mailman.claims import CLAIMS_FILENAME
from mailman.issue import load_issue_record
from mailman.provenance import load_provenance
from mailman.reproduction import REPRODUCTION_FILENAME, merge_is_in_base
from mailman.submission import (
    DUPLICATE_SEARCH_FILENAME,
    partition_duplicates,
    related_duplicates,
)
from mailman.target_intel import TARGET_INTEL_FILENAME

PRIOR_ART_FILENAME = "prior-art.json"

NO_DUPLICATE_SEARCH = "no-duplicate-search"
NO_TARGET_INTEL = "no-target-intel"
NO_CLAIM_CHECK = "no-claim-check"
ISSUE_ASSIGNED = "issue-assigned"
WORK_HANDED_OVER = "work-handed-over"
UNACKNOWLEDGED_CLAIM = "unacknowledged-claim"
NO_REPRODUCTION = "no-reproduction"
BUG_NOT_REPRODUCED = "bug-not-reproduced"
UNVERIFIED_REPRODUCTION = "reproduction-not-machine-checked"
FAILS_FRESHNESS_BAR = "fails-freshness-bar"
OPEN_PULL_REQUEST = "open-pull-request"
# The same code `prepare-submission` uses for a merged duplicate. The
# judgement is identical; `check-target` only reaches it earlier.
ALREADY_FIXED_UPSTREAM = "already-fixed-upstream"
# A merged pull request that is already an ancestor of the base commit the
# reproduction failed at. Upstream shipped something to the same code and the
# defect survived it, so this is a warning to read rather than a refusal.
MERGED_FIX_ALREADY_IN_BASE = "merged-fix-already-in-base"
UNACKNOWLEDGED_ATTEMPTS = "unacknowledged-prior-attempts"
# The repository's guide requires a maintainer to have answered the issue
# before a pull request exists, and nobody has. getsentry/sentry-python closes
# these automatically and labels them `violating-contribution-guidelines`, so
# the patch is never read however good it is. Decided by `prescreen`, which has
# both the repository screen and the issue's own thread.
# https://github.com/wolfgang-aura/Mailman/issues/99
NO_MAINTAINER_REPLY = "no-maintainer-reply"
#: An earlier pull request that is no longer a claim on the issue: open and
#: untouched for a long time, or closed without being merged. A warning, never
#: a block. The stale attempt is prior art the agent has to read and the pull
#: request body has to supersede.
STALE_PRIOR_ATTEMPT = "stale-prior-attempt"
#: The repository's guide rejects a second pull request for an issue outright,
#: and an earlier one is open. Dormancy does not clear it: the rule is about
#: the duplicate, not about whether anybody is still working. urllib3 says
#: "Duplicate pull requests for the same issue, including alternative
#: solutions, will be rejected without review unless a maintainer has approved
#: opening an alternative pull request in advance", and superseding a dormant
#: attempt there is the contribution they close without reading. A closed
#: unmerged attempt is not a duplicate of anything and is unaffected.
DUPLICATE_FORBIDDEN_OPEN_ATTEMPT = "duplicate-forbidden-open-attempt"
#: An earlier attempt a maintainer closed. Not a stale attempt: somebody who
#: speaks for the project read the change and said no, so there is nothing
#: dormant to supersede. `prescreen` passed skfolio#307 and wagtail#14384 with
#: a `stale-prior-attempt` warning, and both pull requests had been closed by
#: maintainers rejecting the change. tqdm#1816 and #1818 were closed by their
#: own authors, which is the shape the stale rule is for.
MAINTAINER_CLOSED_ATTEMPT = "maintainer-closed-attempt"

#: How long an open pull request may sit untouched before it stops claiming the
#: issue. Decided by the operator on 2026-09-17: the last hunt lost 40 of 66
#: pre-screens to `open-pull-request`, most of them against attempts nobody had
#: touched in months. A dormant attempt is prior art, not a rival in flight.
#: One constant, read in one place, so `prescreen`, `check-target`, `hunt
#: status`, `hunt finish`, the orchestrator and `prepare-submission` cannot
#: disagree about what counts as claimed.
STALE_ATTEMPT_DAYS = 60

#: Author associations that speak for the project. A dormant branch belonging
#: to one of these is a maintainer's own work in progress, and opening a second
#: pull request over it is the same offence as racing a live one.
_MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})


def _first(row: dict[str, Any], *names: str) -> Any:
    """The first of these keys the row actually carries.

    Rows reach this module in three spellings: the duplicate search's
    `created_at`, `gh`'s own `createdAt`, and prior art's summary. Reading all
    of them here keeps every caller from having to normalize first.
    """
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def attempt_last_activity(row: dict[str, Any]) -> datetime | None:
    """When this pull request was last touched, by its own record."""
    return _timestamp(_first(row, "updated_at", "updatedAt")) or _timestamp(
        _first(row, "created_at", "createdAt")
    )


def attempt_age_days(
    row: dict[str, Any], *, now: datetime | None = None
) -> float | None:
    """Days since the last activity, or None when the row records none."""
    last = attempt_last_activity(row)
    if last is None:
        return None
    return ((now or datetime.now(UTC)) - last).total_seconds() / 86400.0


def attempt_is_merged(row: dict[str, Any]) -> bool:
    if str(row.get("outcome") or "").lower() == "merged":
        return True
    if str(row.get("state") or "").upper() == "MERGED":
        return True
    return bool(_first(row, "merged_at", "mergedAt"))


def attempt_is_closed_unmerged(row: dict[str, Any]) -> bool:
    if attempt_is_merged(row):
        return False
    outcome = str(row.get("outcome") or "").lower()
    if outcome:
        return outcome != "open"
    return str(row.get("state") or "").upper() == "CLOSED" or bool(
        _first(row, "closed_at", "closedAt")
    )


def attempt_is_maintainers(row: dict[str, Any]) -> bool:
    association = str(
        _first(row, "author_association", "authorAssociation") or ""
    ).upper()
    return association in _MAINTAINER_ASSOCIATIONS


def attempt_is_dormant(
    row: dict[str, Any], *, now: datetime | None = None
) -> bool:
    """Whether the attempt itself has stopped moving, whoever wrote it.

    Separate from `is_stale_attempt` because the author association costs an
    extra call on some paths, and this is the question that decides whether
    paying for it is worth it.
    """
    if not isinstance(row, dict) or attempt_is_merged(row):
        return False
    if attempt_is_closed_unmerged(row):
        return True
    age = attempt_age_days(row, now=now)
    # No timestamp at all means dormancy cannot be proved, and an unproved
    # dormancy has to read as a live claim. Silence is not evidence.
    return age is not None and age >= STALE_ATTEMPT_DAYS


def attempt_is_maintainer_closed(row: dict[str, Any]) -> bool:
    """Whether somebody who speaks for the project closed this attempt.

    Written by whichever stage fetched the attempt, because the closing actor
    is an extra API read and this module has no API budget. A record with no
    answer reads as `False`: unknown, and the caller notes that it is unknown
    rather than claiming the author withdrew it.
    """
    if not isinstance(row, dict):
        return False
    closed_by = row.get("closed_by")
    if isinstance(closed_by, dict) and closed_by.get("maintainer"):
        return True
    return bool(row.get("maintainer_closed"))


def is_stale_attempt(row: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Whether this pull request is a dormant prior attempt, not a claim.

    A merged pull request is never stale: it is what the repository ships, and
    `already-fixed-upstream` answers it. A maintainer's own attempt is never
    stale either, however long it has sat there, and neither is one a
    maintainer closed: that is a decision about the change.
    """
    return (
        attempt_is_dormant(row, now=now)
        and not attempt_is_maintainers(row)
        and not attempt_is_maintainer_closed(row)
    )


def stale_attempt_row(
    row: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """The stale attempt as the assessment, the prompts and the record carry it."""
    age = attempt_age_days(row, now=now)
    last = attempt_last_activity(row)
    return {
        "number": row.get("number"),
        "repository": _first(row, "repository", "repo"),
        "url": row.get("url"),
        "title": row.get("title"),
        "state": "closed unmerged" if attempt_is_closed_unmerged(row) else "open",
        "updated_at": last.isoformat() if last else None,
        "days_stale": round(age, 1) if age is not None else None,
        "is_draft": _first(row, "is_draft", "isDraft"),
        "author_association": _first(row, "author_association", "authorAssociation"),
        # Who closed it, when the stage that fetched the row found out. `None`
        # means nobody asked or GitHub did not say, which the reader has to be
        # able to tell from "the author withdrew it".
        "closed_by": row.get("closed_by"),
    }


def partition_duplicate_blocked(
    rows: list[dict[str, Any]], *, forbids_duplicates: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split stale-attempt rows into the ones a no-duplicate rule re-blocks.

    One function, so `prescreen`'s thread stage and `check-target` cannot
    disagree about which dormant attempts a repository still counts as the
    claim. Rows are `stale_attempt_row` shapes, whose `state` is either `open`
    or `closed unmerged`.
    """
    if not forbids_duplicates:
        return list(rows), []
    stale = [row for row in rows if row.get("state") != "open"]
    blocked = [row for row in rows if row.get("state") == "open"]
    return stale, blocked


@dataclass(frozen=True)
class TargetAssessment:
    """What the recorded searches say about an issue, before a run starts."""

    searched: bool
    target_read: bool = False
    intel: dict[str, Any] = field(default_factory=dict)
    reproduction: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, Any] = field(default_factory=dict)
    base_snippets: dict[str, Any] = field(default_factory=dict)
    open_attempts: list[dict[str, Any]] = field(default_factory=list)
    merged_attempts: list[dict[str, Any]] = field(default_factory=list)
    superseded_attempts: list[dict[str, Any]] = field(default_factory=list)
    closed_attempts: list[dict[str, Any]] = field(default_factory=list)
    stale_attempts: list[dict[str, Any]] = field(default_factory=list)
    duplicate_blocked_attempts: list[dict[str, Any]] = field(default_factory=list)
    maintainer_closed_attempts: list[dict[str, Any]] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def may_start(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {
            "searched": self.searched,
            "target_read": self.target_read,
            "intel": self.intel,
            "reproduction": self.reproduction,
            "claims": self.claims,
            "base_snippets": self.base_snippets,
            "open_attempts": self.open_attempts,
            "merged_attempts": self.merged_attempts,
            "superseded_attempts": self.superseded_attempts,
            "closed_attempts": self.closed_attempts,
            "stale_attempts": self.stale_attempts,
            "duplicate_blocked_attempts": self.duplicate_blocked_attempts,
            "maintainer_closed_attempts": self.maintainer_closed_attempts,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "may_start": self.may_start,
        }

    def _intel_summary(self) -> str:
        """State the merge path in the two sentences that change a decision."""
        freshness = self.intel.get("freshness", {})
        assessment = self.intel.get("assessment", {})
        parts = [
            f"target    {self.intel.get('repository')}: "
            f"{freshness.get('human_outside_merges', 0)} human outside merge(s) in "
            f"{self.intel.get('window_days')} days, "
            f"{freshness.get('outside_pull_requests_closed_unmerged', 0)} outside "
            "pull request(s) closed unmerged in the same window"
        ]
        read = assessment.get("merge_path_rows_read", 0)
        held = assessment.get("merges_whose_author_held_the_assignment", 0)
        if read and held:
            parts.append(
                f"          {held} of {read} outside merge(s) read here held the "
                "linked issue's assignment first. See target-intel.md for the "
                "threads that won them."
            )
        markers = assessment.get("automated_enforcement") or []
        if markers:
            parts.append(
                "          automated rules in force: " + ", ".join(markers)
            )
        return "\n".join(parts)

    def _reproduction_summary(self) -> list[str]:
        """Say whether the bug is still there, in the words that decide it."""
        if not self.reproduction:
            return [
                "Nothing is recorded about whether the reported bug still "
                "happens at the base commit. Run `mailman reproduce RUN_ID -- "
                "<command>` after `prepare-environment`. A human reading may "
                "be recorded, but it does not authorize agent work."
            ]
        if self.reproduction.get("machine_checked") is not True:
            note = self.reproduction.get("note", "")
            return [
                "reproduce  BLOCKED: read by a human, not checked by machine: "
                + note
            ]
        if self.reproduction.get("reproduced") is True:
            return [
                "reproduce  the reported behaviour still happens at the base "
                "commit"
            ]
        failed = [
            check
            for check in self.reproduction.get("checks", [])
            if isinstance(check, dict) and not check.get("passed")
        ]
        lines = [
            "reproduce  the reported behaviour did NOT happen at the base "
            "commit. The bug may already be fixed, or the reproducer may be "
            "wrong. Either way this is not a target yet."
        ]
        for check in failed:
            lines.append(f"           {check.get('name')}: {check.get('detail')}")
        return lines

    def _claims_summary(self) -> list[str]:
        """Say who has already said they are on this, in their own words."""
        if self.claims.get("success") is not True:
            return [
                "Nothing is recorded about who has already claimed this issue "
                "in its own comments. Run `mailman claims RUN_ID`: a claim made "
                "in a comment is the earliest form of prior art and the "
                "duplicate search cannot see it."
            ]
        if self.claims.get("self_reported") is True:
            # Worth stating rather than passing quietly. The reader should know
            # the claim gate found nothing because nothing exists, not because
            # a thread was read and came back clean.
            return [
                "claims    no upstream issue: this run started from a defect "
                "report, so the duplicate search is the only prior art"
            ]
        assignees = self.claims.get("assignees") or []
        lines: list[str] = []
        if assignees:
            lines.append(
                "assigned  this issue is assigned to " + ", ".join(assignees)
            )
        for row in self.claims.get("claims") or []:
            lines.append(
                f"claimed   {row.get('author')} ({row.get('association')}, "
                f"{row.get('created_at')}): {row.get('quote', '')[:200]}"
            )
        for row in self.claims.get("assignments") or []:
            lines.append(
                f"handed    a maintainer, {row.get('author')}, handed the work "
                f"over: {row.get('quote', '')[:200]}"
            )
        if not lines:
            lines.append(
                f"claims    {self.claims.get('comments_read', 0)} comment(s) "
                "read, none of them a claim"
            )
        return lines

    def summary(self) -> str:
        # The prior-art verdict is stated on its own terms. It used to be
        # inferred from "nothing else was printed", which the target-intel line
        # silently swallowed.
        lines: list[str] = []
        if not self.searched:
            lines.append(
                "No duplicate search is recorded for this run. Run "
                "`mailman duplicate-search RUN_ID --query ...` first."
            )
        if not self.target_read:
            lines.append(
                "Nothing is recorded about how this target hands out work. Run "
                "`mailman target-intel RUN_ID` first: it reads the merges that "
                "actually landed, the threads that preceded them, and the rules "
                "the repository enforces automatically."
            )
        elif self.intel:
            lines.append(self._intel_summary())
        lines.extend(self._reproduction_summary())
        lines.extend(self._claims_summary())
        if self.base_snippets.get("success"):
            prefix = "at base   " if self.base_snippets.get("already_fixed") else "snippets  "
            lines.append(prefix + str(self.base_snippets.get("detail", "")))
        for attempt in self.open_attempts:
            lines.append(
                f"open      #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        for attempt in self.merged_attempts:
            lines.append(
                f"merged    #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        for attempt in self.superseded_attempts:
            lines.append(
                f"in base   #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        for attempt in self.closed_attempts:
            lines.append(
                f"closed    #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        for attempt in self.stale_attempts:
            days = attempt.get("days_stale")
            age = f", {days} days since its last activity" if days is not None else ""
            lines.append(
                f"stale     #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('state')}{age}) {attempt.get('url', '')}"
            )
        for attempt in self.maintainer_closed_attempts:
            closer = attempt.get("closed_by") or {}
            lines.append(
                f"rejected  #{attempt.get('number')} {attempt.get('title', '')} "
                f"({closer.get('detail', 'closed by the project')}) "
                f"{attempt.get('url', '')}"
            )
        if self.maintainer_closed_attempts:
            lines.append(
                "A maintainer closed that attempt. Somebody who speaks for the "
                "project read the change and said no, so there is no dormant "
                "branch here to supersede. Read the closure, then pick another "
                "issue."
            )
        for attempt in self.duplicate_blocked_attempts:
            days = attempt.get("days_stale")
            age = f", {days} days since its last activity" if days is not None else ""
            lines.append(
                f"rival     #{attempt.get('number')} {attempt.get('title', '')} "
                f"(open{age}) {attempt.get('url', '')}"
            )
        if self.duplicate_blocked_attempts:
            lines.append(
                "This repository rejects a duplicate pull request for an issue "
                "that already has one, without reading it. A dormant attempt is "
                "still that pull request, so there is nothing to supersede here. "
                "Pick another issue."
            )
        if self.stale_attempts:
            lines.append(
                f"A pull request open and untouched for {STALE_ATTEMPT_DAYS} days, "
                "or closed without merging, is a prior attempt rather than a "
                "claim. Read its diff and its review comments before you start, "
                "and say in the pull request body that yours supersedes it."
            )
        if self.open_attempts:
            lines.append(
                "An open pull request means someone is already on this. A "
                "second one is the contribution maintainers close and ban for."
            )
        if self.claims.get("assignees"):
            lines.append(
                "An assigned issue belongs to whoever holds it. Pick another."
            )
        elif self.claims.get("assignments"):
            lines.append(
                "A maintainer already handed this work to somebody in the "
                "thread. That is an assignment written in prose rather than in "
                "the assignee field, and it counts the same."
            )
        elif self.claims.get("claims"):
            lines.append(
                "Somebody has said in the comments that they are taking this, "
                "and nobody has answered them. Read the thread, then pass "
                "--acknowledge-claims to start anyway."
            )
        if self.merged_attempts:
            lines.append(
                "A merged pull request on this issue is already upstream. "
                "Nobody rejected it; it is what the repository ships. Read it "
                "before assuming there is anything left to fix, and re-check "
                "the reproduction against a tree that includes it."
            )
        if self.superseded_attempts:
            lines.append(
                "A merged pull request matched, but its merge commit is "
                "already an ancestor of the base commit, and the reproduction "
                "failed at that same commit. Upstream shipped something to "
                "this code and the defect survived it, so this is not the "
                "same change. Read it anyway before repeating any of it."
            )
        if self.closed_attempts and not self.open_attempts:
            lines.append(
                "Closed attempts usually mean the maintainers rejected the "
                "approach, not the code. Read them before repeating one. Pass "
                "--acknowledge-prior-attempts to start anyway."
            )
        if (
            self.searched
            and not self.open_attempts
            and not self.merged_attempts
            and not self.superseded_attempts
            and not self.closed_attempts
            and not self.stale_attempts
            and not self.duplicate_blocked_attempts
            and not self.maintainer_closed_attempts
            and not (self.claims.get("claims") or self.claims.get("assignments"))
            and not self.claims.get("assignees")
        ):
            lines.append("No prior attempt found. This target looks unclaimed.")
        return "\n".join(lines)


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def own_pull_request(run_directory: Path) -> int | None:
    """The pull request this run became, once one has been filed.

    A filed run answers its own duplicate search. The search asks who else is
    working on the issue; the run's own pull request is open, it names the
    issue, and it is a rival to nothing. Hunt 20260916T132927Z-c05183 refreshed
    a run it had just filed, read edgartools#1329 as an open attempt against
    the issue that #1329 fixes, and reported the candidate as replaceable.
    https://github.com/wolfgang-aura/Mailman/issues/97
    """
    number = (load_provenance(run_directory) or {}).get("pull_request")
    return number if isinstance(number, int) else None


def _without_own_attempts(
    rows: list[Any] | None, *, own: int | None
) -> list[dict[str, Any]]:
    """Drop the rows that are this run's own filed pull request."""
    kept = [row for row in rows or [] if isinstance(row, dict)]
    if own is None:
        return kept
    return [row for row in kept if row.get("number") != own]


def assess_target(
    run_directory: Path,
    *,
    acknowledged: bool = False,
    acknowledged_claims: bool = False,
    now: datetime | None = None,
    forbids_duplicates: bool = False,
) -> TargetAssessment:
    """Judge a target from the searches already recorded in the run.

    `now` is the instant the staleness of an earlier attempt is measured
    against. It exists so a test can freeze it; nothing else passes it.

    `forbids_duplicates` is the repository's own rule, read out of the screen
    by the caller: when it is set, a dormant open attempt is still a claim.
    """
    duplicate_search = _read(run_directory / DUPLICATE_SEARCH_FILENAME)
    prior_art = _read(run_directory / PRIOR_ART_FILENAME)
    intel = _read(run_directory / TARGET_INTEL_FILENAME)
    reproduction = _read(run_directory / REPRODUCTION_FILENAME)
    claims = _read(run_directory / CLAIMS_FILENAME)
    base_snippets = _read(run_directory / BASE_SNIPPET_CHECK_FILENAME)
    searched = duplicate_search.get("success") is True
    target_read = intel.get("success") is True

    own = own_pull_request(run_directory)
    attempts = _without_own_attempts(prior_art.get("attempts"), own=own)
    # `prior-art` is deliberately a separate command because it reads the
    # bodies and maintainer responses of matched pull requests. It is not,
    # however, a prerequisite for refusing an already-claimed target. A run
    # can have a completed duplicate search and no prior-art record at all;
    # that was enough for qlib #2278 to pass this gate even though the search
    # already contained open #2330 and #2279. Use the search's strong rows as
    # an early stop, and let prior-art add the context when it exists.
    issue_number = duplicate_search.get("issue_number")
    if not isinstance(issue_number, int):
        reference = (load_issue_record(run_directory) or {}).get("reference")
        issue_number = (
            reference.get("number")
            if isinstance(reference, dict) and isinstance(reference.get("number"), int)
            else None
        )
    related_search = related_duplicates(
        _without_own_attempts(duplicate_search.get("matches"), own=own),
        issue_number=issue_number,
    )
    strong_search, _ = partition_duplicates(
        related_search, issue_number=issue_number
    )
    attempts_by_number = {
        attempt.get("number"): attempt
        for attempt in attempts
        if isinstance(attempt, dict)
    }
    for match in strong_search:
        state = str(match.get("state") or "").lower()
        if state not in ("open", "merged"):
            continue
        # An open issue that cross-references this one is not an attempt.
        # skfolio#312, a tracking issue, was read as an open pull request
        # against #307 and refused a target nobody had claimed.
        if not match.get("pull_request"):
            continue
        number = match.get("number")
        if not isinstance(number, int):
            continue
        # A fresh duplicate search is authoritative for whether the attempt is
        # open. Replace a stale prior-art outcome rather than allowing an old
        # closed record to clear a currently open rival.
        attempts_by_number[number] = {
            **attempts_by_number.get(number, {}),
            **match,
            "outcome": state,
        }
    for match in related_search:
        if str(match.get("state") or "").lower() != "closed":
            continue
        if not match.get("pull_request"):
            continue
        number = match.get("number")
        if not isinstance(number, int) or number in attempts_by_number:
            continue
        attempts_by_number[number] = {**match, "outcome": "closed unmerged"}
    attempts = list(attempts_by_number.values())
    # A dormant attempt is sorted out before anything else reads these lists,
    # so every stage that asks whether the issue is claimed gets one answer.
    # A closure by somebody who speaks for the project is read first, because
    # it changes what every later bucket means: this is a rejection, not an
    # abandoned branch, and neither the stale rule nor the closed-attempt flag
    # answers it.
    maintainer_closed = [
        stale_attempt_row(attempt, now=now)
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt_is_maintainer_closed(attempt)
        and not attempt_is_merged(attempt)
    ]
    rejected_numbers = {row["number"] for row in maintainer_closed}
    dormant = [
        stale_attempt_row(attempt, now=now)
        for attempt in attempts
        if isinstance(attempt, dict) and is_stale_attempt(attempt, now=now)
    ]
    # A repository that rejects duplicates outright keeps its open attempts,
    # dormant or not. They stay out of `open_attempts` so the record says which
    # rule refused the target rather than reporting a live rival that is not.
    stale_attempts, duplicate_blocked = partition_duplicate_blocked(
        dormant, forbids_duplicates=forbids_duplicates
    )
    stale_numbers = {row["number"] for row in dormant} | rejected_numbers
    open_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt.get("outcome") == "open"
        and attempt.get("number") not in stale_numbers
    ]
    all_merged = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("outcome") == "merged"
    ]
    superseded_attempts = [
        attempt
        for attempt in all_merged
        if merge_is_in_base(run_directory, attempt, reproduction)
    ]
    merged_attempts = [
        attempt for attempt in all_merged if attempt not in superseded_attempts
    ]
    # Three buckets, because a row's state changes what it means. `merged` used
    # to land here and be reported as a rejection, which is the opposite of the
    # truth. See https://github.com/wolfgang-aura/Mailman/issues/38.
    closed_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt.get("outcome") not in ("open", "merged")
        and attempt.get("number") not in stale_numbers
    ]

    blocking: list[str] = []
    warnings: list[str] = []
    if not searched:
        blocking.append(NO_DUPLICATE_SEARCH)
    if not target_read:
        # Not overridable. Reading how a target merges outside work costs a
        # handful of API calls; skipping it cost a whole session on
        # https://github.com/wolfgang-aura/Mailman/issues/35.
        blocking.append(NO_TARGET_INTEL)
    elif not intel.get("assessment", {}).get("passes_freshness_bar", True):
        warnings.append(FAILS_FRESHNESS_BAR)
    if reproduction.get("success") is not True:
        # An agent run against an already-fixed issue is the most expensive way
        # to learn nothing. See
        # https://github.com/wolfgang-aura/Mailman/issues/37.
        blocking.append(NO_REPRODUCTION)
    elif reproduction.get("machine_checked") is not True:
        # A human reading may be useful evidence, but it cannot tell an agent
        # whether its patch changes the reported behaviour. Hunt
        # 20260909T132008Z-054119 spent 55 minutes on a proxy for a remote UI
        # symptom and produced a deterministic hang. See issue #82.
        blocking.append(UNVERIFIED_REPRODUCTION)
    elif reproduction.get("reproduced") is not True:
        # Not overridable, and deliberately so: the right response to a bug
        # that no longer happens is to abandon the run, not to acknowledge it.
        blocking.append(BUG_NOT_REPRODUCED)
    if claims.get("success") is not True:
        # A comment claim is prior art the duplicate search cannot see. See
        # https://github.com/wolfgang-aura/Mailman/issues/36.
        blocking.append(NO_CLAIM_CHECK)
    elif claims.get("assignees"):
        # Not overridable: somebody holds this issue.
        blocking.append(ISSUE_ASSIGNED)
    elif claims.get("assignments"):
        # A maintainer handing the work over is the assignee field written in
        # prose. Not overridable, for the same reason.
        blocking.append(WORK_HANDED_OVER)
    elif claims.get("claims"):
        # An offer nobody answered is worth a human reading rather than a hard
        # stop, so this is the one claim state a flag can clear. Its own flag:
        # --acknowledge-prior-attempts answers a different question, and one
        # flag for two questions is the defect in
        # https://github.com/wolfgang-aura/Mailman/issues/38.
        if acknowledged_claims:
            warnings.append(UNACKNOWLEDGED_CLAIM)
        else:
            blocking.append(UNACKNOWLEDGED_CLAIM)
    if stale_attempts:
        # Never a block. The operator decided on 2026-09-17 that an attempt
        # nobody has touched for STALE_ATTEMPT_DAYS, or one closed without
        # merging, has stopped claiming the issue. It is still prior art, so
        # it travels into both prompts and into the pull request body.
        warnings.append(STALE_PRIOR_ATTEMPT)
    if maintainer_closed:
        # Deliberately not overridable, and not the same question as
        # `--acknowledge-prior-attempts`: that flag answers "an attempt was
        # closed", and this is "the project read it and said no".
        blocking.append(MAINTAINER_CLOSED_ATTEMPT)
    if duplicate_blocked:
        # Not overridable and not a warning. Superseding a dormant attempt is
        # exactly the contribution this repository's guide says it rejects
        # without review.
        blocking.append(DUPLICATE_FORBIDDEN_OPEN_ATTEMPT)
    if open_attempts:
        # Deliberately not overridable. Every escape hatch here is one someone
        # takes at the wrong moment, and `run-agent` still exists for a
        # deliberate run against a claimed issue.
        blocking.append(OPEN_PULL_REQUEST)
    if base_snippets.get("already_fixed") is True:
        # The issue quotes the line it is about, that line is gone from the
        # base tree, and the file it came from is still there. Upstream changed
        # it. No prior art links a merged pull request to this issue, which is
        # exactly why nothing else saw it.
        # https://github.com/wolfgang-aura/Mailman/issues/103
        blocking.append(ALREADY_FIXED_UPSTREAM)
    if merged_attempts and ALREADY_FIXED_UPSTREAM not in blocking:
        # Not overridable. `--acknowledge-prior-attempts` answers "the
        # maintainers closed an attempt"; it has no answer for "upstream
        # already ships this", and `prepare-submission` blocks the same case
        # under the same code hours later.
        blocking.append(ALREADY_FIXED_UPSTREAM)
    if superseded_attempts:
        # Deliberately not a flag. The evidence already answers the question,
        # so nothing is being waved through.
        warnings.append(MERGED_FIX_ALREADY_IN_BASE)
    if closed_attempts:
        if acknowledged:
            warnings.append(UNACKNOWLEDGED_ATTEMPTS)
        else:
            blocking.append(UNACKNOWLEDGED_ATTEMPTS)

    return TargetAssessment(
        searched=searched,
        target_read=target_read,
        intel=intel,
        reproduction=reproduction,
        claims=claims,
        base_snippets=base_snippets,
        open_attempts=open_attempts,
        merged_attempts=merged_attempts,
        superseded_attempts=superseded_attempts,
        closed_attempts=closed_attempts,
        stale_attempts=stale_attempts,
        duplicate_blocked_attempts=duplicate_blocked,
        maintainer_closed_attempts=maintainer_closed,
        blocking=blocking,
        warnings=warnings,
    )
