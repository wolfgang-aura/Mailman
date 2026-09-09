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
from mailman.handoff import check_handoff, load_handoff
from mailman.models import RunStatus, utc_now
from mailman.review_decision import DecisionError, load_decision
from mailman.screen import load_screen
from mailman.target_intel import repository_slug
from mailman.targeting import assess_target

PROCEDURE = Path(__file__).with_name("procedure.md")
#: How long one coordinator owns a hunt before another may take it over
#: without an argument. Long enough to cover a reviewer stage, short enough
#: that an abandoned hunt is not stuck for a day.
LEASE_MINUTES = 90
HUNT_TIME_BUDGET_SECONDS = 2 * 60 * 60

HUMAN_REASONS = ("authentication", "budget", "scope", "conflicting-instructions")
DROP_CODES = {
    "issue-assigned", "work-handed-over", "open-pull-request",
    "already-fixed-upstream", "bug-not-reproduced", "fails-freshness-bar",
    "reproduction-not-machine-checked",
}
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


def deadline(record: dict) -> datetime:
    """Return the hunt's fixed deadline, including for older records."""
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
    if datetime.now(UTC) >= expires:
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
                reviewer: str, reviewer_model: str, owner: str | None = None) -> dict:
    if count < 1 or isinstance(count, bool):
        raise ValueError("the PR count must be positive")
    if not primary_model.strip() or not reviewer_model.strip():
        raise ValueError("ask for both model IDs before starting the hunt")
    created_at = utc_now()
    created = datetime.fromisoformat(created_at)
    record = {
        "schema_version": 1, "hunt_id": new_run_id(), "requested": count,
        "data_root": str(root.resolve()), "created_at": created_at,
        "time_budget_seconds": HUNT_TIME_BUDGET_SECONDS,
        "deadline_at": (
            created + timedelta(seconds=HUNT_TIME_BUDGET_SECONDS)
        ).isoformat(),
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


def record_filing(root: Path, record: dict, run_id: str, *, pr_url: str,
                  commit: str | None = None) -> dict:
    """Write the pull request a candidate became, and close the hunt when done.

    This is the only thing that makes a filing visible to the next session. It
    runs after the operator approves and after the PR exists, so it takes the
    URL rather than creating anything.
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


def next_action(directory: Path) -> dict:
    run, _ = load_run(directory.name, directory.parent)

    def action(stage: str, command: str, detail: str = "", disposition: str = "REPAIR") -> dict:
        return {"run_id": run.run_id, "ready": False, "stage": stage,
                "action": command, "detail": detail, "disposition": disposition,
                "human_required": False}

    screen = load_screen(directory.parent, repository_slug(run.repository)) or {}
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
    assessment = assess_target(directory)
    if assessment.blocking:
        code = assessment.blocking[0]
        return action("target", f"mailman check-target {run.run_id}", "; ".join(assessment.blocking),
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
        history = read_object(directory / "orchestration.json")
        has_primary = any(step.get("name") == "agent:primary" for step in history.get("steps", []))
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
    if (not submission.get("ready")
            or submission.get("diff_sha256") != hashlib.sha256(exported_diff.read_text(encoding="utf-8").encode("utf-8")).hexdigest()):
        return action("submission", f"mailman prepare-submission {run.run_id} --policy POLICY.json",
                      "; ".join(submission.get("blocking_codes", [])))
    try:
        decision = load_decision(directory)
    except DecisionError as error:
        return action("decision", f"mailman decision {run.run_id}", str(error))
    if decision.recommendation != "SEND":
        return action("decision", "Resolve the HOLD or replace the candidate.", disposition="REPLACE" if decision.recommendation == "DROP" else "REPAIR")
    if decision.blocking_questions:
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
    return {"run_id": run.run_id, "ready": True, "stage": "filing-approval",
            "disposition": "READY", "human_required": False,
            "action": "Include in the final approval packet."}


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
        try:
            checked = next_action(directory)
        except (OSError, ValueError) as error:
            checked = {"run_id": run.run_id, "ready": False, "disposition": "REPAIR",
                       "human_required": False, "detail": str(error), "action": "Repair the named evidence record."}
        key = (repository_slug(run.repository), run.issue or run.defect_report)
        if checked["ready"] and key in targets:
            checked.update(ready=False, disposition="REPLACE", detail="same target already counted")
        if checked["ready"]:
            targets.add(key)
        rows.append(checked)
    ready = sum(row["ready"] for row in rows)
    result = {"hunt_id": record["hunt_id"], "requested": record["requested"],
              "ready": ready, "remaining": max(0, record["requested"] - ready),
              "checked_at": utc_now(), "runs": rows,
              "next": "Prepare the approval packet." if ready >= record["requested"] else "Complete the next action or find a replacement candidate.",
              "escalations": record["escalations"]}
    expires = deadline(record)
    result["deadline_at"] = expires.isoformat()
    result["deadline_expired"] = datetime.now(UTC) >= expires
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
        "remaining": result["remaining"], "checked_at": result["checked_at"],
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

    ready = [row["run_id"] for row in result["runs"] if row["ready"]]
    if not ready:
        return None
    if record.get("checkpoint_runs") == ready:
        return Path(record["checkpoint"])
    directories = [root / run_id for run_id in ready]
    for directory in directories:
        write_run_page(directory)
    destination = hunt_path(root, record["hunt_id"]).parent / "checkpoint.html"
    write_packet_page(
        directories,
        destination,
        title=f"{len(ready)} of {record['requested']} candidates ready so far",
    )
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
        if (row["ready"] and not include_ready) or load_handoff(directory) is None:
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
        claims = read_claims(directory)
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
    directories = [root / row["run_id"] for row in result["runs"] if row["ready"]][:record["requested"]]
    for directory in directories:
        write_run_page(directory)
    packet = hunt_path(root, record["hunt_id"]).parent / "index.html"
    write_packet_page(directories, packet, title="PRs awaiting filing approval")
    record["status"] = "AWAITING_FILING_APPROVAL"
    record["packet"] = str(packet)
    record["packet_sha256"] = hashlib.sha256(packet.read_bytes()).hexdigest()
    save(hunt_path(root, record["hunt_id"]), record)
    return {**result, "complete": True, "packet": str(packet), "published": False}
