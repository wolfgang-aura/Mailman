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
from mailman.executor import execute
from mailman.issue import capture_issue_from_github
from mailman.maintainers import maintainer_logins
from mailman.prior_art import collect_prior_art, resolve_cited_pull_requests
from mailman.screen import (
    DIRECT_PUSH_LIMIT,
    direct_push_share,
    forbids_duplicate_pull_requests,
    load_screen,
    pull_request_base,
    requires_prior_discussion,
    refusal_stands,
    screen_is_current,
    screen_shortlist,
)
from mailman.shortlist import (
    ACKNOWLEDGEMENT_GRACE_DAYS,
    CLOSED_TO_OUTSIDERS,
    is_recent,
    is_unacknowledged,
    label_closes,
    label_invites,
    ranking,
)
from mailman.submission import (
    duplicate_is_related,
    partition_duplicates,
    record_duplicate_search,
    related_duplicates,
)
from mailman.targeting import (
    attempt_is_reaffirmed,
    ALREADY_FIXED_UPSTREAM,
    CITED_MERGED_BEFORE_ISSUE,
    CITED_MERGED_ELSEWHERE,
    CITED_MERGED_IN_BODY,
    DUPLICATE_FORBIDDEN_OPEN_ATTEMPT,
    ISSUE_ASSIGNED,
    MAINTAINER_CLOSED_ATTEMPT,
    MAINTAINER_CLOSED_ATTEMPT_REAFFIRMED,
    MAINTAINER_OWNED_FIX,
    MAINTAINER_PENDING_FIX,
    MAINTAINER_REMARK_ELSEWHERE,
    MERGED_BEFORE_ISSUE_DAYS,
    NO_CLAIM_CHECK,
    NO_DUPLICATE_SEARCH,
    NO_MAINTAINER_REPLY,
    OPEN_PULL_REQUEST,
    STALE_PRIOR_ATTEMPT,
    UNACKNOWLEDGED_ATTEMPTS,
    UNACKNOWLEDGED_CLAIM,
    STALE_CLAIM,
    WORK_HANDED_OVER,
    assess_target,
    own_pull_request,
    partition_duplicate_blocked,
)
from mailman.toolchain import resolve_tool

#: 4 reads the pull requests the issue's own thread names; 5 asks whether the
#: repository requires a maintainer reply before a pull request exists; 6 asks
#: how long each cited attempt has been dormant; 7 asks whether the repository
#: rejects duplicate pull requests, which decides whether a dormant one may be
#: superseded at all; 8 asks who closed each closed attempt; 9 asks whether
#: anybody who speaks for the project has acknowledged the report; 10 asks
#: whether a maintainer reserved the issue for human contributors; 11 asks
#: whether a maintainer left a design choice open in the thread. A screen
#: written before any of them never asked the question, so `check` sends it
#: back.
#: 12 asks whether the project's latest word disputes the report, and reads
#: labels that send it upstream or wait on the reporter. Mailman #193.
#: 13 reads maintainers from the repository screen's recorded set as well as
#: from `author_association`, and refuses a maintainer's own parked fix.
#: Mailman #203.
#: 14 records each searched attempt's author association and refuses a
#: project voice's closed fix that no maintainer rejected. Mailman #199.
PRESCREEN_SCHEMA_VERSION = 14
ISSUE_SCREENS = "issue-screens"
#: A pre-screen filters a shortlist; it is not the filing gate. The run stage
#: still re-runs the duplicate search under its own one-hour limit, and
#: `hunt finish` still refuses stale evidence. A day is long enough to screen a
#: shortlist in the morning and work it in the afternoon.
PRESCREEN_HOURS = 24
ISSUE_UNREADABLE = "issue-unreadable"
#: A pull request the thread cites that `gh` could not read, for a reason
#: other than GitHub saying it is not one. It may be the open rival, so the
#: issue is held until it can be read. Mailman #344.
CITED_UNREAD = "cited-pull-request-unread"
ISSUE_NOT_OPEN = "issue-not-open"
ISSUE_NOT_BOUNDED_FIX = "issue-not-bounded-fix"
#: The maintainers have said the design is not settled. A patch on such an
#: issue is pressure, not help: py-pdf/pypdf#4105 was filed on #4035 under
#: `needs-discussion`, and the maintainer asked why a PR existed at all.
#: The only upstream write here is a comment with evidence. Mailman #124.
ISSUE_UNDER_DISCUSSION = "issue-under-discussion"
#: The fix is small enough that the maintainer writes it rather than reviews it,
#: in a repository whose maintainers push to the default branch. Blocking.
TRIVIAL_FIX_DIRECT_PUSH = "trivial-fix-direct-push-repository"
#: The fix looks trivial but the repository reviews what it merges. A warning:
#: a typo in a project that runs everything through a pull request is still a
#: pull request somebody has to open.
TRIVIAL_FIX = "trivial-fix"
TRIVIAL = "trivial"
#: Reported from outside, older than the grace window, and nobody who speaks
#: for the project has replied or labelled it. A warning, not a block: it is
#: printed with the verdict and ranks the issue below acknowledged ones, so the
#: risk is seen before a run is opened. urllib3#5053 spent a full run in this
#: state and closed `not_planned`. https://github.com/wolfgang-aura/Mailman/issues/116
UNACKNOWLEDGED_ISSUE = "unacknowledged-issue"
#: Reported from outside, still inside the grace window, and nobody who speaks
#: for the project has replied or labelled it yet. A warning: a run on it ends
#: in an ASK decision, which counts as ready_to_ask and never toward the hunt's
#: quota. agentscope#3059 passed silently in this state. Mailman #287.
UNTRIAGED_ASK_FIRST = "untriaged-ask-first"
UNKNOWN = "unknown"
#: Somebody in the thread says the bug is gone on main or the latest release.
#: A warning: the claim is often right and cheap to check before `init-run`.
#: pylint#10032 passed with "no longer reproduces on current `main`". Mailman #236.
REPORTED_FIXED_ON_MAIN = "reported-fixed-on-main"
#: The coordinator read the thread and turned the issue down for a reason no
#: rule decides yet. Recorded so the next hunt does not read the same thread:
#: hunt 20260927T212801Z-67a9aa rejected marimo#6250, zarr-python#2706 and
#: four others by hand, and `hunt targets` offered every one of them again.
REJECTED_BY_COORDINATOR = "rejected-by-coordinator"
#: A maintainer wrote in the thread that the issue is for human contributors,
#: or that agent-written pull requests may be rejected. Blocking: our pull
#: request is the one they described. beetbox/beets#6984 said so and had three
#: closed attempts; the hunt reached it by hand after the prescreen passed it.
ISSUE_RESERVED_FOR_HUMANS = "issue-reserved-for-humans"
#: A maintainer wrote in the thread that the design is open ("not sure how we
#: should", "one option is... another", "we could add a config option") and no
#: later maintainer comment settled it. The label check misses these because
#: nobody labelled them: zarr-python#2706, responses#744 and marimo#6250 were
#: each turned down by hand after passing. Mailman #124.
DESIGN_UNDECIDED = "design-undecided"
#: A project voice turned the report down ("works as intended", "I don't think
#: we want to implement this") and no later one invited the change. pint#2060
#: and filesystem_spec#1741 each passed and were turned down by hand. Mailman #174.
MAINTAINER_DECLINED = "maintainer-declined"
#: The project's latest word asks for logs, a retry or a reproducer, says it
#: could not reproduce, or sends the report to another project, and nobody
#: who speaks for it confirmed the bug since. huggingface_hub#3974, #3871 and
#: #3795 passed in one batch and none was workable. Mailman #193.
MAINTAINER_DISPUTED = "maintainer-disputed"
#: A label that says the bug is somebody else's, or that the project is still
#: waiting to verify it: plotnine#975 `upstream-bug`, celery#9901
#: `Status: Needs Verification`. Mailman #193.
ISSUE_NOT_TRIAGED_HERE = "issue-not-triaged-here"
_NOT_TRIAGED_LABEL = re.compile(
    r"upstream|third[- ]party"
    r"|needs?[- :]*(?:verification|verify|info|more[- ]info|feedback|repro"
    r"|reproduction|reproducer|triage|response|confirmation)"
    r"|awaiting|waiting[- ]for|more[- ]info[- ]needed|cannot[- ]reproduce"
    # commitizen `wait-for-response`; its `wait-for-implementation` means
    # the maintainers agree, so only the reporter-facing ones. Mailman #231.
    r"|wait[- ]for[- ](?:response|reply|feedback|info|author|reporter|op)\b"
    r"|can'?t[- ]reproduce|not[- ]reproducible|unconfirmed|wont[- ]?fix|invalid",
    re.IGNORECASE,
)
#: The issue body (usually the repository's template) says a pull request
#: needs a maintainer-applied label, and the issue does not carry it. mlflow's
#: template asks for `ready` and warns that other pull requests "may be
#: automatically closed"; mlflow#26266 passed without it. Mailman #229.
ISSUE_LACKS_REQUIRED_LABEL = "issue-lacks-required-label"
_REQUIRED_LABEL_RE = re.compile(
    r"appl(?:y|ied|ies)\s+(?:the\s+)?[`'\"]([^`'\"\n]{1,40})[`'\"]\s+label",
    re.IGNORECASE,
)


