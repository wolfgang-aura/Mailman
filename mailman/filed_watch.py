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
import re
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

#: How many other open pull requests to ask about a failing check before
#: calling it the base branch's problem, and how many must answer.
PEER_SAMPLE = 5
PEER_MINIMUM = 3
#: `path/to/file.ext:LINE` in a check run's output text or summary.
_FILE_LINE = re.compile(r"(?<![\w/.-])(/?[\w.-]+(?:/[\w.-]+)*\.[A-Za-z]\w*):\d+")

#: The one word the table prints per row, in the order they are worth reading.
STATUS_ATTENTION = "attention"
STATUS_UNKNOWN = "unknown"
#: Every failing check is one the base branch or its tooling broke.
STATUS_INHERITED = "inherited"
STATUS_APPROVED = "approved"
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


def _is_standing_approval(last_outside: dict[str, Any] | None,
                          last_ours: datetime | None) -> bool:
    """True when the newest word from outside is an approval that still stands.

    An approval is not an unanswered comment: it asks for nothing and the next
    move is the maintainer's. An approval older than our own last push is not
    standing, because the reviewer approved a head we have since replaced.
    See https://github.com/wolfgang-aura/Mailman/issues/130.
    """
    if not last_outside or last_outside.get("kind") != "review":
        return False
    if last_outside.get("review_state") != "APPROVED":
        return False
    at = _read_timestamp(last_outside.get("at"))
    if at is None:
        return False
    return last_ours is None or at > last_ours


def _reasons_for(pull: dict[str, Any], checks: dict[str, Any],
                 last_outside: dict[str, Any] | None,
                 last_ours: datetime | None,
                 inherited: dict[str, str] | None = None) -> list[str]:
    reasons: list[str] = []
    ours = [name for name in checks["failing"] if name not in (inherited or {})]
    if ours:
        reasons.append("failing check: " + ", ".join(ours))
    mergeable = pull.get("mergeable_state")
    if mergeable in STUCK_MERGEABLE_STATES:
        reasons.append(f"mergeable_state {mergeable}")
    if last_outside is not None and not _is_standing_approval(last_outside, last_ours):
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
        "foreign_commits": [],
        "foreign_approvals": [],
        "inherited": {},
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

    checks, latest_checks = _read_checks(gh, slug, result["head_sha"])
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
                if kind == "review":
                    # `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`.
                    last_outside["review_state"] = str(entry.get("state") or "").upper()
    head_repo = (pull.get("head") or {}).get("repo") or {}
    owners = {login for login in (author, (head_repo.get("owner") or {}).get("login"))
              if login}
    foreign = _foreign_commits(commits, owners)
    result["foreign_commits"] = foreign
    foreign_shas = {entry["sha"] for entry in foreign}
    result["foreign_approvals"] = [
        {"login": (entry.get("user") or {}).get("login"),
         "sha": entry.get("commit_id"), "at": entry.get("submitted_at")}
        for entry in (reviews if isinstance(reviews, list) else [])
        if isinstance(entry, dict)
        and str(entry.get("state") or "").upper() == "APPROVED"
        and entry.get("commit_id") in foreign_shas
    ]
    for entry in commits if isinstance(commits, list) else []:
        if not isinstance(entry, dict) or entry.get("sha") in foreign_shas:
            # A maintainer's push does not answer the maintainer.
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
    if checks["failing"]:
        result["inherited"] = _classify_inherited(
            gh, slug, number, checks, latest_checks,
            base_ref=(pull.get("base") or {}).get("ref"),
        )
    reasons = _reasons_for(pull, checks, last_outside, last_ours, result["inherited"])
    result["reasons"] = reasons
    if reasons:
        result["status"] = STATUS_ATTENTION
    elif result["inherited"]:
        result["status"] = STATUS_INHERITED
    elif _is_standing_approval(last_outside, last_ours):
        result["status"] = STATUS_APPROVED
    else:
        result["status"] = STATUS_OK
    return result


