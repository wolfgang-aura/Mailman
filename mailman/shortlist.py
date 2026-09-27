"""Rank the issues a screen found workable by who asked for them and when.

Across the last two hunts, 72 of 106 pre-screened issues died because somebody
already had an open pull request. The shortlist was the newest unclaimed
issues on the tracker, which is exactly where every other contributor looks
first. An issue a maintainer has asked for is a different population: the
project has said it wants the change and nobody has taken it up, so the
pre-screen is less likely to find a rival and the maintainer is more likely to
review what arrives. See https://github.com/wolfgang-aura/Mailman/issues/102.

The order is a strict priority, not a blend: an issue a maintainer filed or
answered outranks one nobody on the project has spoken on (#135), then any
invited issue outranks any uninvited one, then a recent issue outranks an old
one, then an issue nobody has cited a pull request against outranks one with
an attempt on record. The
weights are powers of two so the score is that priority and nothing else, and
the reasons travel beside the score so a coordinator can see why an issue is
first rather than trust that it is.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from mailman.claims import (
    MAINTAINER_ASSOCIATIONS,
    invites_pull_request,
    is_maintainer_invitation,
)

#: A maintainer asked for the pull request: in a comment, in the report when
#: the reporter is a maintainer, or with a label that says so.
MAINTAINER_INVITED = "maintainer-invited"
#: Opened, or last written on by a maintainer, within `RECENT_DAYS`.
RECENT = "recent"
#: No pull request is linked to or cited by the issue, open, merged or dormant.
NO_LINKED_PR = "no-linked-pr"

#: Reported from outside, older than the grace window, and nobody who speaks
#: for the project has answered. A demotion, not a reason to rank higher: its
#: weight outweighs every other reason together, so any acknowledged issue
#: ranks above any unacknowledged one. urllib3#5053 spent a full run on an
#: issue in this state and closed `not_planned`. Mailman #116.
UNACKNOWLEDGED = "unacknowledged"

RECENT_DAYS = 14
#: How long an outside report may wait for a maintainer before it counts as
#: unacknowledged. The same fortnight as `screen.FIRST_RESPONSE_DAYS`.
ACKNOWLEDGEMENT_GRACE_DAYS = 14

REASONS = (MAINTAINER_INVITED, RECENT, NO_LINKED_PR, UNACKNOWLEDGED)
_WEIGHTS = {MAINTAINER_INVITED: 4, RECENT: 2, NO_LINKED_PR: 1, UNACKNOWLEDGED: -8}

#: Label spellings that ask for outside work. Compared with hyphens,
#: underscores and case folded, because `help-wanted`, `help_wanted` and
#: `Help Wanted` all name the same thing.
INVITATION_LABELS = frozenset(
    {"help wanted", "good first issue", "contributions welcome", "pr welcome"}
)


def _label_names(labels: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(labels, list):
        return names
    for entry in labels:
        if isinstance(entry, dict):
            names.append(str(entry.get("name") or ""))
        elif isinstance(entry, str):
            names.append(entry)
    return names


def _normal(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").replace("_", " ").split())


def label_invites(labels: Any) -> bool:
    """Say whether one of these labels asks for a pull request."""
    return any(_normal(name) in INVITATION_LABELS for name in _label_names(labels))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_recent(
    *stamps: Any, now: datetime | None = None, days: int = RECENT_DAYS
) -> bool:
    """Say whether any of these timestamps falls inside the recent window."""
    moment = now or datetime.now(UTC)
    threshold = moment - timedelta(days=days)
    return any(
        parsed is not None and parsed >= threshold
        for parsed in (_timestamp(stamp) for stamp in stamps)
    )


def is_unacknowledged(
    *,
    reporter_association: Any,
    maintainer_answered: bool,
    created_at: Any,
    now: datetime | None = None,
    days: int = ACKNOWLEDGEMENT_GRACE_DAYS,
) -> bool:
    """Say whether an outside report has waited past the grace window unanswered.

    An issue whose age cannot be read is not called unacknowledged: the
    warning is a claim about time, and it needs a timestamp to make it.
    """
    if reporter_association in MAINTAINER_ASSOCIATIONS or maintainer_answered:
        return False
    opened = _timestamp(created_at)
    if opened is None:
        return False
    return (now or datetime.now(UTC)) - opened > timedelta(days=days)


def ranking(
    *,
    invited: bool,
    recent: bool,
    no_linked_pull_request: bool,
    unacknowledged: bool = False,
) -> dict[str, Any]:
    """The score and its reasons, in the order the reasons rank."""
    reasons = [
        code
        for code, held in (
            (MAINTAINER_INVITED, invited),
            (RECENT, recent),
            (NO_LINKED_PR, no_linked_pull_request),
            (UNACKNOWLEDGED, unacknowledged),
        )
        if held
    ]
    return {"score": sum(_WEIGHTS[code] for code in reasons), "reasons": reasons}


def rank_issue(
    issue: dict[str, Any],
    comments: Iterable[dict[str, Any]] = (),
    *,
    linked_pull_requests: bool,
    maintainer_touched_at: Any = None,
    now: datetime | None = None,
    thread_read: bool = True,
) -> dict[str, Any]:
    """Rank one issue from its list row, its thread and what cites it.

    `issue` is the row GitHub's issues list returns, so the report's own
    author association and body are read from it. `comments` is the thread,
    when it was read; an issue past the thread cap is ranked on its row alone,
    and is never called unacknowledged, because nobody read whether it was.
    """
    thread = [comment for comment in comments if isinstance(comment, dict)]
    invited = (
        label_invites(issue.get("labels"))
        or is_maintainer_invitation(issue)
        or any(is_maintainer_invitation(comment) for comment in thread)
    )
    recent = is_recent(issue.get("created_at"), maintainer_touched_at, now=now)
    unacknowledged = thread_read and is_unacknowledged(
        reporter_association=issue.get("author_association"),
        # An invitation, by label or in words, is a maintainer's answer.
        maintainer_answered=invited
        or any(
            comment.get("author_association") in MAINTAINER_ASSOCIATIONS
            for comment in thread
        ),
        created_at=issue.get("created_at"),
        now=now,
    )
    return ranking(
        invited=invited,
        recent=recent,
        no_linked_pull_request=not linked_pull_requests,
        unacknowledged=unacknowledged,
    )


def maintainer_engaged(row: dict[str, Any]) -> bool:
    """Whether a maintainer filed the issue or has written in its thread."""
    return bool(row.get("maintainer_filed") or row.get("maintainer_replied"))


def sort_shortlist(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Maintainer-engaged first, then highest score, the newer issue, the lower number.

    An issue nobody on the project has spoken on is untriaged, and an
    untriaged run never counts ready, so engagement outranks the score.
    https://github.com/wolfgang-aura/Mailman/issues/135
    """
    return sorted(
        rows,
        key=lambda row: (
            not maintainer_engaged(row),
            -int(row.get("score") or 0),
            int(row.get("age_days") if row.get("age_days") is not None else 10**6),
            int(row.get("number") or 0),
        ),
    )


