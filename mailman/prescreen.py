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
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.claims import read_claims
from mailman.issue import capture_issue_from_github
from mailman.prior_art import collect_prior_art, resolve_cited_pull_requests
from mailman.screen import (
    DIRECT_PUSH_LIMIT,
    direct_push_share,
    load_screen,
    requires_prior_discussion,
)
from mailman.submission import (
    partition_duplicates,
    record_duplicate_search,
    related_duplicates,
)
from mailman.targeting import (
    ALREADY_FIXED_UPSTREAM,
    ISSUE_ASSIGNED,
    NO_DUPLICATE_SEARCH,
    NO_MAINTAINER_REPLY,
    OPEN_PULL_REQUEST,
    UNACKNOWLEDGED_ATTEMPTS,
    UNACKNOWLEDGED_CLAIM,
    WORK_HANDED_OVER,
    assess_target,
    own_pull_request,
)

#: 4 reads the pull requests the issue's own thread names; 5 asks whether the
#: repository requires a maintainer reply before a pull request exists. A screen
#: written before either never asked the question, so `check` sends it back.
PRESCREEN_SCHEMA_VERSION = 5
ISSUE_SCREENS = "issue-screens"
#: A pre-screen filters a shortlist; it is not the filing gate. The run stage
#: still re-runs the duplicate search under its own one-hour limit, and
#: `hunt finish` still refuses stale evidence. A day is long enough to screen a
#: shortlist in the morning and work it in the afternoon.
PRESCREEN_HOURS = 24
ISSUE_UNREADABLE = "issue-unreadable"
ISSUE_NOT_OPEN = "issue-not-open"
ISSUE_NOT_BOUNDED_FIX = "issue-not-bounded-fix"
#: The fix is small enough that the maintainer writes it rather than reviews it,
#: in a repository whose maintainers push to the default branch. Blocking.
TRIVIAL_FIX_DIRECT_PUSH = "trivial-fix-direct-push-repository"
#: The fix looks trivial but the repository reviews what it merges. A warning:
#: a typo in a project that runs everything through a pull request is still a
#: pull request somebody has to open.
TRIVIAL_FIX = "trivial-fix"
TRIVIAL = "trivial"
UNKNOWN = "unknown"
#: Labels that name the size of the change rather than its subject.
_TRIVIAL_LABELS = frozenset({"typo", "typos"})
#: Wordings that describe a change a maintainer writes in less time than he
#: spends reading a stranger's patch for it. Each carries the reason it is
#: recorded under, because a rejection nobody can read is a rejection nobody
#: trusts. `[^\n]` rather than `.` keeps a match inside one sentence.
#: https://github.com/wolfgang-aura/Mailman/issues/79
_TRIVIAL_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a typo", re.compile(r"\btypos?\b|\bmis-?spell(?:ed|ing|s)?\b", re.IGNORECASE)),
    (
        "a documentation wording change",
        re.compile(
            r"\b(?:docs?|documentation|docstring|readme|changelog)\b[^\n]{0,60}"
            r"\b(?:says?|reads?|wrong|incorrect|outdated|stale|"
            r"should (?:say|read|be))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a one-line message or wording change",
        re.compile(
            r"\b(?:error|warning|log|help|deprecation)\s+messages?\b[^\n]{0,60}"
            r"\b(?:wrong|incorrect|misleading|confusing|typo|"
            r"should (?:say|read|be))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a version-pin bump",
        re.compile(
            r"\b(?:bump|pin|unpin|relax|loosen|widen|raise|drop)\b[^\n]{0,60}"
            r"\b(?:version|requirement|constraint|upper bound|"
            r"dependency|dependencies|pin)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a change the reporter calls one line",
        re.compile(
            r"\bone[- ]?liners?\b|\bone[- ]line\b|\bsingle[- ]line\b"
            r"|\btrivial (?:fix|change|patch)\b",
            re.IGNORECASE,
        ),
    ),
)
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
    NO_MAINTAINER_REPLY,
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


def _store_prescreen(
    data_root: Path, slug: str, number: int, record: dict[str, Any]
) -> None:
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


def _captured_body(directory: Path) -> str:
    """The issue body out of the `issue.md` the capture already wrote.

    The capture record counts the body's characters but does not keep the text,
    and re-reading the issue to classify it would double the API cost of a
    stage that exists to be cheap.
    """
    path = directory / "issue.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    _, _, after = text.partition("## Issue body")
    body, _, _ = after.partition("## Capture boundary")
    return body.strip()


# Identifiers a reporter writes into an issue body: a backticked name, a dotted
# path, or a Python file. Prose words are not identifiers, so a backticked name
# has to carry an underscore to count, and a dotted path is reduced to its last
# segment, which is the token a pull request title or body repeats.
_BACKTICK_RE = re.compile(r"`([^`\s][^`\n]{0,79})`")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w.]*(?:\(\))?$")
_PY_FILE_RE = re.compile(r"\b[\w/-]+\.py\b")
ISSUE_SYMBOL_LIMIT = 4


def issue_symbols(body: str, *, limit: int = ISSUE_SYMBOL_LIMIT) -> list[str]:
    """Identifier-looking tokens from the issue body, first mention first.

    llama_index#22639 named `_handle_upserts` in its first paragraph and both
    open rival pull requests carried it, but neither mentioned the issue
    number, so a narrow search built only from the symbols the coordinator
    typed passed the issue. The body already holds the query; this reads it.
    Each symbol becomes its own narrow query, because GitHub's search joins
    terms with AND and a seven-term query returned nothing on the same issue.
    The count is capped because each one is a search API call.
    """
    found: list[str] = []
    for match in _BACKTICK_RE.finditer(body):
        token = match.group(1).strip()
        if not _IDENTIFIER_RE.match(token):
            continue
        name = token.removesuffix("()").rstrip(".").rsplit(".", 1)[-1]
        if "_" not in name:
            continue
        if name not in found:
            found.append(name)
    for match in _PY_FILE_RE.finditer(body):
        name = match.group(0).rsplit("/", 1)[-1]
        if name not in found:
            found.append(name)
    return found[:limit]


def estimate_fix_size(
    title: str | None, body: str | None, labels: Sequence[Any] = ()
) -> tuple[str, str]:
    """Guess how big the change is, from what the issue says it wants.

    Two answers only, `trivial` and `unknown`, because that is as much as the
    issue text can carry. `trivial` means the maintainer plausibly writes this
    himself in the time it takes to open the review tab: a documentation typo,
    one wrong message, a version pin. Everything else is `unknown`; this never
    claims a change is large.

    The reason travels with the answer. A run refused on a guess has to say
    which words produced the guess. See
    https://github.com/wolfgang-aura/Mailman/issues/79.
    """
    spellings = {str(label).strip().lower() for label in labels or []}
    named = sorted(spellings & _TRIVIAL_LABELS)
    if named:
        return TRIVIAL, f"labelled {named[0]}"
    text = f"{title or ''}\n{body or ''}"
    for reason, pattern in _TRIVIAL_SIGNALS:
        found = pattern.search(text)
        if found:
            return TRIVIAL, f"{reason}, from {found.group(0).strip()!r}"
    return UNKNOWN, "nothing in the issue names a one-line change"


def _fix_size_detail(estimate: str, reason: str, share: float | None) -> str:
    """One readable line, because a number in a record decides nothing by itself."""
    habit = (
        "the repository's direct-push share is unrecorded; screen it with "
        "`mailman screen-target`"
        if share is None
        else f"{share} of its recent default-branch commits arrived outside a "
        "pull request"
    )
    if estimate != TRIVIAL:
        return f"fix size unknown: {reason}; {habit}"
    if share is not None and share >= DIRECT_PUSH_LIMIT:
        return (
            f"the fix reads as trivial ({reason}) and {habit}, at or above the "
            f"{DIRECT_PUSH_LIMIT} limit: the maintainer will write this before "
            "he reviews it"
        )
    return f"the fix reads as trivial ({reason}), but {habit}"


def _citable(
    claims: dict[str, Any], *, slug: str, directory: Path
) -> list[dict[str, Any]]:
    """The references worth a `gh` call, minus the one we filed ourselves.

    A filed run's own pull request cites the issue and is cited back by it, and
    reading that as a rival is the defect commit 495b9e8 fixed for the
    duplicate search. The same exclusion belongs here, for the same reason.
    """
    own = own_pull_request(directory)
    references = [
        reference
        for reference in claims.get("references") or []
        if isinstance(reference, dict)
    ]
    if own is None:
        return references
    return [
        reference
        for reference in references
        if not (
            str(reference.get("repository") or "").lower() == slug.lower()
            and reference.get("number") == own
        )
    ]


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
    # Asked before the duplicate search, because it is the cheaper question and
    # it can end the screen on its own. A fix small enough to write is a wasted
    # run in a repository where the maintainer writes rather than reviews.
    estimate, reason = estimate_fix_size(
        captured.get("title"), _captured_body(directory), captured.get("labels") or []
    )
    screen = load_screen(data_root, slug)
    share = direct_push_share(screen)
    warnings: list[str] = []
    if estimate == TRIVIAL:
        if share is not None and share >= DIRECT_PUSH_LIMIT:
            issue_blocking.append(TRIVIAL_FIX_DIRECT_PUSH)
        else:
            warnings.append(TRIVIAL_FIX)
    record["fix_size"] = {
        "estimate": estimate,
        "reason": reason,
        "direct_push_share": share,
        "direct_push_limit": DIRECT_PUSH_LIMIT,
        "detail": _fix_size_detail(estimate, reason, share),
    }
    if issue_blocking:
        record.update(
            {
                "blocking": issue_blocking,
                "warnings": warnings,
                "verdict": "reject",
                "stages_skipped": ["duplicate-search", "prior-art", "claims"],
                "next": f"Do not open a run on {slug}#{number}: "
                + "; ".join(issue_blocking)
                + (
                    f". {record['fix_size']['detail']}"
                    if TRIVIAL_FIX_DIRECT_PUSH in issue_blocking
                    else ""
                ),
            }
        )
        _store_prescreen(data_root, slug, number, record)
        return record
    # The thread is read before the search, because it answers the same
    # question for less. An issue whose own body links the pull request that
    # fixes it needs one `gh pr view`, not eighty-eight search results that
    # rank it nowhere: deepset-ai/haystack#12777 passed this screen with its
    # draft implementation linked in the last line of the body.
    # https://github.com/wolfgang-aura/Mailman/issues/98
    claims = read_claims(
        directory, executable=executable, timeout_seconds=timeout_seconds
    )
    record["claims"] = {
        "success": claims.get("success"),
        "assignees": claims.get("assignees", []),
        "assignments": len(claims.get("assignments", [])),
        "claims": len(claims.get("claims", [])),
        "maintainer_replied": claims.get("maintainer_replied"),
    }
    cited = resolve_cited_pull_requests(
        directory,
        references=_citable(claims, slug=slug, directory=directory),
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    record["cited_pull_requests"] = {
        "references": cited["references"],
        "resolved": cited["resolved"],
        "skipped": cited["skipped"],
        "open": [row["number"] for row in cited["open"]],
        "merged": [row["number"] for row in cited["merged"]],
        "decided_by": cited["decided_by"],
        "detail": cited["detail"],
    }
    # Whether we may file here at all, before whether this issue is worth it.
    # getsentry/sentry-python closes a pull request whose issue no maintainer
    # answered, automatically, and labels it a guideline violation: the patch
    # is never read however good it is.
    # https://github.com/wolfgang-aura/Mailman/issues/99
    required = requires_prior_discussion(screen)
    record["prior_discussion"] = {
        "required": bool(required),
        "quote": required.get("quote") if required else None,
        "maintainer_replied": claims.get("maintainer_replied"),
    }
    thread_blocking: list[str] = []
    if required and claims.get("maintainer_replied") is False:
        thread_blocking.append(NO_MAINTAINER_REPLY)
    if cited["open"]:
        thread_blocking.append(OPEN_PULL_REQUEST)
    if cited["merged"]:
        thread_blocking.append(ALREADY_FIXED_UPSTREAM)
    if thread_blocking:
        details = []
        if NO_MAINTAINER_REPLY in thread_blocking:
            details.append(
                f"{slug} requires a maintainer to have answered the issue "
                f"before a pull request exists ({required.get('quote')!r}), and "
                "nobody who speaks for the project has replied on this one"
            )
        if cited["decided_by"]:
            details.append(cited["detail"])
        record.update(
            {
                "blocking": thread_blocking,
                "warnings": warnings,
                "verdict": "reject",
                "stages_skipped": ["duplicate-search", "prior-art"],
                "next": f"Do not open a run on {slug}#{number}: "
                + "; ".join(thread_blocking)
                + (f". {'. '.join(details)}" if details else ""),
            }
        )
        _store_prescreen(data_root, slug, number, record)
        return record
    # The narrow search is only as good as its terms. The typed symbols make
    # one query; each symbol read out of the issue body makes its own, so the
    # search no longer depends on the coordinator guessing the right name.
    typed = [symbol for symbol in symbols if symbol.strip()]
    from_body = [
        symbol
        for symbol in issue_symbols(_captured_body(directory))
        if symbol not in typed
    ]
    record["issue_symbols"] = from_body
    search = record_duplicate_search(
        directory,
        repository=slug,
        query=query or str(captured.get("title") or f"#{number}"),
        issue_number=number,
        symbols=typed,
        issue_symbols=from_body,
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
    assessment = assess_target(directory)
    blocking = [code for code in assessment.blocking if code in DECIDABLE]
    record["blocking"] = blocking
    record["warnings"] = warnings + [
        code for code in assessment.warnings if code in DECIDABLE
    ]
    record["open_attempts"] = [row.get("number") for row in assessment.open_attempts]
    record["merged_attempts"] = [
        row.get("number") for row in assessment.merged_attempts
    ]
    record["closed_attempts"] = [
        row.get("number") for row in assessment.closed_attempts
    ]
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
            f"the pre-screen for {slug}#{number} predates a screening question "
            f"it never answered; run `mailman prescreen {slug}#{number}` again"
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