def _classify_inherited(gh: _Gh, slug: str, number: int, checks: dict[str, Any],
                        runs: dict[str, dict[str, Any]], *,
                        base_ref: str | None = None) -> dict[str, str]:
    """Each failing check the pull request did not cause, with the evidence.

    tqdm#1837 failed `pre-commit.ci - pr` because a newer flake8-bugbear fired
    on two lines already on master; 8 of 10 open tqdm pull requests failed it.
    First evidence: the file:line locations the check reports, from its output
    and annotations, all lie outside the files this pull request touches. A
    test file is not evidence on its own, because a change can break a test it
    never edits (edgartools#1329, #115). Failing that: most of up to five
    other open pull requests fail the same check. Failing that: the base
    branch fails it at its own tip, as pymc main failed `test_step_args` under
    pymc#8442 (#162). The extra calls are made only for a row with a failing
    check, and one that fails leaves the check counted against us.
    https://github.com/wolfgang-aura/Mailman/issues/134
    """
    inherited: dict[str, str] = {}
    touched: set[str] | None = None
    undecided: list[str] = []
    for name in checks["failing"]:
        run = runs.get(name) or {}
        paths = _reported_paths(gh, slug, run) if run.get("output") else set()
        if paths and touched is None:
            files = gh.json(f"repos/{slug}/pulls/{number}/files?per_page=100")
            touched = {
                entry.get("filename") for entry in files if isinstance(entry, dict)
            } if isinstance(files, list) else set()
        if not paths or (touched and not _overlaps(paths, touched)
                         and any(_is_test_path(path) for path in paths)):
            undecided.append(name)
        elif touched and not _overlaps(paths, touched):
            inherited[name] = (
                "fails only in files this pull request does not touch: "
                + ", ".join(sorted(paths))
            )
    if undecided:
        inherited.update(_compare_with_peers(gh, slug, number, undecided))
    undecided = [name for name in undecided if name not in inherited]
    if undecided and base_ref:
        base = _read_checks(gh, slug, base_ref)[0]
        for name in undecided:
            if name in base["failing"]:
                inherited[name] = f"the base branch {base_ref} fails it at its tip too"
    return inherited


def _overlaps(paths: set[str], touched: set[str]) -> bool:
    """True when a reported path names a touched file, allowing for CI prefixes."""
    return any(
        path == file or path.endswith("/" + file) or file.endswith("/" + path)
        for path in paths for file in touched
    )


def _is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    return any(part in ("test", "tests") for part in parts[:-1]) or parts[-1].startswith(
        "test"
    ) or parts[-1].rsplit(".", 1)[0].endswith("_test")


def _reported_paths(gh: _Gh, slug: str, run: dict[str, Any]) -> set[str]:
    output = run.get("output") or {}
    text = f"{output.get('summary') or ''}\n{output.get('text') or ''}"
    paths = {match.group(1).removeprefix("./") for match in _FILE_LINE.finditer(text)}
    if output.get("annotations_count") and run.get("id"):
        rows = gh.json(f"repos/{slug}/check-runs/{run['id']}/annotations?per_page=50")
        for row in rows if isinstance(rows, list) else []:
            # GitHub files job-level annotations ("Process completed with exit
            # code 1.") under `.github`, and a notice is not a failure (#251).
            if (isinstance(row, dict) and row.get("path")
                    and row["path"] != ".github"
                    and row.get("annotation_level") != "notice"):
                paths.add(row["path"])
    return paths


def _compare_with_peers(gh: _Gh, slug: str, number: int,
                        names: list[str]) -> dict[str, str]:
    listing = gh.json(f"repos/{slug}/pulls?state=open&per_page={PEER_SAMPLE + 1}")
    heads = [
        (entry.get("head") or {}).get("sha")
        for entry in (listing if isinstance(listing, list) else [])
        if isinstance(entry, dict) and entry.get("number") != number
    ][:PEER_SAMPLE]
    tally = {name: [0, 0] for name in names}
    for sha in filter(None, heads):
        peer, seen = _read_checks(gh, slug, sha)
        for name in names:
            if name in seen:
                tally[name][1] += 1
                if name in peer["failing"]:
                    tally[name][0] += 1
    return {
        name: f"{failed} of {asked} other open pull requests fail it too"
        for name, (failed, asked) in tally.items()
        if asked >= PEER_MINIMUM and failed * 2 > asked
    }


def _foreign_commits(commits: Any, owners: set[str]) -> list[dict[str, Any]]:
    """Commits on our head branch whose GitHub author is not the fork owner.

    A maintainer pushed two commits to openai-agents-python#4890 and another
    approved them; the watch said `approved` and nobody looked for a day. A
    commit with no linked GitHub account is not judged either way.
    https://github.com/wolfgang-aura/Mailman/issues/140
    """
    found: list[dict[str, Any]] = []
    for entry in commits if isinstance(commits, list) else []:
        if not isinstance(entry, dict):
            continue
        login = (entry.get("author") or {}).get("login")
        if login and login not in owners:
            found.append({
                "sha": entry.get("sha"),
                "author": login,
                "at": ((entry.get("commit") or {}).get("committer") or {}).get("date"),
            })
    return found


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
    return _read_checks(gh, slug, head_sha)[0]


