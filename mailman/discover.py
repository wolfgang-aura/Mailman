"""Find fresh bug reports across a curated list of repositories.

The procedure starts from a repository screen, and a screen costs about two
hundred API calls. Which repositories to screen was left to the coordinator,
who searched by hand in scratch scripts and screened repositories whose
refusal was already on disk. On 2026-10-02 four of eight such screens failed
on a gate a stored screen had already answered. This command asks one cheap
question first: which listed repositories have an open, unassigned bug report
from the last N days with no linked pull request. Mailman #384.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.claims import _row as claim_row
from mailman.claims import classify_thread, rival_pull_requests
from mailman.hunt import maintainer_engaged
from mailman.maintainers import MAINTAINER_ASSOCIATIONS
from mailman.prescreen import load_prescreen
from mailman.screen import load_screen, refusal_stands
from mailman.targeting import claim_is_stale

#: The tracked list of recognizable Python repositories, one slug per line.
REPOSITORIES_FILE = Path(__file__).with_name("discover-repos.txt")
#: Repositories with one of our pull requests open; one per repository.
OPEN_PR_REPOSITORIES = Path(".mailman") / "open-pr-repos.txt"
#: GitHub refuses a search query much longer than this.
QUERY_REPOSITORY_CHARACTERS = 170
#: Repositories label bug reports differently; a comma is an OR.
BUG_LABELS = 'bug,"type: bug","type:bug","T: bug","type/bug","kind/bug","C-bug","Type: Bug"'
#: Hits kept per repository, freshest first, before any timeline is read.
#: A repository with a hundred open bugs needs only its newest few looked
#: at. Mailman #389.
PER_REPOSITORY = 5
#: Seconds between searches. Eight still met the secondary rate limit.
SEARCH_SPACING_SECONDS = 15.0
#: An answered batch is reused this long, so a stopped pass resumes instead
#: of starting over. Mailman #395.
CACHE_SECONDS = 6 * 3600

Search = Callable[[str], "list[dict[str, Any]] | None"]
Timeline = Callable[[str, int], "list[dict[str, Any]] | None"]


def read_repository_list(path: Path | str) -> list[str]:
    """Slugs from a list file, comments and blank lines skipped, order kept.

    A string that names no file is the list itself, comma or space separated
    (#403).
    """
    seen: set[str] = set()
    slugs: list[str] = []
    text = (Path(path).read_text(encoding="utf-8") if Path(path).is_file()
            else str(path).replace(",", " "))
    for raw in text.splitlines():
        for slug in raw.split("#", 1)[0].split():
            key = slug.lower()
            if key not in seen:
                seen.add(key)
                slugs.append(slug)
    return slugs


def skipped_repositories(
    slugs: Iterable[str], data_root: Path, excluded: Iterable[str]
) -> dict[str, str]:
    """Each slug not worth a search, with the reason."""
    excluded_keys = {slug.lower() for slug in excluded}
    skipped: dict[str, str] = {}
    for slug in slugs:
        if slug.lower() in excluded_keys:
            skipped[slug] = "excluded"
            continue
        screen = load_screen(data_root, slug)
        if (
            isinstance(screen, dict)
            and screen.get("success")
            and screen.get("verdict") != "pass"
            and refusal_stands(screen)
        ):
            failed = ", ".join(screen.get("failed_gates") or []) or "a gate"
            skipped[slug] = f"current screen failed on {failed}"
    return skipped


def query_batches(slugs: Sequence[str], limit: int = QUERY_REPOSITORY_CHARACTERS) -> list[list[str]]:
    """Slugs grouped so each group's `repo:` qualifiers fit in one query."""
    batches: list[list[str]] = []
    batch: list[str] = []
    for slug in slugs:
        trial = batch + [slug]
        if batch and len(" ".join(f"repo:{name}" for name in trial)) > limit:
            batches.append(batch)
            batch = [slug]
        else:
            batch = trial
    if batch:
        batches.append(batch)
    return batches


def build_query(batch: Sequence[str], since: str) -> str:
    repositories = " ".join(f"repo:{slug}" for slug in batch)
    return (
        f"{repositories} is:issue is:open label:{BUG_LABELS} "
        f"created:>{since} -linked:pr no:assignee"
    )


def _row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": str(item.get("repository_url", "")).split("repos/", 1)[-1],
        "number": item.get("number"),
        "title": item.get("title", ""),
        "created_at": str(item.get("created_at", ""))[:10],
        "comments": item.get("comments", 0),
        "author_association": item.get("author_association", ""),
        "labels": [label.get("name", "") for label in item.get("labels") or []],
        "engaged": item.get("author_association") in MAINTAINER_ASSOCIATIONS,
        "_author": (item.get("user") or {}).get("login"),
        # The report is the reporter's first comment: "I can send a PR" in
        # the body claims the work as a later comment would. Mailman #396.
        "_report": {
            "event": "commented",
            "user": item.get("user"),
            "author_association": item.get("author_association"),
            "body": item.get("body"),
            "created_at": item.get("created_at"),
        },
    }


