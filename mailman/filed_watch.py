"""A standing watch over every pull request the harness has filed.

`hunt file` records the pull request URL and stops. After that a filed pull
request with a failing check, a base that has moved on, or a maintainer reply
nobody answered reads exactly like one that is fine: the hunt says `FILED`,
the ledger says `OPEN`, and nothing looks again. edgartools#1329 failed CI on
2026-09-17 01:23 UTC and was found by hand three hours later, by asking
`gh api` about every filed pull request one at a time.

This module asks the same questions for every row, prints one line per pull
request, and exits non-zero when any of them needs work. It writes what it
found to `.mailman/filed-watch.json` with a timestamp, so the last time the
watch ran and succeeded is on disk rather than in somebody's memory.
See https://github.com/wolfgang-aura/Mailman/issues/115.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.completion import read_object
from mailman.executor import CommandResult, execute
from mailman.provenance import load_provenance
from mailman.target_intel import _Gh, _is_bot, repository_slug
from mailman.toolchain import resolve_tool

FILED_WATCH_FILENAME = "filed-watch.json"
FILED_WATCH_SCHEMA_VERSION = 1

#: A check-run conclusion that means the pull request is red. `neutral` and
#: `skipped` are not failures, and `None` means the run has not finished.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
#: A `mergeable_state` that means the branch needs a push before it can merge.
#: `behind` is the base having moved past the merge base; `dirty` is a
#: conflict. `unknown` is GitHub still computing and is not held against the
#: row. `blocked` is a review or a required check, which the other columns say.
STUCK_MERGEABLE_STATES = frozenset({"behind", "dirty"})
#: A legacy commit status (the `/status` endpoint) that means the same.
#: pdm reports through statuses and has no check runs at all.
FAILING_STATUS_STATES = frozenset({"failure", "error"})
#: GitHub computes `mergeable_state` lazily and answers `unknown` on the first
#: read after a push. One more read, a moment later, is what its docs say to
#: do; pdm#3883 and openai-agents-python#4890 both answered `blocked` the
#: second time.
MERGEABLE_RETRY_SECONDS = 2.0

#: The one word the table prints per row, in the order they are worth reading.
STATUS_ATTENTION = "attention"
STATUS_UNKNOWN = "unknown"
STATUS_OK = "ok"
STATUS_MERGED = "merged"
STATUS_CLOSED = "closed"


def watch_path(data_root: Path) -> Path:
    """Where the last reading lives: beside `hunts/`, not inside any run."""
    return data_root.parent / FILED_WATCH_FILENAME


def _read_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def filed_rows(data_root: Path) -> list[dict[str, Any]]:
    """Every filed pull request either ledger knows, one row per pull request.

    Two ledgers hold filings. `hunts/*/hunt.json` carries a `filed` block on
    each row `hunt file` recorded; `runs/*/submission/provenance.json` carries
    a `pull_request` for every run whose provenance was written, including
    runs filed before hunts existed. A pull request in either is a row here;
    one in both is one row that names both sources.
    """
    rows: dict[tuple[str, int], dict[str, Any]] = {}

    def row_for(repository: str, number: int) -> dict[str, Any]:
        slug = repository_slug(repository)
        key = (slug.lower(), int(number))
        if key not in rows:
            rows[key] = {
                "repository": slug,
                "pull_request": int(number),
                "url": f"https://github.com/{slug}/pull/{int(number)}",
                "run_id": None,
                "hunt_id": None,
                "filed_at": None,
                "filed_commit": None,
                "superseded_by": None,
                "sources": [],
            }
        return rows[key]

    hunts_directory = data_root.parent / "hunts"
    for path in sorted(hunts_directory.glob("*/hunt.json")):
        record = read_object(path)
        if not record:
            continue
        for entry in record.get("runs", []):
            filed = entry.get("filed")
            if not filed or not filed.get("repository") or not filed.get("pr_number"):
                continue
            row = row_for(filed["repository"], filed["pr_number"])
            row["run_id"] = row["run_id"] or entry.get("run_id")
            row["hunt_id"] = row["hunt_id"] or record.get("hunt_id")
            row["filed_at"] = row["filed_at"] or filed.get("filed_at")
            row["filed_commit"] = row["filed_commit"] or filed.get("commit")
            row["sources"].append("hunt")

    if data_root.is_dir():
        for directory in sorted(p for p in data_root.glob("*") if p.is_dir()):
            record = load_provenance(directory)
            if not record or not record.get("pull_request") or not record.get("repository"):
                continue
            row = row_for(record["repository"], record["pull_request"])
            row["run_id"] = row["run_id"] or record.get("run_id") or directory.name
            row["filed_at"] = row["filed_at"] or record.get("recorded_at")
            commits = record.get("commits") or []
            row["filed_commit"] = row["filed_commit"] or (commits[-1] if commits else None)
            row["superseded_by"] = row["superseded_by"] or record.get("superseded_by")
            row["sources"].append("provenance")

    return sorted(rows.values(), key=lambda row: (row["repository"].lower(), row["pull_request"]))


def _reasons_for(pull: dict[str, Any], checks: dict[str, Any],
                 last_outside: dict[str, Any] | None,
                 last_ours: datetime | None) -> list[str]:
    reasons: list[str] = []
    if checks["failing"]:
        reasons.append("failing check: " + ", ".join(checks["failing"]))
    mergeable = pull.get("mergeable_state")
    if mergeable in STUCK_MERGEABLE_STATES:
        reasons.append(f"mergeable_state {mergeable}")
    if last_outside is not None:
        outside_at = _read_timestamp(last_outside.get("at"))
        if outside_at is not None and (last_ours is None or outside_at > last_ours):
            reasons.append(
                f"unanswered comment from {last_outside['login']} at "
                f"{last_outside['at']}"
            )
    return reasons


def inspect_pull_request(gh: _Gh, row: dict[str, Any], *,
                         now: datetime | None = None,
                         retry_delay_seconds: float = MERGEABLE_RETRY_SECONDS,
                         ) -> dict[str, Any]:
    """One row's reading from GitHub, or the reason it could not be read.

    Seven calls: the pull request, the check runs and the commit statuses on
    its head, the issue comments, the reviews, the review comments and the
    commits. A failure on the first makes the row `unknown`; a failure on any
    later one is recorded and the row is still `unknown`, because a reading
    with a column missing is not one the exit code can vouch for.
    """
    moment = now or datetime.now(UTC)
    slug = row["repository"]
    number = row["pull_request"]
    result: dict[str, Any] = {
        **row,
        "state": None,
        "mergeable_state": None,
        "head_sha": None,
        "author": None,
        "checks": {"total": 0, "pending": 0, "failing": []},
        "last_outside": None,
        "last_ours_at": None,
        "updated_at": None,
        "days_since_update": None,
        "status": STATUS_UNKNOWN,
        "reasons": [],
        "detail": None,
        "checked_at": moment.isoformat(),
    }
    failures_before = len(gh.failures)
    pull = gh.json(f"repos/{slug}/pulls/{number}")
    if (isinstance(pull, dict) and pull.get("state") == "open"
            and pull.get("mergeable_state") == "unknown"):
        if retry_delay_seconds > 0:
            time.sleep(retry_delay_seconds)
        again = gh.json(f"repos/{slug}/pulls/{number}")
        if isinstance(again, dict) and "state" in again:
            pull = again
    if not isinstance(pull, dict) or "state" not in pull:
        message = None
        if isinstance(pull, dict):
            message = pull.get("message")
        result["detail"] = (
            f"repos/{slug}/pulls/{number}: "
            + (message or _last_error(gh) or "gh gave no reason")
        )
        return result

    author = (pull.get("user") or {}).get("login") or ""
    result["author"] = author
    result["head_sha"] = (pull.get("head") or {}).get("sha")
    result["mergeable_state"] = pull.get("mergeable_state")
    result["updated_at"] = pull.get("updated_at")
    if pull.get("merged") or pull.get("merged_at"):
        result["state"] = "merged"
    else:
        result["state"] = (pull.get("state") or "").lower() or None
    updated = _read_timestamp(pull.get("updated_at"))
    if updated is not None:
        result["days_since_update"] = max(0, (moment - updated).days)

    checks = _check_runs(gh, slug, result["head_sha"])
    result["checks"] = checks

    comments = gh.json(f"repos/{slug}/issues/{number}/comments?per_page=100")
    reviews = gh.json(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
    review_comments = gh.json(f"repos/{slug}/pulls/{number}/comments?per_page=100")
    commits = gh.json(f"repos/{slug}/pulls/{number}/commits?per_page=100")

    if len(gh.failures) > failures_before:
        result["detail"] = _last_error(gh) or "a column could not be read"
        return result

    last_outside: dict[str, Any] | None = None
    last_ours: datetime | None = None
    for kind, entries, stamp in (
        ("comment", comments, "created_at"),
        ("review", reviews, "submitted_at"),
        ("review comment", review_comments, "created_at"),
    ):
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user") or {}
            login = user.get("login") or ""
            at = _read_timestamp(entry.get(stamp))
            if at is None:
                continue
            if login == author:
                if last_ours is None or at > last_ours:
                    last_ours = at
                continue
            if _is_bot(user):
                continue
            if last_outside is None or at > _read_timestamp(last_outside["at"]):
                last_outside = {"login": login, "at": at.isoformat(), "kind": kind}
    for entry in commits if isinstance(commits, list) else []:
        if not isinstance(entry, dict):
            continue
        commit = entry.get("commit") or {}
        for who in ("committer", "author"):
            at = _read_timestamp((commit.get(who) or {}).get("date"))
            if at is not None and (last_ours is None or at > last_ours):
                last_ours = at
    result["last_outside"] = last_outside
    result["last_ours_at"] = last_ours.isoformat() if last_ours else None

    if result["state"] == "merged":
        result["status"] = STATUS_MERGED
        return result
    if result["state"] != "open":
        result["status"] = STATUS_CLOSED
        return result
    reasons = _reasons_for(pull, checks, last_outside, last_ours)
    result["reasons"] = reasons
    result["status"] = STATUS_ATTENTION if reasons else STATUS_OK
    return result


def _last_error(gh: _Gh) -> str | None:
    """The stderr of the last `gh` call that failed, for the `detail` column."""
    for command in reversed(gh.commands):
        if command.get("timed_out"):
            return f"timed out after {command.get('timeout_seconds')}s"
        if command.get("exit_code") != 0:
            text = (command.get("stderr") or command.get("stdout") or "").strip()
            return text.splitlines()[0] if text else None
    return None


def _check_runs(gh: _Gh, slug: str, head_sha: str | None) -> dict[str, Any]:
    checks: dict[str, Any] = {"total": 0, "pending": 0, "failing": []}
    if not head_sha:
        return checks
    payload = gh.json(f"repos/{slug}/commits/{head_sha}/check-runs?per_page=100")
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    combined = gh.json(f"repos/{slug}/commits/{head_sha}/status")
    statuses = combined.get("statuses") if isinstance(combined, dict) else None
    latest: dict[str, dict[str, Any]] = {}
    for status in statuses if isinstance(statuses, list) else []:
        if not isinstance(status, dict):
            continue
        name = str(status.get("context") or "")
        state = status.get("state")
        # The combined status keeps one entry per context, already the newest.
        latest[name] = {
            "status": "pending" if state == "pending" else "completed",
            "conclusion": "failure" if state in FAILING_STATUS_STATES else state,
            "_stamp": status.get("updated_at") or "",
        }
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "")
        # A re-run leaves the earlier attempt in the listing. Keep the newest
        # per name so a check that failed and then passed reads as passing.
        stamp = run.get("started_at") or run.get("completed_at") or ""
        if name not in latest or str(stamp) >= str(latest[name].get("_stamp", "")):
            latest[name] = {**run, "_stamp": stamp}
    checks["total"] = len(latest)
    for name, run in sorted(latest.items()):
        if run.get("status") != "completed":
            checks["pending"] += 1
        elif run.get("conclusion") in FAILING_CONCLUSIONS:
            checks["failing"].append(name)
    return checks


def watch_filed(
    data_root: Path,
    *,
    executable: str | None = None,
    timeout_seconds: float = 30,
    working_directory: Path | None = None,
    now: datetime | None = None,
    retry_delay_seconds: float = MERGEABLE_RETRY_SECONDS,
    _execute: Callable[..., CommandResult] | None = None,
) -> dict[str, Any]:
    """Read every filed pull request, write the record, and say what needs work.

    The record is written whether or not anything needs work: its timestamp
    is the last time the watch ran to completion, which is the health signal
    a cron job leaves behind.
    """
    moment = now or datetime.now(UTC)
    rows = filed_rows(data_root)
    home = working_directory or data_root.parent
    home.mkdir(parents=True, exist_ok=True)
    gh = _Gh(executable or resolve_tool(home, "gh"), home, timeout_seconds,
             _execute or execute)
    readings = [
        inspect_pull_request(gh, row, now=moment,
                             retry_delay_seconds=retry_delay_seconds)
        for row in rows
    ]
    needs_work = [
        reading for reading in readings
        if reading["status"] in (STATUS_ATTENTION, STATUS_UNKNOWN)
    ]
    result = {
        "schema_version": FILED_WATCH_SCHEMA_VERSION,
        "checked_at": moment.isoformat(),
        "data_root": str(data_root),
        "rows": readings,
        "needs_work": [
            {"url": reading["url"], "status": reading["status"],
             "reasons": reading["reasons"], "detail": reading["detail"]}
            for reading in needs_work
        ],
        "ok": not needs_work,
        "gh_failures": list(gh.failures),
    }
    path = watch_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    result["path"] = str(path)
    return result


def render_watch(result: dict[str, Any]) -> str:
    """One line per pull request, then one line saying whether to act."""
    rows = result.get("rows") or []
    if not rows:
        return "no filed pull requests in either ledger"
    columns = ("PR", "STATE", "MERGEABLE", "CHECKS", "LAST OUTSIDE", "AGE", "STATUS")
    table: list[tuple[str, ...]] = []
    for row in rows:
        checks = row.get("checks") or {}
        if row.get("status") == STATUS_UNKNOWN:
            check_text = "-"
        elif checks.get("failing"):
            check_text = "FAIL " + ", ".join(checks["failing"])
        elif checks.get("pending"):
            check_text = f"pending {checks['pending']}/{checks['total']}"
        elif checks.get("total"):
            check_text = f"pass {checks['total']}"
        else:
            check_text = "none"
        outside = row.get("last_outside")
        outside_text = (
            f"{outside['login']} {str(outside['at'])[:10]}" if outside else "-"
        )
        age = row.get("days_since_update")
        table.append((
            f"{row['repository']}#{row['pull_request']}",
            row.get("state") or "-",
            row.get("mergeable_state") or "-",
            check_text,
            outside_text,
            f"{age}d" if age is not None else "-",
            row.get("status") or STATUS_UNKNOWN,
        ))
    widths = [
        max(len(columns[index]), *(len(line[index]) for line in table))
        for index in range(len(columns))
    ]
    lines = ["  ".join(name.ljust(widths[index]) for index, name in enumerate(columns))]
    for line in table:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)))
    for row in rows:
        if row.get("status") == STATUS_ATTENTION:
            for reason in row.get("reasons") or []:
                lines.append(f"  {row['repository']}#{row['pull_request']}: {reason}")
        elif row.get("status") == STATUS_UNKNOWN:
            lines.append(
                f"  {row['repository']}#{row['pull_request']}: could not read "
                f"({row.get('detail') or 'no detail'})"
            )
    count = len(result.get("needs_work") or [])
    if count:
        lines.append(f"{count} pull request(s) need work; record at {result.get('path')}")
    else:
        lines.append(
            f"every open pull request is green and answered; record at "
            f"{result.get('path')}"
        )
    return "\n".join(lines)