def render_shortlist(rows: list[dict[str, Any]], *, limit: int = 10) -> list[str]:
    """One line per issue, top of the list first, the rest counted not printed."""
    lines: list[str] = []
    for row in rows[:limit]:
        engaged = [
            name
            for name, held in (
                ("maintainer-filed", row.get("maintainer_filed")),
                ("maintainer-replied", row.get("maintainer_replied")),
            )
            if held
        ]
        reasons = ", ".join(engaged + list(row.get("reasons") or [])) or "-"
        age = row.get("age_days")
        age_text = f"{age}d" if age is not None else "?d"
        lines.append(
            f"    #{row.get('number'):<6} {age_text:>5}  {reasons:<36} "
            f"{str(row.get('title') or '')[:60]}"
        )
    if len(rows) > limit:
        lines.append(f"    ... {len(rows) - limit} more in the screen record")
    return lines


__all__ = [
    "ACKNOWLEDGEMENT_GRACE_DAYS",
    "INVITATION_LABELS",
    "MAINTAINER_INVITED",
    "NO_LINKED_PR",
    "REASONS",
    "RECENT",
    "RECENT_DAYS",
    "UNACKNOWLEDGED",
    "invites_pull_request",
    "is_recent",
    "is_unacknowledged",
    "label_invites",
    "maintainer_engaged",
    "rank_issue",
    "ranking",
    "render_shortlist",
    "sort_shortlist",
]