def required_labels(body: str) -> list[str]:
    """Labels the issue body says a maintainer must apply before a PR."""
    found: list[str] = []
    for match in _REQUIRED_LABEL_RE.finditer(body or ""):
        label = match.group(1).strip()
        if label and label.lower() not in (item.lower() for item in found):
            found.append(label)
    return found


#: The repository's own screen refused it. An issue there is not a candidate
#: however clean its thread: alembic#1390 and podman-compose#1549 both passed
#: this prescreen in repositories that failed freshness and responsiveness, and
#: the pool they seemed to fill was empty. Mailman #152.
REPOSITORY_SCREEN_FAILED = "repository-screen-failed"
#: A feature request a maintainer asked for in the thread ("PR welcome",
#: "happy to merge"), or labelled `help wanted` / `good first issue`. A
#: warning, not a block: the maintainer bounded the work
#: and invited it, but the diff is larger than a bug fix. Without the
#: invitation the feature label still blocks. Mailman #155.
INVITED_ENHANCEMENT = "invited-enhancement"
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
#: Feature labels a maintainer's invitation can lift; the rest of
#: `_NON_FIX_LABELS` never names a change somebody can simply write.
_FEATURE_LABELS = frozenset(
    {"enhancement", "feature", "feature request", "feature-request"}
)
_NON_FIX_LABELS = _FEATURE_LABELS | frozenset(
    {
        "meta",
        "project",
        "question",
        "tracking",
    }
)
_DISCUSSION_LABELS = frozenset(
    {
        "needs-discussion",
        "needs discussion",
        "needs-decision",
        "needs decision",
        "discussion",
        "design",
        "design-decision",
        "rfc",
        "proposal",
        "undecided",
    }
)
#: A label description saying the project has not decided yet. nicegui's
#: `analysis` reads "Status: Requires team/community input". Mailman #280.
_DISCUSSION_DESCRIPTION = re.compile(
    r"\b(?:requires?|needs?|awaiting|waiting (?:for|on))\b[^.;]{0,40}"
    r"\b(?:input|discussion|decision|consensus|feedback from (?:the )?(?:team|maintainers))\b",
    re.IGNORECASE,
)
#: What this stage can decide with public GitHub state and a few API calls.
#: Reproduction needs a clone, and target intel comes from `screen-target`.
DECIDABLE = (
    NO_DUPLICATE_SEARCH,
    # A claim check that could not run is no evidence the issue is free; a
    # rate-limited batch passed issues with none. Mailman #342.
    NO_CLAIM_CHECK,
    NO_MAINTAINER_REPLY,
    OPEN_PULL_REQUEST,
    ALREADY_FIXED_UPSTREAM,
    UNACKNOWLEDGED_ATTEMPTS,
    ISSUE_ASSIGNED,
    WORK_HANDED_OVER,
    UNACKNOWLEDGED_CLAIM,
    STALE_CLAIM,
    STALE_PRIOR_ATTEMPT,
    DUPLICATE_FORBIDDEN_OPEN_ATTEMPT,
    MAINTAINER_CLOSED_ATTEMPT,
    MAINTAINER_OWNED_FIX,
    MAINTAINER_PENDING_FIX,
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
    # Not `with_suffix`: `plotly.py` would read as the extension, and every
    # issue in that repository shared one file. Mailman #282.
    directory = prescreen_directory(data_root, slug, number)
    return directory.with_name(f"{directory.name}.json")


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


#: A classification scope in front of a label name: `status: needs
#: discussion`, `type: enhancement`, `kind/feature`. pypdf#4105 got past an
#: exact-name check this way (#334). Only these scopes: `area/design` names a
#: subject, not a decision still open.
_LABEL_SCOPE = re.compile(
    r"^(?:type|kind|status|state|stage|triage|category|resolution)\s*[:/]\s*"
)


def _label_name(label: object) -> str:
    """A label with its scope dropped and its separators read as spaces."""
    name = _LABEL_SCOPE.sub("", str(label).strip().lower())
    return re.sub(r"[\s_-]+", " ", name).strip()


def _label_names(captured: dict[str, Any]) -> set[str]:
    return {_label_name(label) for label in captured.get("labels") or []}


def _named(labels: frozenset[str]) -> frozenset[str]:
    return frozenset(_label_name(label) for label in labels)


def _feature_only(captured: dict[str, Any]) -> bool:
    """Say whether the issue's only non-fix labels are feature labels."""
    labels = _label_names(captured)
    return bool(labels & _named(_FEATURE_LABELS)) and not (
        labels & _named(_NON_FIX_LABELS - _FEATURE_LABELS)
    )


def _issue_blocking(captured: dict[str, Any]) -> list[str]:
    blocking: list[str] = []
    if captured.get("success") is not True:
        blocking.append(ISSUE_UNREADABLE)
    elif str(captured.get("state") or "").upper() != "OPEN":
        blocking.append(ISSUE_NOT_OPEN)
    labels = {str(label).strip().lower() for label in captured.get("labels") or []}
    if _label_names(captured) & _named(_NON_FIX_LABELS):
        blocking.append(ISSUE_NOT_BOUNDED_FIX)
    descriptions = (captured.get("label_descriptions") or {}).values()
    if _label_names(captured) & _named(_DISCUSSION_LABELS) or any(
        _DISCUSSION_DESCRIPTION.search(str(text)) for text in descriptions
    ):
        blocking.append(ISSUE_UNDER_DISCUSSION)
    if any(_NOT_TRIAGED_LABEL.search(label) for label in labels):
        blocking.append(ISSUE_NOT_TRIAGED_HERE)
    # haystack#13018 `handled internally`. Mailman #416.
    if label_closes(sorted(labels)) or any(
        CLOSED_TO_OUTSIDERS.search(str(text)) for text in descriptions
    ):
        blocking.append(ISSUE_RESERVED_FOR_HUMANS)
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


def shortlist_engagement(
    screen: dict[str, Any] | None, number: int
) -> dict[str, Any] | None:
    """What the repository screen's shortlist row recorded about maintainers.

    None when the screen has no row for this issue. A row from a screen
    written before f94d449 carries neither flag, and a flag the screen could
    not read is None; only a True is evidence. `engaged` is that evidence.
    https://github.com/wolfgang-aura/Mailman/issues/135
    """
    for row in screen_shortlist(screen):
        if row.get("number") == number:
            filed = row.get("maintainer_filed")
            replied = row.get("maintainer_replied")
            labelled = row.get("maintainer_labelled")
            return {
                "maintainer_filed": filed,
                "maintainer_replied": replied,
                "maintainer_labelled": labelled,
                "engaged": filed is True or replied is True or labelled is True,
            }
    return None


def _acknowledgement(
    claims: dict[str, Any], *, shortlist_engaged: bool = False
) -> dict[str, Any]:
    """Whether anybody who speaks for the project has answered this report.

    `shortlist_engaged` is the screen's record that a maintainer filed or
    replied on the issue. It can only clear the warning; without it the
    thread this stage read decides, as before.
    """
    unacknowledged = bool(claims.get("success")) and is_unacknowledged(
        reporter_association=claims.get("reporter_association"),
        maintainer_answered=bool(
            shortlist_engaged
            or claims.get("maintainer_replied")
            or claims.get("maintainer_labelled")
            or claims.get("invitations")
        ),
        created_at=claims.get("issue_created_at"),
        reporter_is_maintainer=bool(claims.get("reporter_is_maintainer")),
    )
    # The same question with no grace window: a fresh report nobody answered
    # is not yet overdue, but it is still untriaged. Mailman #287.
    untriaged = (
        bool(claims.get("success"))
        and not unacknowledged
        and is_unacknowledged(
            reporter_association=claims.get("reporter_association"),
            maintainer_answered=bool(
                shortlist_engaged
                or claims.get("maintainer_replied")
                or claims.get("maintainer_labelled")
                or claims.get("invitations")
            ),
            created_at=claims.get("issue_created_at"),
            days=0,
            reporter_is_maintainer=bool(claims.get("reporter_is_maintainer")),
        )
    )
    return {
        "unacknowledged": unacknowledged,
        "untriaged": untriaged,
        "reporter_association": claims.get("reporter_association"),
        "maintainer_replied": claims.get("maintainer_replied"),
        "maintainer_labelled": bool(claims.get("maintainer_labelled")),
        "shortlist_engaged": shortlist_engaged,
        "issue_created_at": claims.get("issue_created_at"),
        "grace_days": ACKNOWLEDGEMENT_GRACE_DAYS,
        "detail": (
            f"reported from outside the project "
            f"({claims.get('reporter_association')}), older than "
            f"{ACKNOWLEDGEMENT_GRACE_DAYS} days, and no owner, member or "
            "collaborator has replied or labelled it. Nobody who can speak for "
            "the project has said they want this fixed; urllib3#5053 spent a "
            "full run in this state and closed not_planned"
            if unacknowledged
            else None
        ),
    }


def _ranking(
    claims: dict[str, Any], *, labels: Sequence[Any], linked: bool,
    shortlist_engaged: bool = False,
) -> dict[str, Any]:
    """The same score the screen's shortlist carries, from what this stage read.

    The screen ranks from a list row and one page of comments; this stage has
    the whole thread and the resolved pull requests, so its answer can differ
    from the shortlist's and is the one to trust. The reasons are recorded so
    a coordinator reading two passes can see which one a maintainer asked for.
    """
    return ranking(
        invited=not label_closes(list(labels))
        and (bool(claims.get("invitations")) or label_invites(list(labels))),
        recent=is_recent(
            claims.get("issue_created_at"), claims.get("maintainer_touched_at")
        ),
        no_linked_pull_request=not linked,
        unacknowledged=_acknowledgement(
            claims, shortlist_engaged=shortlist_engaged
        )["unacknowledged"],
    )


def is_fresh(record: dict[str, Any], *, hours: int = PRESCREEN_HOURS) -> bool:
    try:
        screened = datetime.fromisoformat(record["screened_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now(UTC) - screened < timedelta(hours=hours)


def _live_maintainers(
    slug: str, executable: str | None, timeout_seconds: float
) -> frozenset[str]:
    """Who merged the repository's recent pull requests, read with one call."""
    from mailman.maintainers import MERGERS_QUERY, mergers

    owner, _, name = slug.partition("/")
    result = execute(
        [
            executable or resolve_tool(Path.cwd(), "gh"),
            "api",
            "graphql",
            "-f",
            f"query={MERGERS_QUERY % (owner, name)}",
        ],
        working_directory=Path.cwd(),
        timeout_seconds=timeout_seconds,
    )
    if result.timed_out or result.exit_code != 0:
        return frozenset()
    try:
        return frozenset(mergers(json.loads(result.stdout or "{}")))
    except json.JSONDecodeError:
        return frozenset()


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
    # Who merges here, as the repository screen recorded it. GitHub calls a
    # maintainer with a private membership CONTRIBUTOR. Empty without a
    # screen, which leaves every check on the association alone. Mailman #203.
    stored_screen = load_screen(data_root, slug)
    maintainers = maintainer_logins(stored_screen)
    # A screen written before #203 never read who merges; read it now rather
    # than call a private-member maintainer an outsider. Mailman #263.
    if isinstance(stored_screen, dict) and "maintainer_logins" not in stored_screen:
        maintainers = _live_maintainers(slug, executable, timeout_seconds)
    captured = capture_issue_from_github(
        directory,
        issue_url=issue_url,
        executable=executable,
        timeout_seconds=timeout_seconds,
        maintainers=maintainers,
    )
    record: dict[str, Any] = {
        "schema_version": PRESCREEN_SCHEMA_VERSION,
        "screened_at": datetime.now(UTC).isoformat(),
        "repository": slug,
        "issue_number": number,
        "symbols": [symbol for symbol in symbols if symbol.strip()],
        "workspace": str(directory),
        "stale_attempts": [],
        "duplicate_blocked_attempts": [],
        "maintainer_closed_attempts": [],
        "maintainer_owned_attempts": [],
        "maintainer_pending_attempts": [],
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
    labels_present = {
        str(label).strip().lower() for label in captured.get("labels") or []
    }
    missing = [
        label
        for label in required_labels(_captured_body(directory))
        if label.lower() not in labels_present
    ]
    if missing:
        issue_blocking.append(ISSUE_LACKS_REQUIRED_LABEL)
        record["required_labels_missing"] = missing
    screen = load_screen(data_root, slug)
    record["maintainer_logins_known"] = len(maintainers)
    # stanza takes pull requests only against `dev`, 91 commits ahead of
    # `main`; a run pinned to `main` patches the wrong tree. Mailman #258.
    record["pull_request_base"] = pull_request_base(screen)
    warnings: list[str] = []
    if screen and screen.get("success") and screen.get("verdict") != "pass":
        # A refusal read under older windows may not stand, and the hunt
        # re-reads it before a run counts. The issue checks are cheaper than
        # a re-screen, so they go first. Mailman #383. A refusal on a gate
        # no window reads, such as policy, stands regardless. Mailman #387.
        if refusal_stands(screen):
            issue_blocking.append(REPOSITORY_SCREEN_FAILED)
        else:
            warnings.append(
                f"The screen of {slug} failed under older windows; run "
                f"`mailman screen-target {slug} --refresh` before init-run."
            )
    share = direct_push_share(screen)
    shortlisted = shortlist_engagement(screen, number)
    shortlist_engaged = bool(shortlisted and shortlisted["engaged"])
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
    # A feature label alone waits for the thread: a maintainer who wrote "PR
    # welcome" has bounded and invited the work. Anything else blocking
    # still ends the screen here, feature label included. Mailman #155.
    feature_pending = issue_blocking == [ISSUE_NOT_BOUNDED_FIX] and _feature_only(
        captured
    )
    if feature_pending:
        issue_blocking = []
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
                )
                + (
                    ". The issue asks for a maintainer-applied label before a "
                    "pull request and lacks "
                    + ", ".join(
                        f"`{label}`" for label in record["required_labels_missing"]
                    )
                    if ISSUE_LACKS_REQUIRED_LABEL in issue_blocking
                    else ""
                )
                + (
                    (
                        f". The recorded screen of {slug} failed; "
                        f"`mailman screen-target {slug} --refresh` re-reads it"
                        if screen_is_current(screen)
                        else f". The screen of {slug} failed under older windows; "
                        f"`mailman screen-target {slug} --refresh` may change it"
                    )
                    if REPOSITORY_SCREEN_FAILED in issue_blocking
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
        directory,
        executable=executable,
        timeout_seconds=timeout_seconds,
        maintainers=maintainers,
    )
    record["claims"] = {
        "success": claims.get("success"),
        "assignees": claims.get("assignees", []),
        "assignments": len(claims.get("assignments", [])),
        "claims": len(claims.get("claims", [])),
        "invitations": len(claims.get("invitations", [])),
        "agent_exclusions": claims.get("agent_exclusions", []),
        "design_undecided": claims.get("design_undecided", []),
        "declined": claims.get("declined", []),
        "disputed": claims.get("disputed"),
        "maintainer_replied": claims.get("maintainer_replied"),
        "maintainer_touched_at": claims.get("maintainer_touched_at"),
        "remarks_elsewhere": claims.get("remarks_elsewhere", []),
    }
    if claims.get("remarks_elsewhere"):
        warnings.append(MAINTAINER_REMARK_ELSEWHERE)
    # The shortlist row already says whether a maintainer filed or answered
    # the issue. A True there is triage evidence even when this read of the
    # thread misses it; its absence changes nothing. Mailman #135.
    record["shortlist_engagement"] = shortlisted
    record["acknowledgement"] = _acknowledgement(
        claims, shortlist_engaged=shortlist_engaged
    )
    if record["acknowledgement"]["unacknowledged"]:
        warnings.append(UNACKNOWLEDGED_ISSUE)
    if record["acknowledgement"]["untriaged"]:
        warnings.append(UNTRIAGED_ASK_FIRST)
    record["reported_fixed"] = claims.get("reported_fixed")
    if record["reported_fixed"]:
        warnings.append(REPORTED_FIXED_ON_MAIN)
    cited = resolve_cited_pull_requests(
        directory,
        references=_citable(claims, slug=slug, directory=directory),
        executable=executable,
        timeout_seconds=timeout_seconds,
        repository=slug,
        issue_number=number,
        maintainers=maintainers,
    )
    record["cited_pull_requests"] = {
        "references": cited["references"],
        "resolved": cited["resolved"],
        "skipped": cited["skipped"],
        "open": [row["number"] for row in cited["open"]],
        "merged": [row["number"] for row in cited["merged"]],
        "merged_in_body": [
            row["number"] for row in cited["merged"] if row.get("in") == "body"
        ],
        "stale": [row["number"] for row in cited["stale"]],
        "maintainer_closed": [row["number"] for row in cited["maintainer_closed"]],
        "maintainer_owned": [row["number"] for row in cited["maintainer_owned"]],
        "decided_by": cited["decided_by"],
        "detail": cited["detail"],
    }
    # Whether a dormant attempt may be superseded at all is the repository's
    # rule, not ours. urllib3 rejects a second pull request for an issue
    # without reading it, and offering to supersede a dormant one there is the
    # contribution they close.
    duplicate_rule = forbids_duplicate_pull_requests(screen)
    record["duplicate_policy"] = {
        "forbidden": bool(duplicate_rule),
        "quote": duplicate_rule.get("quote") if duplicate_rule else None,
    }
    # A dormant attempt is prior art the run carries, not a reason to refuse
    # the target. `cited["open"]` already excludes them.
    stale_cited, blocked_cited = partition_duplicate_blocked(
        list(cited["stale"]), forbids_duplicates=bool(duplicate_rule)
    )
    record["stale_attempts"] = stale_cited
    record["duplicate_blocked_attempts"] = list(blocked_cited)
    # An attempt a maintainer closed is a rejection of the change, not a
    # dormant branch. skfolio#307 and wagtail#14384 passed this stage as
    # supersedable stale attempts and were neither.
    # Unless a maintainer confirmed the issue after closing it: then the
    # closure turned down that change, not the bug. Python-Markdown#1643.
    # Mailman #378.
    record["reaffirmed_closed_attempts"] = [
        row
        for row in cited["maintainer_closed"]
        if attempt_is_reaffirmed(row, claims.get("maintainer_labelled"))
    ]
    record["maintainer_closed_attempts"] = [
        row
        for row in cited["maintainer_closed"]
        if row not in record["reaffirmed_closed_attempts"]
    ]
    if record["reaffirmed_closed_attempts"]:
        warnings.append(MAINTAINER_CLOSED_ATTEMPT_REAFFIRMED)
    # A maintainer's own closed fix for this issue is parked work, not an
    # abandoned attempt. marimo#9862. Mailman #203.
    record["maintainer_owned_attempts"] = list(cited["maintainer_owned"])
    if stale_cited:
        warnings.append(STALE_PRIOR_ATTEMPT)
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
        "shortlist_engaged": shortlist_engaged,
    }
    thread_blocking: list[str] = []
    if feature_pending:
        # A `help wanted` or `good first issue` label is the same invitation
        # written as triage: only somebody with triage rights can apply it.
        if claims.get("invitations") or label_invites(captured.get("labels") or []):
            warnings.append(INVITED_ENHANCEMENT)
        else:
            thread_blocking.append(ISSUE_NOT_BOUNDED_FIX)
    if claims.get("agent_exclusions"):
        thread_blocking.append(ISSUE_RESERVED_FOR_HUMANS)
    if claims.get("design_undecided"):
        thread_blocking.append(DESIGN_UNDECIDED)
    if claims.get("declined"):
        thread_blocking.append(MAINTAINER_DECLINED)
    elif claims.get("disputed") and not claims.get("design_undecided"):
        thread_blocking.append(MAINTAINER_DISPUTED)
    if required and claims.get("maintainer_replied") is False and not shortlist_engaged:
        thread_blocking.append(NO_MAINTAINER_REPLY)
    if record["maintainer_closed_attempts"]:
        thread_blocking.append(MAINTAINER_CLOSED_ATTEMPT)
    if record["maintainer_owned_attempts"]:
        thread_blocking.append(MAINTAINER_OWNED_FIX)
    if blocked_cited:
        thread_blocking.append(DUPLICATE_FORBIDDEN_OPEN_ATTEMPT)
    # A pull request under another owner neither answers this issue nor
    # competes with a fix to it: pydata/xarray#10269 was refused as already
    # fixed because a downstream project merged its own workaround. Mailman
    # #172. A sibling under the same owner still counts: jsonschema#1497's fix
    # is open in python-jsonschema/referencing.
    def _here(row: dict[str, Any]) -> bool:
        owner = str(row.get("repository") or slug).split("/")[0]
        return owner.lower() == slug.split("/")[0].lower()

    if any(_here(row) for row in cited["open"]):
        thread_blocking.append(OPEN_PULL_REQUEST)
    if cited.get("success") is not True:
        thread_blocking.append(CITED_UNREAD)
    # A merged pull request the reporter names in the body is the cause or
    # the context of the report, not its fix: zauberzeug/nicegui#6339 was
    # "found while reviewing #6294, where it is out of scope", and #6331 says
    # #6329 "does not cover these paths". Both were refused as already fixed.
    # A fix is announced later, in a comment, and those still block. The
    # reproduction at the base commit is the check behind this one.
    # A sibling repository's merge that GitHub does not record as closing this
    # issue is a workaround there, not a fix here: shinyreact#306 for
    # py-shiny#2497. Mailman #206.
    this_issue = f"{slug}#{number}".lower()

    def _elsewhere(row: dict[str, Any]) -> bool:
        repository = str(row.get("repository") or slug).lower()
        return repository != slug.lower() and this_issue not in (row.get("closes") or [])

    merged_elsewhere = [
        row for row in cited["merged"] if _here(row) and _elsewhere(row)
    ]
    if merged_elsewhere:
        warnings.append(CITED_MERGED_ELSEWHERE)
    merged_here = [
        row for row in cited["merged"] if _here(row) and not _elsewhere(row)
    ]
    # One merged long before the issue was opened shipped in releases the
    # reporter already had: cause or context again, not a fix. Mailman #147.
    shipped = [
        row
        for row in merged_here
        if _merged_before_issue(row, claims.get("issue_created_at"))
    ]
    record["cited_pull_requests"]["merged_before_issue"] = [
        row["number"] for row in shipped
    ]
    merged_fixes = [
        row
        for row in merged_here
        if row.get("in") != "body" and row not in shipped
    ]
    # A fix that never cites the issue, and one behind an issue the body
    # defers to. Both runs of hunt 20260930T083934Z-941a77 died on these
    # after passing this stage. Mailman #257.
    uncited = uncited_merged_fixes(
        directory,
        slug=slug,
        title=str(captured.get("title") or ""),
        issue_created_at=claims.get("issue_created_at"),
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    record["uncited_merged_fixes"] = uncited
    completed = completed_cited_issues(
        directory,
        slug=slug,
        skipped=cited["skipped"],
        issue_created_at=claims.get("issue_created_at"),
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    record["completed_cited_issues"] = completed
    if merged_fixes or uncited["matches"] or completed:
        thread_blocking.append(ALREADY_FIXED_UPSTREAM)
    elif merged_here:
        warnings.append(
            CITED_MERGED_BEFORE_ISSUE
            if len(shipped) == len(merged_here)
            else CITED_MERGED_IN_BODY
        )
    labels = captured.get("labels") or []
    record["ranking"] = _ranking(
        claims,
        labels=labels,
        linked=any(
            cited[key]
            for key in (
                "open", "merged", "stale", "maintainer_closed", "maintainer_owned"
            )
        ),
        shortlist_engaged=shortlist_engaged,
    )
    if thread_blocking:
        details = []
        if ISSUE_RESERVED_FOR_HUMANS in thread_blocking:
            first = claims["agent_exclusions"][0]
            details.append(
                f"{first.get('author')} ({first.get('association')}) reserved "
                f"this issue for human work: {first.get('quote')!r}"
            )
        if DESIGN_UNDECIDED in thread_blocking:
            last = claims["design_undecided"][-1]
            details.append(
                f"{last.get('author')} ({last.get('association')}) left the "
                f"design open with {last.get('phrase')!r} ({last.get('quote')!r}) "
                "and no later maintainer comment settled it. Comment with "
                "evidence, or wait for a decision; do not open a run"
            )
        if MAINTAINER_DECLINED in thread_blocking:
            last = claims["declined"][-1]
            details.append(
                f"{last.get('author')} ({last.get('association')}) turned the "
                f"report down: {last.get('quote')!r}. Nobody who speaks for the "
                "project has invited the change since; do not open a run"
            )
        if MAINTAINER_DISPUTED in thread_blocking:
            details.append(
                f"the project's latest word disputes the report or waits on the "
                f"reporter: {claims['disputed']!r}. Nobody who speaks for it "
                "has confirmed the bug since; do not open a run"
            )
        if NO_MAINTAINER_REPLY in thread_blocking:
            details.append(
                f"{slug} requires a maintainer to have answered the issue "
                f"before a pull request exists ({required.get('quote')!r}), and "
                "nobody who speaks for the project has replied on this one"
            )
        if MAINTAINER_CLOSED_ATTEMPT in thread_blocking:
            named = ", ".join(
                f"#{row.get('number')} ({(row.get('closed_by') or {}).get('detail')})"
                for row in record["maintainer_closed_attempts"]
            )
            details.append(
                "a maintainer closed an earlier attempt at this issue: "
                f"{named}. Somebody who speaks for the project read that "
                "change and said no, so there is nothing dormant to supersede"
            )
        if MAINTAINER_OWNED_FIX in thread_blocking:
            named = ", ".join(
                f"#{row.get('number')} by {row.get('author')}"
                for row in record["maintainer_owned_attempts"]
            )
            details.append(
                f"a maintainer of {slug} wrote a fix for this issue and it was "
                f"closed without merging: {named}. That is the project's own "
                "work parked, not an abandoned attempt; a second pull request "
                "would race the maintainer. Ask on the issue whether they want "
                "help finishing it; do not open a run"
            )
        if DUPLICATE_FORBIDDEN_OPEN_ATTEMPT in thread_blocking:
            named = ", ".join(
                f"#{row.get('number')}" for row in blocked_cited
            )
            details.append(
                f"{slug} rejects a duplicate pull request for an issue that "
                f"already has one ({record['duplicate_policy']['quote']!r}), "
                f"and {named} is open. Dormant or not, it is still the pull "
                "request they count, so there is nothing to supersede"
            )
        if ISSUE_NOT_BOUNDED_FIX in thread_blocking:
            details.append(
                "it is labelled as a feature request and no maintainer in the "
                "thread asked for a pull request"
            )
        if CITED_UNREAD in thread_blocking:
            details.append(
                f"{cited['detail']}; `mailman prescreen {slug}#{number}` again "
                "once GitHub answers"
            )
        # The cited pull request is the reason only when it is what blocked.
        if cited["decided_by"] and (
            OPEN_PULL_REQUEST in thread_blocking
            or ALREADY_FIXED_UPSTREAM in thread_blocking
        ):
            details.append(cited["detail"])
        if uncited["matches"]:
            named = ", ".join(f"#{row['number']}" for row in uncited["matches"])
            details.append(
                f"merged since the issue opened, and naming every identifier "
                f"in its title ({', '.join(uncited['terms'])}): {named}. Read "
                "it; if it fixed this, the issue is done"
            )
        if completed:
            named = ", ".join(f"#{row['number']}" for row in completed)
            details.append(
                f"the body defers to {named}, closed as completed after this "
                "issue opened"
            )
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
        # The title query above ANDs every word of the title. The compact
        # one keeps its few distinctive terms, which is what found open
        # rival marimo#10915 after the title query missed it. #201.
        title=str(captured.get("title") or ""),
        executable=executable,
        timeout_seconds=timeout_seconds,
    )
    record["duplicate_search"] = {
        "success": search["success"],
        "complete": search["complete"],
        "matches": search.get("match_count", 0),
        "compact_terms": search.get("compact_terms", []),
        "failed_methods": search.get("failed_methods", []),
    }
    related = related_duplicates(search.get("matches"), issue_number=number)
    strong, _ = partition_duplicates(related, issue_number=number)
    # A closed attempt is only weak as a rival, but who closed it decides
    # whether it was rejected. stanza#1677 passed over two maintainer-closed
    # pull requests the search found, because nothing read their closer. #268.
    closed = [
        row
        for row in related
        if str(row.get("state") or "").lower() == "closed"
        and duplicate_is_related(row)
    ]
    numbers = sorted(
        {
            row["number"]
            for row in [*strong, *closed]
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
            maintainers=maintainers,
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
    assessment = assess_target(directory, forbids_duplicates=bool(duplicate_rule))
    blocking = [code for code in assessment.blocking if code in DECIDABLE]
    record["blocking"] = blocking
    combined = warnings + [
        code for code in assessment.warnings if code in DECIDABLE
    ]
    record["warnings"] = list(dict.fromkeys(combined))
    # The search and the issue's own thread can name the same dormant attempt.
    known = {row.get("number") for row in record["stale_attempts"]}
    record["stale_attempts"].extend(
        row for row in assessment.stale_attempts if row.get("number") not in known
    )
    blocked_known = {row.get("number") for row in record["duplicate_blocked_attempts"]}
    record["duplicate_blocked_attempts"].extend(
        row
        for row in assessment.duplicate_blocked_attempts
        if row.get("number") not in blocked_known
    )
    rejected_known = {
        row.get("number") for row in record["maintainer_closed_attempts"]
    }
    record["maintainer_closed_attempts"].extend(
        row
        for row in assessment.maintainer_closed_attempts
        if row.get("number") not in rejected_known
    )
    # A project voice's closed fix the search found: parked, not abandoned.
    # Mailman #199.
    record["maintainer_pending_attempts"] = list(
        assessment.maintainer_pending_attempts
    )
    record["open_attempts"] = [row.get("number") for row in assessment.open_attempts]
    record["merged_attempts"] = [
        row.get("number") for row in assessment.merged_attempts
    ]
    record["closed_attempts"] = [
        row.get("number") for row in assessment.closed_attempts
    ]
    # Re-ranked now that the search has had its say: an attempt the thread
    # never named is still a pull request on record.
    record["ranking"] = _ranking(
        claims,
        labels=labels,
        linked=bool(
            record["stale_attempts"]
            or record["duplicate_blocked_attempts"]
            or record["maintainer_closed_attempts"]
            or record["maintainer_pending_attempts"]
            or record["open_attempts"]
            or record["merged_attempts"]
            or record["closed_attempts"]
        ),
        shortlist_engaged=shortlist_engaged,
    )
    record["verdict"] = "reject" if blocking else "pass"
    if blocking:
        record["next"] = f"Do not open a run on {slug}#{number}: " + "; ".join(blocking)
        if MAINTAINER_PENDING_FIX in blocking:
            named = ", ".join(
                f"#{row.get('number')} by {row.get('author')}"
                for row in record["maintainer_pending_attempts"]
            )
            record["next"] += (
                f". Somebody who speaks for {slug} wrote a fix that was closed "
                f"without merging and that no maintainer rejected: {named}. "
                "That is the project's own work parked, not an abandoned "
                "attempt; ask on the issue whether they want help finishing it"
            )
        if NO_CLAIM_CHECK in blocking:
            record["next"] += (
                f". The issue thread could not be read ({claims.get('detail')}); "
                f"`mailman prescreen {slug}#{number}` again once GitHub answers"
            )
    else:
        record["next"] = (
            f"mailman init-run --repository https://github.com/{slug}.git "
            f"--issue https://github.com/{slug}/issues/{number} ..."
        )
        if record["pull_request_base"]:
            branch = record["pull_request_base"]["branch"]
            record["next"] += (
                f" -- with --base-commit at the head of `{branch}`, not the "
                f"default branch: {record['pull_request_base']['quote']!r}"
            )
        if UNACKNOWLEDGED_ISSUE in record["warnings"]:
            record["next"] += (
                f" -- but first weigh {UNACKNOWLEDGED_ISSUE}: "
                + record["acknowledgement"]["detail"]
            )
        if UNTRIAGED_ASK_FIRST in record["warnings"]:
            record["next"] += (
                f" -- but first weigh {UNTRIAGED_ASK_FIRST}: no owner, member "
                "or collaborator has replied or labelled this report yet, so a "
                "run on it decides ASK and counts only as ready_to_ask, never "
                "toward the quota"
            )
        if REPORTED_FIXED_ON_MAIN in record["warnings"]:
            record["next"] += (
                f" -- but first check main, {REPORTED_FIXED_ON_MAIN}: "
                + str(record["reported_fixed"])
            )
    _store_prescreen(data_root, slug, number, record)
    return record


def reject_by_hand(
    data_root: Path, issue: str, *, reason: str, evidence: str | None = None
) -> dict[str, Any]:
    """Record the coordinator's own rejection of an issue, with its reason.

    A machine screen already on disk is kept and extended, so its evidence is
    not lost; with none, the record carries only what the coordinator wrote.
    """
    reason = reason.strip()
    if len(reason) < 10:
        raise ValueError(
            "a rejection needs a reason a later session can check, "
            "at least a sentence long"
        )
    slug, number = issue_reference(issue)
    record = load_prescreen(data_root, slug, number) or {
        "repository": slug,
        "issue_number": number,
        "workspace": str(prescreen_directory(data_root, slug, number)),
    }
    blocking = [item for item in record.get("blocking") or [] if item != REJECTED_BY_COORDINATOR]
    record.update(
        {
            "schema_version": PRESCREEN_SCHEMA_VERSION,
            "screened_at": datetime.now(UTC).isoformat(),
            "verdict": "reject",
            "blocking": [*blocking, REJECTED_BY_COORDINATOR],
            "warnings": record.get("warnings") or [],
            "coordinator_rejection": {
                "reason": reason,
                "evidence": (evidence or "").strip() or None,
            },
            "next": f"Do not open a run on {slug}#{number}: {reason}",
        }
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


def _merged_before_issue(row: dict[str, Any], issue_created_at: Any) -> bool:
    """Whether a merge shipped well before the issue was opened. Mailman #147."""
    try:
        merged = datetime.fromisoformat(str(row.get("merged_at")).replace("Z", "+00:00"))
        opened = datetime.fromisoformat(str(issue_created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return opened - merged > timedelta(days=MERGED_BEFORE_ISSUE_DAYS)


#: Exception class names read as camelCase identifiers but name the symptom,
#: not the code; every traceback title carries one.
_SYMPTOM_NAME = re.compile(r"(?:Error|Exception|Warning)$")
_TITLE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[a-z0-9][A-Z]")
#: How many same-repository issues cited from the body one prescreen reads.
CITED_ISSUE_LIMIT = 3


def identifier_terms(title: str) -> list[str]:
    """The code identifiers a title names, lowercased, in order.

    A word counts when it has an inner underscore or an inner capital:
    `hmac_key`, `StoredFunction`. Dunders and exception class names do not;
    `__init__` and `TypeError` are in every traceback title. Mailman #257.
    """
    terms: list[str] = []
    for word in _TITLE_WORD.findall(title or ""):
        if word.startswith("__") and word.endswith("__"):
            continue
        if _SYMPTOM_NAME.search(word):
            continue
        inner = word.strip("_")
        if "_" not in inner and not _CAMEL.search(inner):
            continue
        term = word.lower()
        if term not in terms:
            terms.append(term)
    return terms


def uncited_merged_fixes(
    directory: Path,
    *,
    slug: str,
    title: str,
    issue_created_at: Any,
    executable: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Merged pull requests since the issue opened that name its identifiers.

    aws/sagemaker-python-sdk#6294 fixed #5495's stale `hmac_key` call without
    citing the issue; the title search ANDed every word and found nothing,
    and the run found it with `storedfunction hmac_key`. Mailman #257.
    """
    terms = identifier_terms(title)
    record: dict[str, Any] = {"terms": terms, "matches": [], "searched": False}
    if len(terms) < 2:
        return record
    command = [
        executable or resolve_tool(directory, "gh"),
        "search",
        "prs",
        *terms,
        "--repo",
        slug,
        "--merged",
        "--limit",
        "10",
        "--json",
        "number,title,body,url,closedAt",
    ]
    opened = str(issue_created_at or "")[:10]
    if opened:
        command[command.index("--merged") + 1 : command.index("--merged") + 1] = [
            # A range, not `>=DATE`: a `.cmd` shim reads `>` as a redirect.
            "--merged-at",
            f"{opened}..*",
        ]
    result = execute(
        command, working_directory=directory, timeout_seconds=timeout_seconds
    )
    record["searched"] = True
    if result.timed_out or result.exit_code != 0:
        record["error"] = (result.stderr or "").strip()[:300] or "gh search failed"
        return record
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        record["error"] = "the GitHub CLI returned unreadable JSON"
        return record
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        text = f"{row.get('title') or ''}\n{row.get('body') or ''}".lower()
        if all(term in text for term in terms):
            record["matches"].append(
                {
                    "number": row.get("number"),
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "merged_at": row.get("closedAt"),
                }
            )
    return record


def completed_cited_issues(
    directory: Path,
    *,
    slug: str,
    skipped: Sequence[dict[str, Any]],
    issue_created_at: Any,
    executable: str | None,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Same-repository issues the body defers to, closed as done since.

    huggingface/lerobot#2302 defers to #2283, closed COMPLETED after its fix
    merged; the resolver skipped #2283 because it is an issue. Mailman #257.
    """
    try:
        opened = datetime.fromisoformat(
            str(issue_created_at).replace("Z", "+00:00")
        )
    except ValueError:
        return []
    numbers: list[int] = []
    for reference in skipped:
        number = reference.get("number")
        if (
            reference.get("in") == "body"
            and str(reference.get("repository") or "").lower() == slug.lower()
            and isinstance(number, int)
            and number not in numbers
        ):
            numbers.append(number)
    found: list[dict[str, Any]] = []
    for number in numbers[:CITED_ISSUE_LIMIT]:
        result = execute(
            [
                executable or resolve_tool(directory, "gh"),
                "issue",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                "number,state,stateReason,closedAt,title,url",
            ],
            working_directory=directory,
            timeout_seconds=timeout_seconds,
        )
        if result.timed_out or result.exit_code != 0:
            continue
        try:
            payload = json.loads(result.stdout or "{}")
            closed = datetime.fromisoformat(
                str(payload.get("closedAt")).replace("Z", "+00:00")
            )
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
        if (
            str(payload.get("state") or "").upper() == "CLOSED"
            and str(payload.get("stateReason") or "").upper() == "COMPLETED"
            and closed > opened
        ):
            found.append(
                {
                    "number": payload.get("number", number),
                    "title": payload.get("title"),
                    "url": payload.get("url"),
                    "closed_at": payload.get("closedAt"),
                }
            )
    return found


#: How far the named base branch may have moved past a run's base commit
#: between choosing it and `init-run`. Mailman #258.
BASE_BRANCH_SLACK = 3


def base_branch_refusal(
    record: dict[str, Any] | None,
    *,
    base_commit: str,
    executable: str | None = None,
    timeout_seconds: float = 60,
) -> str | None:
    """Why `base_commit` is not a base the project takes pull requests on.

    `None` when the screen names no other branch. One compare call: the
    branch may be at most `BASE_BRANCH_SLACK` commits past the base commit,
    and the base commit must be on it. Mailman #258.
    """
    base = (record or {}).get("pull_request_base")
    if not isinstance(base, dict) or not base.get("branch"):
        return None
    slug = str(record.get("repository") or "")
    branch = str(base["branch"])
    result = execute(
        [
            executable or resolve_tool(Path.cwd(), "gh"),
            "api",
            f"repos/{slug}/compare/{base_commit}...{branch}",
            "--jq",
            "{status: .status, ahead_by: .ahead_by}",
        ],
        working_directory=Path.cwd(),
        timeout_seconds=timeout_seconds,
    )
    if result.timed_out or result.exit_code != 0:
        return (
            f"{slug} takes pull requests against `{branch}`, and whether "
            f"{base_commit} is on it could not be read: "
            + ((result.stderr or "").strip()[:200] or "gh api failed")
        )
    try:
        compared = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return f"the compare of {base_commit} with `{branch}` was unreadable"
    status = compared.get("status")
    ahead = compared.get("ahead_by")
    if status == "identical" or (
        status == "ahead" and isinstance(ahead, int) and ahead <= BASE_BRANCH_SLACK
    ):
        return None
    return (
        f"{slug} takes pull requests against `{branch}` "
        f"({base.get('quote')!r}), and {base_commit} is not its head: "
        f"compare says {status}, {ahead} commits behind. Use the head of "
        f"`{branch}` as --base-commit"
    )
