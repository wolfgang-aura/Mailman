"""Decide an issue is not worth a run, before creating one.

Hunt 20260907T164341Z-1ca91a opened 24 runs and filed 3. Of the 21 drops, 14
were "someone already fixed this" and 2 were "it no longer happens": decidable
from public GitHub state, but only reachable after `init-run`, because both the
duplicate search and the prior-art read write into a run directory.

So the coordinator paid a run's worth of commands, and a run's worth of output
in its own context, to learn something a single query could have told it. This
runs the same checks against an `OWNER/REPO#N` and writes the verdict beside
the repository screens. https://github.com/wolfgang-aura/Mailman/issues/75
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.claims import read_claims
from mailman.issue import capture_issue_from_github
from mailman.prior_art import collect_prior_art
from mailman.submission import (
    partition_duplicates,
    record_duplicate_search,
    related_duplicates,
)
from mailman.targeting import (
    ALREADY_FIXED_UPSTREAM,
    ISSUE_ASSIGNED,
    NO_DUPLICATE_SEARCH,
    OPEN_PULL_REQUEST,
    UNACKNOWLEDGED_ATTEMPTS,
    UNACKNOWLEDGED_CLAIM,
    WORK_HANDED_OVER,
    assess_target,
)

PRESCREEN_SCHEMA_VERSION = 2
ISSUE_SCREENS = "issue-screens"
#: A pre-screen filters a shortlist; it is not the filing gate. The run stage
#: still re-runs the duplicate search under its own one-hour limit, and
#: `hunt finish` still refuses stale evidence. A day is long enough to screen a
#: shortlist in the morning and work it in the afternoon.
PRESCREEN_HOURS = 24
ISSUE_UNREADABLE = "issue-unreadable"
ISSUE_NOT_OPEN = "issue-not-open"
ISSUE_NOT_BOUNDED_FIX = "issue-not-bounded-fix"
_NON_FIX_LABELS = frozenset(
    {
        "enhancement",
        "feature",
        "feature request",
        "feature-request",
        "meta",
        "project",
        "question",
        "tracking",
    }
)
#: What this stage can decide with public GitHub state and a few API calls.
#: Reproduction needs a clone, and target intel comes from `screen-target`.
DECIDABLE = (
    NO_DUPLICATE_SEARCH,
    OPEN_PULL_REQUEST,
    ALREADY_FIXED_UPSTREAM,
    UNACKNOWLEDGED_ATTEMPTS,
    ISSUE_ASSIGNED,
    WORK_HANDED_OVER,
    UNACKNOWLEDGED_CLAIM,
)


def repository_slug(repository: str) -> str:
    slug = repository.removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        slug = slug.removeprefix(prefix)
    return slug


def issue_reference(issue: str) -> tuple[str, int]:
    """Read `OWNER/REPO#N` or an issue URL into a slug and a number."""
    text = issue.strip()
    if "#" in text:
        slug, _, number = text.rpartition("#")
        return repository_slug(slug), int(number)
    slug = repository_slug(text)
    parts = slug.split("/")
    if len(parts) >= 4 and parts[2] == "issues":
        return f"{parts[0]}/{parts[1]}", int(parts[3])
    raise ValueError("expected OWNER/REPO#NUMBER or an issue URL")


def prescreen_directory(data_root: Path, slug: str, number: int) -> Path:
    owner, _, name = slug.partition("/")
    return data_root / ISSUE_SCREENS / f"{owner}__{name}__{number}"


def prescreen_path(data_root: Path, slug: str, number: int) -> Path:
    return prescreen_directory(data_root, slug, number).with_suffix(".json")


def load_prescreen(data_root: Path, slug: str, number: int) -> dict[str, Any] | None:
    path = prescreen_path(data_root, slug, number)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _store_prescreen(data_root: Path, slug: str, number: int, record: dict[str, Any]) -> None:
    path = prescreen_path(data_root, slug, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _issue_blocking(captured: dict[str, Any]) -> list[str]:
    blocking: list[str] = []
    if captured.get("success") is not True:
        blocking.append(ISSUE_UNREADABLE)
    elif str(captured.get("state") or "").upper() != "OPEN":
        blocking.append(ISSUE_NOT_OPEN)
    labels = {str(label).strip().lower() for label in captured.get("labels") or []}
    if labels & _NON_FIX_LABELS:
        blocking.append(ISSUE_NOT_BOUNDED_FIX)
    return blocking


def is_fresh(record: dict[str, Any], *, hours: int = PRESCREEN_HOURS) -> bool:
    try:
        screened = datetime.fromisoformat(record["screened_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now(UTC) - screened < timedelta(hours=hours)


def prescreen_issue(
    data_root: Path,
    issue: str,
    *,
    query: str | None = None,
    symbols: Sequence[str] = (),
    executable: str | None = None,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    """Read the issue and reject duplicates or unbounded work before a run."""
    slug, number = issue_reference(issue)
    directory = prescreen_directory(data_root, slug, number)
    directory.mkdir(parents=True, exist_ok=True)
    issue_url = f"https://github.com/{slug}/issues/{number}"
    captured = capture_issue_from_github(
        directory,
        issue_url=issue_url,
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    record: dict[str, Any] = {
        "schema_version": PRESCREEN_SCHEMA_VERSION,
        "screened_at": datetime.now(UTC).isoformat(),
        "repository": slug,
        "issue_number": number,
        "symbols": [symbol for symbol in symbols if symbol.strip()],
        "workspace": str(directory),
        "issue": {
            "success": captured.get("success"),
            "state": captured.get("state"),
            "title": captured.get("title"),
            "labels": captured.get("labels", []),
            "body_characters": captured.get("body_characters"),
        },
    }
    issue_blocking = _issue_blocking(captured)
    if issue_blocking:
        record.update(
            {
                "blocking": issue_blocking,
                "warnings": [],
                "verdict": "reject",
                "stages_skipped": ["duplicate-search", "prior-art", "claims"],
                "next": f"Do not open a run on {slug}#{number}: "
                + "; ".join(issue_blocking),
            }
        )
        _store_prescreen(data_root, slug, number, record)
        return record
    search = record_duplicate_search(
        directory,
        repository=slug,
        query=query or str(captured.get("title") or f"#{number}"),
        issue_number=number,
        symbols=symbols,
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    record["duplicate_search"] = {
        "success": search["success"],
        "complete": search["complete"],
        "matches": search.get("match_count", 0),
        "failed_methods": search.get("failed_methods", []),
    }
    related = related_duplicates(search.get("matches"), issue_number=number)
    strong, _ = partition_duplicates(related, issue_number=number)
    numbers = sorted(
        {
            row["number"]
            for row in strong
            if isinstance(row.get("number"), int) and row.get("pull_request")
        }
    )
    if numbers:
        prior = collect_prior_art(
            directory,
            repository=slug,
            numbers=numbers,
            executable=executable,
            timeout_seconds=timeout_seconds,
        )
        record["prior_art"] = {
            "success": prior["success"],
            "requested": numbers,
            "open": prior.get("open"),
            "closed_unmerged": prior.get("closed_unmerged"),
        }
    else:
        record["prior_art"] = {
            "success": True,
            "requested": [],
            "detail": "no strong duplicate to read",
        }
    claims = read_claims(
        directory, executable=executable, timeout_seconds=timeout_seconds
    )
    record["claims"] = {
        "success": claims.get("success"),
        "assignees": claims.get("assignees", []),
        "assignments": len(claims.get("assignments", [])),
        "claims": len(claims.get("claims", [])),
    }
    assessment = assess_target(directory)
    blocking = [code for code in assessment.blocking if code in DECIDABLE]
    record["blocking"] = blocking
    record["warnings"] = [code for code in assessment.warnings if code in DECIDABLE]
    record["open_attempts"] = [row.get("number") for row in assessment.open_attempts]
    record["merged_attempts"] = [row.get("number") for row in assessment.merged_attempts]
    record["closed_attempts"] = [row.get("number") for row in assessment.closed_attempts]
    record["verdict"] = "reject" if blocking else "pass"
    if blocking:
        record["next"] = f"Do not open a run on {slug}#{number}: " + "; ".join(blocking)
    else:
        record["next"] = (
            f"mailman init-run --repository https://github.com/{slug}.git "
            f"--issue https://github.com/{slug}/issues/{number} ..."
        )
    _store_prescreen(data_root, slug, number, record)
    return record


def check(data_root: Path, issue: str) -> tuple[dict[str, Any] | None, str | None]:
    """The pre-screen for this issue, and why it does not authorize a run."""
    slug, number = issue_reference(issue)
    record = load_prescreen(data_root, slug, number)
    if record is None:
        return None, (
            f"no pre-screen for {slug}#{number}. Run `mailman prescreen "
            f"{slug}#{number}` first: most targets fail it, and failing it "
            "before a run exists is the whole point"
        )
    if record.get("schema_version") != PRESCREEN_SCHEMA_VERSION:
        return record, (
            f"the pre-screen for {slug}#{number} predates issue classification; "
            f"run `mailman prescreen {slug}#{number}` again"
        )
    if record.get("verdict") != "pass":
        return record, (
            f"{slug}#{number} was rejected by its pre-screen: "
            + "; ".join(record.get("blocking", []))
        )
    if not is_fresh(record):
        return record, (
            f"the pre-screen for {slug}#{number} is older than "
            f"{PRESCREEN_HOURS} hours; run `mailman prescreen {slug}#{number}` again"
        )
    return record, None
