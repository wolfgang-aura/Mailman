"""Persistent PRHunt quota and completion gates, shared by every coordinator."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailman import health
from mailman.agents import normalize_agent_name
from mailman.artifacts import load_run, new_run_id
from mailman.completion import finalize_review, read_object
from mailman.claims import load_claims
from mailman.handoff import check_handoff, load_handoff, load_offer_handoff
from mailman.maintainers import load_maintainer_logins
from mailman.models import RunStatus, utc_now
from mailman.orchestrator import orchestration_step_names
from mailman.provenance import upstream_issue_number
from mailman.review_decision import UNTRIAGED_GATE, DecisionError, load_decision
from mailman.screen import load_screen, screen_is_current
from mailman.target_intel import repository_slug
from mailman.targeting import (
    STALE_PRIOR_ATTEMPT,
    UNACKNOWLEDGED_ATTEMPTS,
    assess_target,
)

PROCEDURE = Path(__file__).with_name("procedure.md")
#: How long one coordinator owns a hunt before another may take it over
#: without an argument. Long enough to cover a reviewer stage, short enough
#: that an abandoned hunt is not stuck for a day.
LEASE_MINUTES = 90
#: The clock a hunt created before #166 got when its record carries no
#: `deadline_at`. A new hunt has a deadline only when `hunt init
#: --time-budget-hours` asks for one: each model role already has a
#: ten-minute limit and each run its own budget, so a hunt-wide clock only
#: stops a hunt from replacing candidates.
#: https://github.com/wolfgang-aura/Mailman/issues/166
HUNT_TIME_BUDGET_SECONDS = 2 * 60 * 60

HUMAN_REASONS = ("authentication", "budget", "scope", "conflicting-instructions")
DROP_CODES = {
    "issue-assigned", "work-handed-over", "open-pull-request",
    "already-fixed-upstream", "bug-not-reproduced", "fails-freshness-bar",
    "reproduction-not-machine-checked", "no-maintainer-reply",
}
#: Codes a target's assessment raises as a warning. They neither replace the
#: candidate nor send the coordinator back to a stage; they travel with the run
#: so the work order and the pull request body can answer them.
#: `stale-prior-attempt` is here and deliberately not in DROP_CODES: an attempt
#: nobody has touched in months is prior art, not a rival in flight.
WARNING_CODES = {STALE_PRIOR_ATTEMPT}
#: A hunt in one of these states is history. Its record answers "what did we
#: file, and on what evidence", and nothing may rewrite that answer.
TERMINAL_STATUSES = ("FILED", "ABANDONED")
#: How many readiness checks a hunt keeps. Enough to see the run of checks
#: around a filing; short enough that the record stays readable.
CHECK_HISTORY = 20
PULL_REQUEST_URL = re.compile(
    r"^https://github\.com/(?P<repository>[\w.-]+/[\w.-]+)/pull/(?P<number>\d+)/?$"
)


def is_terminal(record: dict) -> bool:
    return record.get("status") in TERMINAL_STATUSES


def deadline(record: dict) -> datetime | None:
    """Return the hunt's fixed deadline, None when it was created without one."""
    if "deadline_at" in record and record["deadline_at"] is None:
        return None
    value = record.get("deadline_at")
    if value:
        parsed = datetime.fromisoformat(value)
    else:
        parsed = datetime.fromisoformat(record["created_at"]) + timedelta(
            seconds=float(record.get("time_budget_seconds", HUNT_TIME_BUDGET_SECONDS))
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def require_time_remaining(record: dict, action: str) -> None:
    expires = deadline(record)
    if expires is not None and datetime.now(UTC) >= expires:
        raise ValueError(
            f"hunt {record['hunt_id']} reached its fixed deadline {expires.isoformat()} "
            f"before {action}. Abandon it or finish preserved ready work; a new "
            "candidate does not get a new clock."
        )


def abandon(root: Path, record: dict, *, reason: str, owner: str | None = None) -> dict:
    """Close a hunt that is over and was never filed.

    Without this a finished-with failure stores `RUNNING` for ever and only
    `effective_status` calls it abandoned, so `hunt list` grows and every later
    session re-judges the same wreckage.
    https://github.com/wolfgang-aura/Mailman/issues/77
    """
    if not reason:
        raise ValueError("say why this hunt is over: --reason")
    lease = record.get("lease")
    if lease and not _expired(lease) and lease["owner"] != owner:
        raise ValueError(
            f"hunt {record['hunt_id']} is owned by {lease['owner']} until "
            f"{lease['expires_at']}. A live hunt is somebody's work in "
            "progress; closing one needs its --owner token."
        )
    record["status"] = "ABANDONED"
    record["abandoned"] = {"reason": reason, "at": utc_now()}
    record.pop("lease", None)
    save(hunt_path(root, record["hunt_id"]), record)
    return record["abandoned"]


def save(path: Path, record: dict) -> None:
    record["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def hunt_path(root: Path, hunt_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", hunt_id):
        raise ValueError("invalid hunt ID")
    return root.parent / "hunts" / hunt_id / "hunt.json"


def create_hunt(root: Path, count: int, *, primary: str, primary_model: str,
                reviewer: str, reviewer_model: str, owner: str | None = None,
                time_budget_seconds: float | None = None) -> dict:
    if count < 1 or isinstance(count, bool):
        raise ValueError("the PR count must be positive")
    if not primary_model.strip() or not reviewer_model.strip():
        raise ValueError("ask for both model IDs before starting the hunt")
    if time_budget_seconds is not None and time_budget_seconds <= 0:
        raise ValueError("a hunt's time budget must be positive")
    created_at = utc_now()
    created = datetime.fromisoformat(created_at)
    record = {
        "schema_version": 1, "hunt_id": new_run_id(), "requested": count,
        "data_root": str(root.resolve()), "created_at": created_at,
        "time_budget_seconds": time_budget_seconds,
        "deadline_at": (
            None if time_budget_seconds is None
            else (created + timedelta(seconds=time_budget_seconds)).isoformat()
        ),
        "primary": {"agent": normalize_agent_name(primary), "model": primary_model},
        "reviewer": {"agent": normalize_agent_name(reviewer), "model": reviewer_model},
        "procedure_sha256": hashlib.sha256(PROCEDURE.read_bytes()).hexdigest(),
        "runs": [], "escalations": [], "status": "RUNNING",
    }
    save(hunt_path(root, record["hunt_id"]), record)
    acquire_lease(root, record, owner=owner)
    return record


def load_hunt(root: Path, hunt_id: str, *, require_procedure: bool = True) -> dict:
    """Read a hunt, refusing one prepared under a procedure that has changed.

    `require_procedure=False` is for closing a hunt rather than continuing it.
    The hunts most in need of closing are the oldest, and they are exactly the
    ones pinned to a superseded procedure, so demanding a refresh first is how
    the wreckage stayed in `hunt list`.
    https://github.com/wolfgang-aura/Mailman/issues/77
    """
    record = read_object(hunt_path(root, hunt_id))
    if not record or record.get("data_root") != str(root.resolve()):
        raise ValueError("hunt missing or belongs to another data root")
    if (require_procedure and not is_terminal(record)
            and record.get("procedure_sha256") != hashlib.sha256(PROCEDURE.read_bytes()).hexdigest()):
        raise ValueError("procedure changed: read `mailman procedure`, then use `hunt refresh-procedure`")
    return record


def add_run(root: Path, record: dict, run_id: str) -> None:
    if is_terminal(record):
        raise ValueError(f"hunt {record['hunt_id']} is {record['status']}; start a new hunt")
    run, _ = load_run(run_id, root)
    for role in ("primary", "reviewer"):
        config = getattr(run, role)
        if {"agent": config.agent, "model": config.model} != record[role]:
            raise ValueError(f"{role} differs from the hunt's selected model; do not substitute models")
    if any(row["run_id"] == run_id for row in record["runs"]):
        return
    require_time_remaining(record, "adding another candidate")
    filing = find_filing(root, run_id=run_id)
    if filing:
        raise ValueError(
            f"run {run_id} was already filed as {filing['filed']['pr_url']} by hunt "
            f"{filing['hunt_id']}. A filed candidate is not a candidate."
        )
    key = target_key(run)
    if key:
        for previous in record["runs"]:
            earlier_key = previous.get("target")
            if not earlier_key:
                try:
                    earlier, _ = load_run(previous["run_id"], root)
                    earlier_key = target_key(earlier)
                except (OSError, ValueError, KeyError):
                    earlier_key = None
            if earlier_key == key:
                raise ValueError(
                    f"hunt {record['hunt_id']} already used target {key} in run "
                    f"{previous['run_id']}. Resume that preserved run or choose "
                    "a different target; dropping it does not reset hunt time."
                )
        filing = find_filing(root, target=key)
        if filing:
            raise ValueError(
                f"{key} was already filed as {filing['filed']['pr_url']} by hunt "
                f"{filing['hunt_id']}. Find a different target."
            )
        holder = holding_hunt(root, key, exclude=record["hunt_id"])
        if holder:
            raise ValueError(
                f"{key} is held by live hunt {holder['hunt_id']} (owner "
                f"{holder['owner']}, until {holder['expires_at']}). Two hunts in one "
                "data root must not work the same target. Run `mailman hunt targets`."
            )
    record["runs"].append({"run_id": run_id, "target": key})
    save(hunt_path(root, record["hunt_id"]), record)


def restore_run(
    root: Path, record: dict, run_id: str, *, reason: str, evidence: str
) -> None:
    """Restore a dropped run after new evidence resolves its recorded blocker."""
    row = next((row for row in record["runs"] if row["run_id"] == run_id), None)
    if row is None:
        raise ValueError("run is not in this hunt")
    if not row.get("dropped"):
        raise ValueError("run is not dropped")
    row.pop("dropped", None)
    row.pop("reason", None)
    row.pop("evidence", None)
    row["restored"] = {"reason": reason, "evidence": evidence, "at": utc_now()}
    save(hunt_path(root, record["hunt_id"]), record)


# --- Filing record --------------------------------------------------------
#
# Hunt 20260907T164341Z-1ca91a produced three candidates that went upstream as
# PDM #3884, PDM #3883 and Poetry #11052. The record knew nothing about it: the
# status still read AWAITING_FILING_APPROVAL and no run carried a PR. A later
# session read that as three ready, unfiled candidates and moved to hand them
# to the operator a second time. https://github.com/wolfgang-aura/Mailman/issues/71


def target_key(run) -> str | None:
    """The `owner/repo#issue` a run works on, or None for a defect report."""
    if not run.issue:
        return None
    number = str(run.issue).strip().lstrip("#").rsplit("/", 1)[-1]
    return f"{repository_slug(run.repository)}#{number}"


def iter_hunts(root: Path):
    for path in sorted((root.parent / "hunts").glob("*/hunt.json")):
        record = read_object(path)
        if record:
            yield record


def hunt_for_run(root: Path, run_id: str) -> dict | None:
    """Return the nonterminal hunt that owns a run, if there is one."""
    for record in iter_hunts(root):
        if is_terminal(record):
            continue
        if any(row.get("run_id") == run_id for row in record.get("runs", [])):
            return record
    return None


def _record_run_provenance(*, root: Path, run, run_directory: Path,
                           pull_request: int) -> dict:
    """The ledger entry for a filed candidate, written where the PR is known."""
    from mailman.provenance import record_provenance

    return record_provenance(
        run_id=run.run_id,
        run_directory=run_directory,
        repository=run.repository,
        base_commit=run.base_commit,
        pull_request=pull_request,
        head=(load_handoff(run_directory) or {}).get("head"),
    )


def record_filing(root: Path, record: dict, run_id: str, *, pr_url: str,
                  commit: str | None = None,
                  provenance_recorder=None) -> dict:
    """Write the pull request a candidate became, and close the hunt when done.

    This is the only thing that makes a filing visible to the next session. It
    runs after the operator approves and after the PR exists, so it takes the
    URL rather than creating anything.

    It also writes the run's provenance, because nothing else required it and
    two PRs filed on 2026-09-09 were absent from `mailman contributions` until
    a hand audit found them. Nothing is saved if the provenance cannot be
    written: a filing the ledger cannot see is the failure this closes.
    See https://github.com/wolfgang-aura/Mailman/issues/84.
    """
    if is_terminal(record):
        raise ValueError(f"hunt {record['hunt_id']} is {record['status']}; start a new hunt")
    match = PULL_REQUEST_URL.match(pr_url.strip())
    if not match:
        raise ValueError("--pr-url must be https://github.com/OWNER/REPO/pull/NUMBER")
    row = next((row for row in record["runs"] if row["run_id"] == run_id), None)
    if row is None:
        raise ValueError("run is not in this hunt")
    if row.get("dropped"):
        raise ValueError("a dropped candidate was not filed; restore it first")
    if row.get("filed"):
        raise ValueError(f"already recorded as {row['filed']['pr_url']}")
    run, _ = load_run(run_id, root)
    if repository_slug(match["repository"]) != repository_slug(run.repository):
        raise ValueError(
            f"that pull request is on {match['repository']}, the run targets "
            f"{run.repository}"
        )
    directory = root / run_id
    recorder = provenance_recorder or _record_run_provenance
    recorder(root=root, run=run, run_directory=directory,
             pull_request=int(match["number"]))
    row["filed"] = {"pr_url": pr_url.strip(), "pr_number": int(match["number"]),
                    "repository": match["repository"], "target": target_key(run),
                    "commit": commit, "filed_at": utc_now()}
    filed = [row for row in record["runs"] if row.get("filed")]
    if len(filed) >= record["requested"]:
        record["status"] = "FILED"
        record["filed_at"] = utc_now()
    save(hunt_path(root, record["hunt_id"]), record)
    return row["filed"]


def find_filing(root: Path, *, run_id: str | None = None,
                target: str | None = None) -> dict | None:
    """Find the hunt that already filed this run or this `owner/repo#issue`."""
    for record in iter_hunts(root):
        for row in record["runs"]:
            filed = row.get("filed")
            if not filed:
                continue
            if run_id and row["run_id"] == run_id:
                return {"hunt_id": record["hunt_id"], "filed": filed}
            if target and filed.get("target") == target:
                return {"hunt_id": record["hunt_id"], "filed": filed}
    return None


# --- Cross-hunt target claims ---------------------------------------------
#
# The lease in #63 stops two coordinators sharing one hunt. It does nothing
# about two hunts sharing a candidate pool, which is what happened on
# 2026-09-09: two live hunts three minutes apart in one data root, each blind
# to the other. One of them guessed at a sibling by reading directory
# timestamps and dropped two good candidates over a 24-hour-dead root.
#
# A claim is derived from the hunts themselves rather than stored separately,
# so there is no second file to go stale.
# https://github.com/wolfgang-aura/Mailman/issues/73


def effective_status(record: dict) -> str:
    """What this hunt really is, not what it last wrote down.

    A `RUNNING` hunt whose lease has expired has no coordinator. Reading it as
    running is how a session concludes a target is taken when nobody is there.
    """
    status_name = record.get("status", "UNKNOWN")
    if status_name != "RUNNING":
        return status_name
    lease = record.get("lease")
    if not lease or _expired(lease):
        return "ABANDONED"
    return "RUNNING"


def target_claims(root: Path) -> list[dict]:
    """Every `owner/repo#issue` a live hunt in this data root is working on."""
    claims: list[dict] = []
    for record in iter_hunts(root):
        live = effective_status(record) == "RUNNING"
        lease = record.get("lease") or {}
        for row in record["runs"]:
            if row.get("dropped"):
                continue
            try:
                run, _ = load_run(row["run_id"], root)
            except (OSError, ValueError, KeyError):
                continue
            key = target_key(run)
            if not key:
                continue
            claims.append({
                "target": key, "run_id": row["run_id"], "hunt_id": record["hunt_id"],
                "hunt_status": effective_status(record), "live": live,
                "filed": (row.get("filed") or {}).get("pr_url"),
                "owner": lease.get("owner"), "expires_at": lease.get("expires_at"),
            })
    return claims


def holding_hunt(root: Path, target: str, *, exclude: str | None = None) -> dict | None:
    for claim in target_claims(root):
        if claim["target"] == target and claim["live"] and claim["hunt_id"] != exclude:
            return claim
    return None


# --- Prescreens and the next target ----------------------------------------
#
# Hunt 20260916T165859Z-0d3481 spent 45 of 56 minutes finding a target: 20
# prescreens, 4 passes, one run, and hunt.json kept none of it, so the next
# hunt re-derived the same rejects. A prescreen made for a hunt is now written
# to it, and the next target is read from the screens rather than from issue
# pages. See https://github.com/wolfgang-aura/Mailman/issues/102.

#: A screen older than this is not read for targets; its shortlist has aged.
TARGET_SCREEN_MAX_AGE_DAYS = 7


def record_prescreen(root: Path, record: dict, prescreen_record: dict) -> dict | None:
    """Append one prescreen verdict to the hunt; a repeat replaces the older row."""
    if is_terminal(record):
        return None
    target = (
        f"{repository_slug(prescreen_record['repository'])}"
        f"#{prescreen_record['issue_number']}"
    )
    row = {
        "target": target,
        "verdict": prescreen_record.get("verdict"),
        "rejects": list(prescreen_record.get("blocking") or []),
        "screened_at": prescreen_record.get("screened_at"),
    }
    record["prescreens"] = [
        *(entry for entry in record.get("prescreens", []) if entry.get("target") != target),
        row,
    ]
    save(hunt_path(root, record["hunt_id"]), record)
    return row


def prescreen_counts(record: dict) -> dict:
    rows = record.get("prescreens") or []
    return {
        "screened": len(rows),
        "passed": sum(1 for row in rows if row.get("verdict") == "pass"),
    }


def open_pull_request_repositories(root: Path) -> set[str]:
    """Repositories where a filed pull request of ours was not last seen closed.

    The filed watch is the only local record of a pull request's state, so a
    filing it has not read counts as open: one open pull request per
    repository is the rule, and guessing closed would break it.
    """
    from mailman.filed_watch import filed_rows, watch_path

    closed: set[tuple[str, int]] = set()
    watch = read_object(watch_path(root)) or {}
    for reading in watch.get("rows") or []:
        if isinstance(reading, dict) and reading.get("state") == "closed":
            closed.add((str(reading.get("repository", "")).lower(),
                        int(reading.get("pull_request") or 0)))
    return {
        row["repository"]
        for row in filed_rows(root)
        if (row["repository"].lower(), row["pull_request"]) not in closed
        and not row.get("superseded_by")
    }


ENGAGED = "engaged"
NOT_ENGAGED = "not-engaged"
ENGAGEMENT_UNKNOWN = "unknown"
#: A maintainer answered, and the latest answer disputes the bug. Mailman #150.
DISPUTED = "disputed"
_ENGAGEMENT_RANK = {ENGAGED: 0, ENGAGEMENT_UNKNOWN: 1, NOT_ENGAGED: 2, DISPUTED: 3}


_BUG_LABEL = re.compile(r"bug|regression|crash", re.IGNORECASE)


def row_bug_labelled(row: dict) -> bool:
    """Whether a label on the row calls the issue a bug. Mailman #189."""
    for entry in row.get("labels") or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and _BUG_LABEL.search(name):
            return True
    return False


def _target_rank(target: dict) -> tuple:
    # Within an engagement group, a maintainer's bug label and then youth:
    # screen order put certbot's 581-day-old test-data chore first on
    # 2026-09-29 while two-day-old labelled bugs sat 200 rows down. #189.
    age = target.get("age_days")
    return (
        _ENGAGEMENT_RANK[target["engagement"]],
        not target["bug_labelled"],
        age if isinstance(age, int) else 10**6,
    )


def row_engagement(row: dict) -> str:
    """Whether a maintainer filed or answered the issue, as far as the screen knows.

    Unknown when the screen predates the flags (f94d449) or did not read the
    thread: that row may be triaged, so it ranks above a known silence.
    https://github.com/wolfgang-aura/Mailman/issues/135
    """
    if row.get("maintainer_disputed"):
        return DISPUTED
    filed, replied = row.get("maintainer_filed"), row.get("maintainer_replied")
    # A label from somebody with triage access is triage too (#139).
    if filed or row.get("maintainer_labelled"):
        return ENGAGED
    if replied:
        # A screen from before #150 never asked whether the reply disputes
        # the bug, and jedi#2077's "couldn't reproduce" ranked engaged. #192.
        return ENGAGED if "maintainer_disputed" in row else ENGAGEMENT_UNKNOWN
    if filed is False and replied is False:
        return NOT_ENGAGED
    return ENGAGEMENT_UNKNOWN


def stale_screen_warning(targets: list[dict]) -> str | None:
    """One line naming the screens too old to carry maintainer flags, or None."""
    stale = [row for row in targets if row.get("stale_screen")]
    if not stale:
        return None
    slugs = sorted({row["target"].rsplit("#", 1)[0] for row in stale})
    commands = "; ".join(f"mailman screen-target {slug} --refresh" for slug in slugs)
    return (
        f"warning: {len(stale)} workable row(s) come from {len(slugs)} screen(s) "
        f"written before maintainer engagement was recorded, so their engagement "
        f"is unknown; refresh with: {commands}"
    )


def workable_targets(root: Path, *, held_repositories: set[str] | None = None,
                     max_age_days: int = TARGET_SCREEN_MAX_AGE_DAYS,
                     now: datetime | None = None,
                     engaged_only: bool = False) -> list[dict]:
    """Shortlisted issues from fresh passing screens that nobody has taken yet.

    An issue is left out when it was prescreened, when a live hunt holds it,
    when its repository holds our open pull request, or when an open or merged
    pull request is cross-referenced to it. Rows a maintainer filed, replied
    on or labelled come first, then rows whose engagement is unknown,
    then the rest; within each group, newest screen first in each screen's
    own shortlist order. An untriaged run never counts ready, so a hunt that
    starts on silent issues comes back empty (#135). `engaged_only` keeps
    only the first group.
    """
    from mailman.prescreen import prescreen_path
    from mailman.screen import SCREENS_DIRECTORY, is_request_row, screen_shortlist

    moment = now or datetime.now(UTC)
    held = {slug.lower() for slug in (
        open_pull_request_repositories(root) if held_repositories is None
        else held_repositories
    )}
    claimed = {claim["target"] for claim in target_claims(root) if claim["live"]}
    screens = []
    for path in sorted((root / SCREENS_DIRECTORY).glob("*.json")):
        screen = read_object(path)
        if not screen or not screen.get("success") or screen.get("verdict") != "pass":
            continue
        if not screen_is_current(screen):
            continue
        try:
            screened = datetime.fromisoformat(str(screen.get("screened_at")))
        except ValueError:
            continue
        if screened.tzinfo is None:
            screened = screened.replace(tzinfo=UTC)
        if moment - screened > timedelta(days=max_age_days):
            continue
        slug = repository_slug(str(screen.get("repository") or ""))
        if slug.lower() in held:
            continue
        screens.append((screened, slug, screen))
    targets = []
    for screened, slug, screen in sorted(screens, key=lambda item: item[0], reverse=True):
        for row in screen_shortlist(screen):
            target = f"{slug}#{row.get('number')}"
            if target in claimed or prescreen_path(root, slug, int(row["number"])).is_file():
                continue
            # An open or merged pull request already answers it; racing one
            # is how 24 of 55 confirmed bugs were lost on 2026-09-28.
            if row.get("rival_pull_requests"):
                continue
            # Screens recorded before the not-a-bug filter still hold
            # requests and RFCs. Mailman #188.
            if is_request_row(row):
                continue
            engagement = row_engagement(row)
            if engaged_only and engagement != ENGAGED:
                continue
            targets.append({
                "target": target, "title": row.get("title"),
                "age_days": row.get("age_days"), "reasons": row.get("reasons") or [],
                "engagement": engagement,
                "bug_labelled": row_bug_labelled(row),
                "maintainer_filed": row.get("maintainer_filed"),
                "maintainer_replied": row.get("maintainer_replied"),
                "maintainer_labelled": row.get("maintainer_labelled"),
                "maintainer_disputed": row.get("maintainer_disputed"),
                # A screen written before f94d449 has no flags at all.
                "stale_screen": "maintainer_filed" not in row,
                "screened_at": screen.get("screened_at"),
            })
    targets.sort(key=_target_rank)
    return targets


# --- Coordinator ownership ------------------------------------------------
#
# Two agent tasks attached to hunt 20260907T164341Z-1ca91a. Only one owned
# candidates; the other polled `hunt status` for hours and added coordination
# cost instead of throughput. A hunt now has one owner at a time, and a second
# coordinator has to say out loud that it is taking over.
# See https://github.com/wolfgang-aura/Mailman/issues/63.


def new_owner_token() -> str:
    return secrets.token_hex(8)


def _expired(lease: dict) -> bool:
    try:
        return datetime.fromisoformat(lease["expires_at"]) <= datetime.now(UTC)
    except (KeyError, TypeError, ValueError):
        return True


def lease_state(record: dict) -> dict:
    lease = record.get("lease")
    if not lease:
        return {"held": False, "reason": "this hunt predates coordinator leases"}
    return {"held": not _expired(lease), "owner": lease["owner"],
            "expires_at": lease["expires_at"],
            "takeovers": len(lease.get("takeovers", []))}


def acquire_lease(root: Path, record: dict, *, owner: str | None = None,
                  takeover_reason: str | None = None,
                  minutes: int = LEASE_MINUTES) -> dict:
    """Take or renew ownership of this hunt.

    Renewing your own live lease is free. Taking a live lease from someone else
    needs a reason, and the reason is kept: an unexplained takeover is exactly
    the situation this is here to make visible.
    """
    existing = record.get("lease")
    owner = owner or new_owner_token()
    if existing and existing["owner"] != owner and not takeover_reason:
        if not _expired(existing):
            raise ValueError(
                f"hunt {record['hunt_id']} is owned by {existing['owner']} until "
                f"{existing['expires_at']}. Do not run a second coordinator on one "
                "hunt. Pass --takeover with --reason if that owner is genuinely gone."
            )
        # An expired lease used to be free to pick up, which made adopting
        # somebody's abandoned hunt the default and left the operator as the
        # only gate on whether that was the right hunt to continue.
        # https://github.com/wolfgang-aura/Mailman/issues/76
        raise ValueError(
            f"hunt {record['hunt_id']} was abandoned by {existing['owner']} at "
            f"{existing['expires_at']}. Continuing someone else's hunt is a "
            "decision, not a default: its candidates were chosen for their "
            "request, not yours. Pass --takeover with --reason to adopt it, or "
            "close it with `hunt abandon --reason ...` and open your own."
        )
    takeovers = list(existing.get("takeovers", [])) if existing else []
    if existing and existing["owner"] != owner:
        takeovers.append({"previous_owner": existing["owner"], "at": utc_now(),
                          "reason": takeover_reason or "expired lease",
                          "was_live": not _expired(existing)})
    record["lease"] = {
        "owner": owner, "acquired_at": utc_now(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(),
        "takeovers": takeovers,
    }
    save(hunt_path(root, record["hunt_id"]), record)
    return record["lease"]


def release_lease(root: Path, record: dict, *, owner: str) -> None:
    lease = record.get("lease")
    if lease and lease["owner"] != owner and not _expired(lease):
        raise ValueError("only the current owner can release this lease")
    record.pop("lease", None)
    save(hunt_path(root, record["hunt_id"]), record)


def require_lease(record: dict, owner: str | None) -> None:
    """Refuse a state change from anyone but the current owner.

    A hunt created before leases existed has no owner recorded, so it stays
    usable. Every hunt created since carries one.
    """
    lease = record.get("lease")
    if not lease:
        return
    if _expired(lease):
        if owner == lease["owner"]:
            return
        raise ValueError(
            f"hunt {record['hunt_id']} was abandoned by {lease['owner']} at "
            f"{lease['expires_at']}. Adopt it with `hunt lease --takeover "
            "--reason ...`, or close it with `hunt abandon --reason ...`."
        )
    if owner != lease["owner"]:
        raise ValueError(
            f"hunt {record['hunt_id']} is owned by {lease['owner']} until "
            f"{lease['expires_at']}. Pass --owner with that token, or take the "
            "hunt over with `hunt lease --takeover --reason ...`. Two "
            "coordinators on one hunt produce less than one."
        )


#: A candidate that is complete except for a maintainer's answer. It is listed
#: and packaged with its offer comment, and it never counts as a pull request.
READY_TO_ASK = "READY_TO_ASK"
OWN_WORDS = "policy-requires-own-words"
OWN_WORDS_ACTION = (
    "Include in the final approval packet. Before filing, rewrite the pull "
    "request body in your own words, set own_words_confirmed in the target "
    "policy and rerun prepare-submission."
)


def ask_ready(run, directory: Path, decision, action, warnings: list) -> dict:
    """Readiness for an ask-first candidate: verified, with an offer to approve.

    The untriaged question is the reason for asking, so it may stay blocking;
    any other blocking question is still coordinator work. No PR handoff is
    required yet: the pull request is filed after a maintainer answers. The
    offer comment's own handoff is required, and once the claims record shows
    a maintainer answered it, the candidate goes back to the SEND path.
    https://github.com/wolfgang-aura/Mailman/issues/138
    """
    other = [question for question in decision.blocking_questions
             if question.gate != UNTRIAGED_GATE]
    if other:
        return action("decision", "Resolve coordinator work; record a genuine user dependency with hunt escalate.")
    try:
        finalize_review(directory)
    except (OSError, ValueError) as error:
        return action("finalize", f"mailman finalize-review {run.run_id}", str(error))
    draft = (directory / decision.offer.path).resolve()
    offer = load_offer_handoff(directory)
    if not offer or Path(str(offer.get("body_path") or "")).resolve() != draft:
        issue = upstream_issue_number(directory, run.repository) or "ISSUE"
        return action("handoff", f"mailman handoff {run.run_id} --offer --kind issue-comment "
                                 f"--issue {issue} --repo {repository_slug(run.repository)} "
                                 f'--body "{draft}"')
    checked = check_handoff(directory, offer=True)
    if not checked["ok"]:
        return action("handoff", f"mailman handoff-check {run.run_id} --offer", checked["detail"])
    replies = (load_claims(directory) or {}).get("offer_replies") or []
    if replies:
        first = replies[0]
        return action("decision", "maintainer replied to offer; switch decision to SEND",
                      f"{first.get('author')} ({first.get('association')}) at "
                      f"{first.get('created_at')}: {first.get('quote')} -- Read the reply. "
                      "On a yes, set recommendation SEND, remove the offer block and do "
                      "the PR handoff. A refusal ends the candidate.")
    row = {"run_id": run.run_id, "ready": False, "stage": "offer-approval",
           "disposition": READY_TO_ASK, "human_required": False,
           "offer": str(directory / decision.offer.path),
           "action": "Include the offer comment in the approval packet. Mailman "
                     "does not post it, and the pull request waits for a "
                     "maintainer's answer; this is not a PR. Once it is posted, "
                     f"`mailman claims {run.run_id}` or `hunt refresh` reads "
                     "the answer."}
    if warnings:
        row["warnings"] = list(warnings)
    return row


def next_action(directory: Path) -> dict:
    run, _ = load_run(directory.name, directory.parent)

    # Filled once the target has been assessed, and carried by every row after
    # it. Left out while empty, because a key on every row costs the
    # coordinator context on every later turn.
    warnings: list[str] = []

    def action(stage: str, command: str, detail: str = "", disposition: str = "REPAIR") -> dict:
        row = {"run_id": run.run_id, "ready": False, "stage": stage,
               "action": command, "detail": detail, "disposition": disposition,
               "human_required": False}
        if warnings:
            row["warnings"] = list(warnings)
        return row

    screen = load_screen(directory.parent, repository_slug(run.repository)) or {}
    if (screen.get("success") and screen.get("verdict") != "pass"
            and not screen_is_current(screen)):
        # A refusal read under narrower windows than today's may not stand,
        # so it is re-read rather than replaced. A pass stays a pass.
        return action("screen", f"mailman screen-target {repository_slug(run.repository)} --refresh",
                      disposition="REPAIR")
    if screen.get("verdict") != "pass" or not screen.get("success"):
        return action("screen", f"mailman screen-target {repository_slug(run.repository)} --refresh",
                      disposition="REPLACE" if screen.get("success") else "REPAIR")
    for filename, command in (
        ("issue.json", "fetch-issue"), ("duplicate-search.json", "duplicate-search"),
        ("prior-art.json", "prior-art"), ("target-intel.json", "target-intel"),
        ("claims.json", "claims"), ("workspace.json", "prepare-workspace"),
        ("environment.json", "prepare-environment"), ("reproduction.json", "reproduce"),
    ):
        data = read_object(directory / filename)
        if not data or data.get("success") is False:
            return action(command, f"mailman {command} {run.run_id}")
    # Orchestration can start after the coordinator has explicitly read and
    # acknowledged closed attempts. Preserve that decision for the later
    # readiness check, but only while the current closed-attempt set has not
    # grown since the recorded assessment.
    recorded_assessment = read_object(directory / "target-assessment.json")
    recorded_closed = {
        attempt.get("number")
        for attempt in recorded_assessment.get("closed_attempts", [])
        if isinstance(attempt, dict) and isinstance(attempt.get("number"), int)
    }
    current_assessment = assess_target(directory)
    current_closed = {
        attempt.get("number")
        for attempt in current_assessment.closed_attempts
        if isinstance(attempt.get("number"), int)
    }
    acknowledged = (
        recorded_assessment.get("may_start") is True
        and UNACKNOWLEDGED_ATTEMPTS in (recorded_assessment.get("warnings") or [])
        and bool(current_closed)
        and current_closed <= recorded_closed
    )
    assessment = assess_target(directory, acknowledged=acknowledged)
    warnings.extend(sorted(set(assessment.warnings) & WARNING_CODES))
    if assessment.blocking:
        code = assessment.blocking[0]
        detail = "; ".join(assessment.blocking)
        if assessment.base_snippets.get("already_fixed"):
            # Name the line that decided it. `already-fixed-upstream` with no
            # merged attempt behind it reads as a mistake until the snippet is
            # printed. https://github.com/wolfgang-aura/Mailman/issues/103
            detail += f". {assessment.base_snippets.get('detail')}"
        return action("target", f"mailman check-target {run.run_id}", detail,
                      "REPLACE" if code in DROP_CODES else "REPAIR")
    if not read_object(directory / "prompts.json").get("verification_command"):
        return action("prompts", f"mailman build-prompts {run.run_id} -- EXECUTABLE ARG ...")
    if run.status not in (RunStatus.ENGINEERING_COMPLETE, RunStatus.READY_FOR_HUMAN_REVIEW):
        state = health.load(directory)
        if state and run.status is RunStatus.BLOCKED:
            # The account, or the host, stopped this stage. Retrying the
            # candidate is the wrong repair and burns the same allowance
            # again. https://github.com/wolfgang-aura/Mailman/issues/67
            return {**action(state["stage"], state["resume_command"], state["detail"]),
                    "health": state["state"]}
        has_primary = "agent:primary" in orchestration_step_names(directory)
        command = "resume-review" if run.status is RunStatus.BLOCKED and has_primary else "orchestrate"
        return action("engineering", f"mailman {command} {run.run_id}",
                      "Read orchestration.json and the failed stage before retrying." if run.status is RunStatus.BLOCKED else "")
    exported_diff = directory / "export" / "changes.diff"
    exported = read_object(directory / "export" / "export.json")
    from mailman.completion import candidate_digest
    if (not exported_diff.is_file() or not exported.get("candidate_digest")
            or exported["candidate_digest"] != candidate_digest(Path(exported["workspace"]), run.base_commit)):
        return action("export", f"mailman export-patch {run.run_id}")
    submission = read_object(directory / "submission" / "submission.json")
    # A project that wants the description in the contributor's own words
    # leaves one step only the human can do, and that step happens at filing
    # approval, which is already theirs. Everything else about the candidate
    # is checked; it counts as ready and names the rewrite.
    # https://github.com/wolfgang-aura/Mailman/issues/181
    own_words = submission.get("blocking_codes") == [OWN_WORDS]
    if ((not submission.get("ready") and not own_words)
            or submission.get("diff_sha256") != hashlib.sha256(exported_diff.read_text(encoding="utf-8").encode("utf-8")).hexdigest()):
        return action("submission", f"mailman prepare-submission {run.run_id} --policy POLICY.json",
                      "; ".join(submission.get("blocking_codes", [])))
    try:
        decision = load_decision(directory)
    except DecisionError as error:
        return action("decision", f"mailman decision {run.run_id}", str(error))
    if decision.recommendation == "ASK":
        return ask_ready(run, directory, decision, action, warnings)
    if decision.recommendation != "SEND":
        return action("decision", "Resolve the HOLD or replace the candidate.", disposition="REPLACE" if decision.recommendation == "DROP" else "REPAIR")
    if decision.blocking_questions and not own_words:
        return action("decision", "Resolve coordinator work; record a genuine user dependency with hunt escalate.")
    try:
        finalize_review(directory)
    except (OSError, ValueError) as error:
        return action("finalize", f"mailman finalize-review {run.run_id}", str(error))
    handoff = load_handoff(directory)
    if not handoff or handoff.get("kind") != "pull-request":
        return action("handoff", f"mailman handoff {run.run_id} --body FINAL_BODY --repo OWNER/REPO --head OWNER:BRANCH --base BASE --title TITLE")
    if repository_slug(handoff.get("repository", "")) != repository_slug(run.repository):
        return action("handoff", "Correct the handoff destination to the run repository.")
    if handoff.get("first_person_claims") or handoff.get("head_owner_type") != "User":
        return action("handoff", "Remove unsupported personal claims and use a confirmed personal fork.")
    checked = check_handoff(directory)
    if not checked["ok"]:
        return action("handoff", f"mailman handoff-check {run.run_id}", checked["detail"])
    ready = {"run_id": run.run_id, "ready": True, "stage": "filing-approval",
             "disposition": "READY", "human_required": False,
             "action": "Include in the final approval packet."}
    if own_words:
        ready.update(human_required=True, action=OWN_WORDS_ACTION)
    if warnings:
        ready["warnings"] = list(warnings)
    return ready


#: Longest evidence or detail string the printed view keeps per row.
VIEW_TEXT = 200


def compact(result: dict) -> dict:
    """The part of a readiness check a coordinator acts on.

    A coordinator is a chat session, so every command it runs stays in its
    context and is re-sent on every later turn. `hunt status` on hunt
    20260907T164341Z-1ca91a printed 12,910 bytes, and 7,037 of them were the
    drop reasons and evidence for 21 candidates that were already dead. That
    share grows for the whole hunt, so checking progress gets more expensive
    the longer the hunt runs, which is the wrong direction.

    The record keeps all of it. This is what gets printed.
    """
    live, dropped = [], []
    for row in result.get("runs", []):
        (dropped if row.get("disposition") == "REPLACED" else live).append(row)
    view = {key: value for key, value in result.items() if key != "runs"}
    view["runs"] = [
        {key: (value[:VIEW_TEXT] + "..." if isinstance(value, str) and len(value) > VIEW_TEXT else value)
         for key, value in row.items()}
        for row in live
    ]
    if dropped:
        codes: dict[str, int] = {}
        for row in dropped:
            codes[row.get("reason") or "unrecorded"] = codes.get(row.get("reason") or "unrecorded", 0) + 1
        view["replaced"] = {"count": len(dropped),
                            "reasons": dict(sorted(codes.items(), key=lambda item: -item[1])),
                            "detail": "in the hunt record; pass --full to print it"}
    return view


def status(root: Path, record: dict) -> dict:
    rows = []
    targets = set()
    for row in record["runs"]:
        if row.get("dropped"):
            rows.append({**row, "ready": False, "disposition": "REPLACED", "human_required": False})
            continue
        run, directory = load_run(row["run_id"], root)
        key = (repository_slug(run.repository), run.issue or run.defect_report)
        if row.get("filed"):
            # A filed candidate is finished, and rechecking it asks the
            # readiness gate a question it cannot answer: the duplicate search
            # now finds this run's own pull request, reads it as a rival, and
            # tells the coordinator to replace the candidate it just filed.
            # Hunt 20260916T132927Z-c05183 read `ready 0, remaining 2` with one
            # of its two pull requests already open upstream.
            # https://github.com/wolfgang-aura/Mailman/issues/97
            rows.append({**row, "ready": True, "stage": "filed",
                         "disposition": "FILED", "action": "",
                         "detail": row["filed"]["pr_url"],
                         "filed": row["filed"]["pr_url"], "human_required": False})
            targets.add(key)
            continue
        try:
            checked = next_action(directory)
        except (OSError, ValueError) as error:
            checked = {"run_id": run.run_id, "ready": False, "disposition": "REPAIR",
                       "human_required": False, "detail": str(error), "action": "Repair the named evidence record."}
        if checked["ready"] and key in targets:
            checked.update(ready=False, disposition="REPLACE", detail="same target already counted")
        if checked["ready"]:
            targets.add(key)
        rows.append(checked)
    ready = sum(row["ready"] for row in rows)
    # Ask-first candidates are counted on their own and never toward the PR
    # quota. https://github.com/wolfgang-aura/Mailman/issues/138
    asking = sum(row.get("disposition") == READY_TO_ASK for row in rows)
    result = {"hunt_id": record["hunt_id"], "requested": record["requested"],
              "ready": ready, "remaining": max(0, record["requested"] - ready),
              "ready_to_ask": asking,
              "checked_at": utc_now(), "runs": rows,
              "next": "Prepare the approval packet." if ready >= record["requested"] else "Complete the next action or find a replacement candidate.",
              "escalations": record["escalations"]}
    expires = deadline(record)
    result["deadline_at"] = expires.isoformat() if expires else None
    result["deadline_expired"] = expires is not None and datetime.now(UTC) >= expires
    if result["deadline_expired"] and result["remaining"]:
        result["next"] = (
            "The hunt deadline expired. Preserve its records and abandon the "
            "hunt; do not add or start another candidate."
        )
    result["lease"] = lease_state(record)
    health_states = {row["health"] for row in rows if row.get("health")}
    if health_states:
        result["health"] = sorted(health_states)
    for row in record["runs"]:
        filed = row.get("filed")
        if filed:
            match = next((r for r in rows if r.get("run_id") == row["run_id"]), None)
            if match is not None:
                match["filed"] = filed["pr_url"]
    result["filed"] = sum(1 for row in record["runs"] if row.get("filed"))
    result["prescreens"] = prescreen_counts(record)
    result["status"] = effective_status(record)
    if is_terminal(record):
        # A filed hunt's record is the provenance for live pull requests. The
        # gate result that authorized the filing was overwritten once by a
        # later recheck, and there was no way back to it. Reading a finished
        # hunt is free; writing to one is not allowed.
        # https://github.com/wolfgang-aura/Mailman/issues/72
        result["persisted"] = False
        return result
    record["last_check"] = result
    record["checks"] = [*record.get("checks", []), {
        "requested": result["requested"], "ready": result["ready"],
        "remaining": result["remaining"], "ready_to_ask": result["ready_to_ask"],
        "checked_at": result["checked_at"],
    }][-CHECK_HISTORY:]
    checkpoint = write_checkpoint(root, record, result)
    if checkpoint:
        result["checkpoint"] = str(checkpoint)
    save(hunt_path(root, record["hunt_id"]), record)
    return result


def write_checkpoint(root: Path, record: dict, result: dict) -> Path | None:
    """Publish the candidates that are ready now, without waiting for the quota.

    Two PDM candidates were finished hours before filing, but the packet is
    only written when the whole quota is ready, so the operator saw nothing.
    Their readiness evidence then aged out and the visible count fell from two
    to zero. A checkpoint is a generated page, not a promise: `hunt finish`
    still re-checks freshness for every candidate immediately before filing.
    See https://github.com/wolfgang-aura/Mailman/issues/64.
    """
    from mailman.review_packet import write_packet_page
    from mailman.review_page import write_run_page

    # A filed candidate is ready and stays out of every packet: its pull
    # request is already open upstream, so publishing it again asks the
    # operator to approve something that has happened.
    # https://github.com/wolfgang-aura/Mailman/issues/97
    prs = [row["run_id"] for row in result["runs"]
           if row["ready"] and not row.get("filed")]
    asks = [row["run_id"] for row in result["runs"]
            if row.get("disposition") == READY_TO_ASK]
    ready = prs + asks
    if not ready:
        return None
    if record.get("checkpoint_runs") == ready:
        return Path(record["checkpoint"])
    directories = [root / run_id for run_id in ready]
    for directory in directories:
        write_run_page(directory)
    destination = hunt_path(root, record["hunt_id"]).parent / "checkpoint.html"
    title = f"{len(prs)} of {record['requested']} candidates ready so far"
    if asks:
        title += f", {len(asks)} ready to ask"
    write_packet_page(directories, destination, title=title)
    record["checkpoint"] = str(destination)
    record["checkpoint_runs"] = ready
    record["checkpoint_at"] = utc_now()
    return destination


def refresh(root: Path, record: dict, *, include_ready: bool = False) -> dict:
    """Re-run the aging evidence for every ready candidate, as one batch.

    Duplicate searches and claim reads expire in an hour. Two finished
    candidates were withheld while a third was still in review, aged out one at
    a time, and the visible ready count fell from two to zero. Refreshing them
    together is what the coordinator was doing by hand.

    This does not weaken the filing gate. `hunt finish` still re-checks every
    candidate and still refuses stale evidence; this only makes getting them
    fresh together a single command.
    See https://github.com/wolfgang-aura/Mailman/issues/69.
    """
    from mailman.claims import read_claims
    from mailman.submission import record_duplicate_search

    before = status(root, record)
    refreshed: list[dict] = []
    for row in before["runs"]:
        if row.get("dropped"):
            continue
        run, directory = load_run(row["run_id"], root)
        # A candidate that never reached a handoff has an earlier problem than
        # aging. Mid-hunt, a ready candidate needs no refresh either: the runs
        # this is for are the finished ones whose evidence expired while
        # another candidate was still in review.
        #
        # Immediately before filing, the opposite is true. The ready ones are
        # exactly the ones about to be pushed, and a duplicate has appeared 94
        # minutes after a run finished. `include_ready` is that pass.
        # https://github.com/wolfgang-aura/Mailman/issues/41
        # An ask-first candidate has only its offer handoff, and refreshing
        # its claims is how a maintainer's answer to the offer is read. #138.
        if (row["ready"] and not include_ready) or (
                load_handoff(directory) is None and load_offer_handoff(directory) is None):
            continue
        outcome = {"run_id": run.run_id}
        search = read_object(directory / "duplicate-search.json")
        if not search.get("query"):
            outcome["duplicate_search"] = "no recorded query; run duplicate-search first"
        else:
            fresh = record_duplicate_search(
                directory,
                repository=run.repository,
                query=search["query"],
                issue_number=search.get("issue_number"),
                symbols=search.get("symbols") or (),
            )
            outcome["duplicate_search"] = {
                "success": fresh["success"], "complete": fresh["complete"],
                "matches": fresh.get("match_count", 0),
                "decided_by": fresh.get("decided_by"),
            }
        claims = read_claims(
            directory,
            maintainers=load_maintainer_logins(
                root, repository_slug(str(run.repository or ""))
            ),
        )
        outcome["claims"] = {"success": claims.get("success"),
                             "assignees": claims.get("assignees")}
        refreshed.append(outcome)
    after = status(root, record)
    return {**after, "refreshed": refreshed,
            "ready_before_refresh": before["ready"]}


def finish(root: Path, record: dict) -> dict:
    from mailman.review_packet import write_packet_page
    from mailman.review_page import write_run_page
    if is_terminal(record):
        if record["status"] == "ABANDONED":
            raise ValueError(
                f"hunt {record['hunt_id']} is ABANDONED; it was closed rather "
                "than filed, and nothing is left to gate"
            )
        raise ValueError(
            f"hunt {record['hunt_id']} is {record['status']}; its packet and gate "
            "result are the record for pull requests that are already open"
        )
    result = status(root, record)
    if result["remaining"]:
        return {**result, "complete": False}
    # A filed row satisfies its slot without joining the packet. The packet is
    # what the operator approves for filing, and its pull request is already
    # open. https://github.com/wolfgang-aura/Mailman/issues/97
    directories = [root / row["run_id"] for row in result["runs"]
                   if row["ready"] and not row.get("filed")][:record["requested"]]
    if not directories:
        # Every requested slot is filled by a pull request that is already
        # open. `hunt file` moves a hunt to FILED once the last requested
        # filing is recorded, and `finish` refuses a terminal hunt above, so
        # reaching here means the record was left behind. Say that rather than
        # write a packet with nothing in it.
        raise ValueError(
            f"hunt {record['hunt_id']} has {result['filed']} filed pull "
            f"request(s) and no unfiled candidate to package; record the "
            "remaining filings with `hunt file` or add a candidate"
        )
    # Ask-first candidates ride along so their offer comments are approved in
    # the same pass. They are not PRs and filled no slot above.
    # https://github.com/wolfgang-aura/Mailman/issues/138
    asks = [root / row["run_id"] for row in result["runs"]
            if row.get("disposition") == READY_TO_ASK]
    directories += asks
    for directory in directories:
        write_run_page(directory)
    packet = hunt_path(root, record["hunt_id"]).parent / "index.html"
    title = "PRs awaiting filing approval"
    if asks:
        title += f", and {len(asks)} offer comment(s)"
    write_packet_page(directories, packet, title=title)
    record["status"] = "AWAITING_FILING_APPROVAL"
    record["packet"] = str(packet)
    record["packet_sha256"] = hashlib.sha256(packet.read_bytes()).hexdigest()
    save(hunt_path(root, record["hunt_id"]), record)
    return {**result, "complete": True, "packet": str(packet), "published": False}