def _read_checks(gh: _Gh, slug: str, head_sha: str | None,
                 ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The check summary for one commit, and the newest run or status per name."""
    checks: dict[str, Any] = {"total": 0, "pending": 0, "failing": []}
    if not head_sha:
        return checks, {}
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
    return checks, latest


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
    path = watch_path(data_root)
    previous = _previous_reading(path)
    result = {
        "schema_version": FILED_WATCH_SCHEMA_VERSION,
        "checked_at": moment.isoformat(),
        "previous_checked_at": previous.get("checked_at") if previous else None,
        "changes": changes_since(previous, readings),
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    result["path"] = str(path)
    return result


def _previous_reading(path: Path) -> dict[str, Any] | None:
    """The record the last watch wrote, or None when there is none to compare."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return previous if isinstance(previous, dict) else None


def _outside_text(outside: dict[str, Any]) -> str:
    kind = str(outside.get("kind") or "comment")
    if outside.get("review_state"):
        kind = f"{kind} ({str(outside['review_state']).lower()})"
    return f"new {kind} from {outside.get('login')} at {outside.get('at')}"


def changes_since(previous: dict[str, Any] | None,
                  readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What moved on each pull request since the last reading.

    The status column says what needs work now; this says what is new, so a
    scheduled watch can stay quiet when a row that already needed work has
    not moved. A row either reading could not read reports only its status,
    because a missing field there is a hole, not a change.
    """
    if not previous:
        return []
    before = {
        str(row.get("url")): row
        for row in previous.get("rows") or []
        if isinstance(row, dict)
    }
    changes: list[dict[str, Any]] = []
    for row in readings:
        name = f"{row['repository']}#{row['pull_request']}"
        old = before.get(str(row.get("url")))
        if old is None:
            changes.append({"url": row["url"], "pull_request": name,
                            "changes": [f"now watched ({row.get('status')})"]})
            continue
        found: list[str] = []
        if old.get("status") != row.get("status"):
            found.append(f"status {old.get('status')} -> {row.get('status')}")
        if STATUS_UNKNOWN not in (old.get("status"), row.get("status")):
            outside = row.get("last_outside") or {}
            old_outside = old.get("last_outside") or {}
            if outside and (outside.get("at"), outside.get("login")) != (
                    old_outside.get("at"), old_outside.get("login")):
                found.append(_outside_text(outside))
            failing = set((row.get("checks") or {}).get("failing") or [])
            old_failing = set((old.get("checks") or {}).get("failing") or [])
            for check in sorted(failing - old_failing):
                found.append(f"check {check} now failing")
            for check in sorted(old_failing - failing):
                found.append(f"check {check} no longer failing")
            if old.get("mergeable_state") != row.get("mergeable_state") and                     "unknown" not in (old.get("mergeable_state"), row.get("mergeable_state")):
                found.append(
                    f"mergeable_state {old.get('mergeable_state')} -> "
                    f"{row.get('mergeable_state')}"
                )
            old_foreign = {c.get("sha") for c in old.get("foreign_commits") or []}
            for commit in row.get("foreign_commits") or []:
                if commit.get("sha") not in old_foreign:
                    found.append(
                        f"commit {str(commit.get('sha'))[:7]} pushed by {commit.get('author')}"
                    )
        if found:
            changes.append({"url": row["url"], "pull_request": name, "changes": found})
    return changes


def _render_changes(result: dict[str, Any]) -> list[str]:
    since = result.get("previous_checked_at")
    if not since:
        return ["no earlier reading to compare; this one is the baseline"]
    changes = result.get("changes") or []
    if not changes:
        return [f"no changes since {since}"]
    lines = [f"{len(changes)} pull request(s) changed since {since}:"]
    for entry in changes:
        for change in entry["changes"]:
            # Matrix check names carry newlines; one change is one line.
            lines.append(f"  {entry['pull_request']}: {' '.join(change.split())}")
    return lines


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
            check_text = "FAIL " + ", ".join(
                name + (" (inherited)" if name in (row.get("inherited") or {}) else "")
                for name in checks["failing"]
            )
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
        elif row.get("status") == STATUS_APPROVED:
            outside = row.get("last_outside") or {}
            lines.append(
                f"  {row['repository']}#{row['pull_request']}: approved by "
                f"{outside.get('login') or 'a maintainer'} on "
                f"{str(outside.get('at'))[:10]}; awaiting merge"
            )
        elif row.get("status") == STATUS_UNKNOWN:
            lines.append(
                f"  {row['repository']}#{row['pull_request']}: could not read "
                f"({row.get('detail') or 'no detail'})"
            )
    for row in rows:
        name = f"{row['repository']}#{row['pull_request']}"
        for check, why in (row.get("inherited") or {}).items():
            lines.append(f"  {name}: {check} is inherited, not ours: {why}")
        commits = row.get("foreign_commits") or []
        if commits:
            lines.append(
                f"  {name}: {len(commits)} commit(s) not pushed by us: "
                + "; ".join(f"{str(c['sha'])[:7]} by {c['author']}" for c in commits)
                + " (build on this head; never force-push over it)"
            )
        for approval in row.get("foreign_approvals") or []:
            lines.append(
                f"  {name}: {approval['login']} approved {str(approval['sha'])[:7]}, "
                "a head we did not push"
            )
    lines.extend(_render_changes(result))
    count = len(result.get("needs_work") or [])
    if count:
        lines.append(f"{count} pull request(s) need work; record at {result.get('path')}")
    else:
        lines.append(
            f"every open pull request is green and answered; record at "
            f"{result.get('path')}"
        )
    return "\n".join(lines)