def _cache_file(cache_directory: Path, query: str) -> Path:
    return cache_directory / f"{hashlib.sha1(query.encode('utf-8')).hexdigest()}.json"


def _cached(cache_directory: Path | None, query: str) -> list[dict[str, Any]] | None:
    """A batch's items answered within CACHE_SECONDS, else None."""
    if cache_directory is None:
        return None
    path = _cache_file(cache_directory, query)
    try:
        if time.time() - path.stat().st_mtime > CACHE_SECONDS:
            return None
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return items if isinstance(items, list) else None


def _store(cache_directory: Path | None, query: str, items: list[dict[str, Any]]) -> None:
    if cache_directory is None:
        return
    cache_directory.mkdir(parents=True, exist_ok=True)
    _cache_file(cache_directory, query).write_text(json.dumps(items), encoding="utf-8")


def discover(
    slugs: Sequence[str],
    *,
    data_root: Path,
    since: str,
    search: Search,
    timeline: Timeline | None = None,
    excluded: Iterable[str] = (),
    per_repository: int = PER_REPOSITORY,
    spacing_seconds: float = SEARCH_SPACING_SECONDS,
    progress: Callable[[str], None] = lambda line: None,
    cache_directory: Path | None = None,
) -> dict[str, Any]:
    """Search the listed repositories batch by batch and rank what comes back.

    A batch the search did not answer is recorded under `unsearched`, so a
    silent rate limit is never read as "no issues here".
    """
    skipped = skipped_repositories(slugs, data_root, excluded)
    searched = [slug for slug in slugs if slug not in skipped]
    rows: list[dict[str, Any]] = []
    unsearched: list[str] = []
    batches = query_batches(searched)
    searched_before = False
    for index, batch in enumerate(batches, 1):
        query = build_query(batch, since)
        items = _cached(cache_directory, query)
        if items is not None:
            progress(f"[{index}/{len(batches)}] {len(items)} from cache")
            rows.extend(_row(item) for item in items)
            continue
        if searched_before and spacing_seconds:
            time.sleep(spacing_seconds)
        searched_before = True
        items = search(query)
        if items is not None:
            _store(cache_directory, query, items)
        if items is None:
            unsearched.extend(batch)
            progress(f"[{index}/{len(batches)}] not answered: {' '.join(batch)}")
            continue
        progress(f"[{index}/{len(batches)}] {len(items)} from {len(batch)} repositories")
        rows.extend(_row(item) for item in items)
    # A stored prescreen rejection stands; reading the timeline again only
    # puts the issue back in front of the coordinator. Mailman #394.
    rejected: dict[str, list[str]] = {}
    unrejected = []
    for row in rows:
        record = load_prescreen(data_root, row["repository"], row["number"])
        if isinstance(record, dict) and record.get("verdict") == "reject":
            rejected[f"{row['repository']}#{row['number']}"] = list(record.get("blocking") or [])
        else:
            unrejected.append(row)
    rows = unrejected
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    kept_per: dict[str, int] = {}
    capped = []
    for row in rows:
        count = kept_per.get(row["repository"], 0)
        if count < per_repository:
            kept_per[row["repository"]] = count + 1
            capped.append(row)
    rows = capped
    # `-linked:pr` misses a pull request that only cross-references the
    # issue; one timeline read per hit drops it here. Mailman #385.
    claimed: dict[str, list[str]] = {}
    # A failed read is not "no rivals"; it stays out of the list. Mailman #390.
    unread: list[str] = []
    if timeline is not None:
        kept = []
        for index, row in enumerate(rows, 1):
            if index % 25 == 0 or index == len(rows):
                progress(f"timelines {index}/{len(rows)}")
            events = timeline(row["repository"], row["number"])
            name = f"{row['repository']}#{row['number']}"
            rivals = rival_pull_requests(events) + comment_claims([row["_report"], *(events or [])])
            if events is None:
                unread.append(name)
            elif rivals:
                claimed[name] = rivals
            else:
                row["engaged"] = row["engaged"] or maintainer_engaged(events, row["_author"])
                kept.append(row)
        rows = kept
    # A hunt counts only triaged runs, so a report a maintainer filed,
    # answered or labelled comes first. Mailman #393.
    for row in rows:
        row.pop("_author", None)
        row.pop("_report", None)
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    rows.sort(key=lambda row: not row["engaged"])
    return {
        "claimed": claimed,
        "rejected": rejected,
        "since": since,
        "searched": len(searched) - len(unsearched),
        "unsearched": unsearched,
        "unread_timelines": unread,
        "skipped": skipped,
        "issues": rows,
    }


def gh_search(executable: str = "gh", timeout_seconds: float = 60, retries: int = 3) -> Search:
    """A search that waits out the secondary rate limit, None when it gives up."""

    def search(query: str) -> list[dict[str, Any]] | None:
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [executable, "api", "-X", "GET", "search/issues",
                     "-f", f"q={query}", "-f", "per_page=100"],
                    capture_output=True, text=True, encoding="utf-8",
                    timeout=timeout_seconds,
                )
                data = json.loads(result.stdout or "{}")
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
                data = {}
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if attempt + 1 < retries:
                time.sleep(30 * (attempt + 1))
        return None

    return search


def comment_claims(events: list[dict[str, Any]] | None) -> list[str]:
    """Live "I'll take this" comments and maintainer handovers on a timeline.

    The same rules prescreen's claims stage blocks on: a stale outsider claim
    no longer holds the issue. Mailman #396.
    """
    comments = [
        event for event in events or []
        if isinstance(event, dict) and event.get("event") == "commented"
    ]
    holders: list[str] = []
    for comment, kind in zip(comments, classify_thread(comments)):
        if kind == "assignment" or (
            kind == "claim" and not claim_is_stale(claim_row(comment))
        ):
            login = (comment.get("user") or comment.get("actor") or {}).get("login")
            holders.append(f"comment by @{login}")
    return holders


def gh_timeline(executable: str = "gh", timeout_seconds: float = 60) -> Timeline:
    """One page of an issue's timeline, None when it cannot be read."""

    def timeline(slug: str, number: int) -> list[dict[str, Any]] | None:
        try:
            result = subprocess.run(
                [executable, "api", f"repos/{slug}/issues/{number}/timeline?per_page=100"],
                capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds,
            )
            data = json.loads(result.stdout or "null")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, list) else None

    return timeline


def default_since(days: int, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(days=days)).strftime("%Y-%m-%d")


def render_discovery(result: dict[str, Any]) -> str:
    lines = [f"discover: bug reports opened after {result['since']}"]
    for row in result["issues"]:
        labels = ",".join(row["labels"])[:40]
        lines.append(
            f"{'*' if row.get('engaged') else ' '} {row['repository'] + '#' + str(row['number']):44} {row['created_at']} "
            f"c={row['comments']:<3} {row['author_association'][:4]:4} {labels:40} {row['title'][:70]}"
        )
    lines.append(
        f"  {len(result['issues'])} issue(s), "
        f"{sum(1 for row in result['issues'] if row.get('engaged'))} triaged (*), "
        f"from {result['searched']} repositories searched; {len(result['skipped'])} skipped"
    )
    for name, rivals in sorted(result.get("claimed", {}).items()):
        lines.append(f"  claimed {name}: {', '.join(rivals)}")
    for name, blocking in sorted(result.get("rejected", {}).items()):
        lines.append(f"  rejected {name}: {', '.join(blocking) or 'prescreen'}")
    for slug, reason in sorted(result["skipped"].items()):
        if reason != "excluded":
            lines.append(f"  skipped {slug}: {reason}")
    if result["unsearched"]:
        lines.append(
            "  NOT SEARCHED (rate limit or error; rerun later): "
            + " ".join(result["unsearched"])
        )
    if result.get("unread_timelines"):
        lines.append(
            "  TIMELINE NOT READ (claim unknown; rerun later): "
            + " ".join(result["unread_timelines"])
        )
    return "\n".join(lines)


def stderr_progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)
