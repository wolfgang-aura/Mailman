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

import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.claims import rival_pull_requests
from mailman.screen import load_screen, refusal_stands

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

Search = Callable[[str], "list[dict[str, Any]] | None"]
Timeline = Callable[[str, int], "list[dict[str, Any]] | None"]


def read_repository_list(path: Path) -> list[str]:
    """Slugs from a list file, comments and blank lines skipped, order kept."""
    seen: set[str] = set()
    slugs: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
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
    }


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
    for index, batch in enumerate(batches, 1):
        if index > 1 and spacing_seconds:
            time.sleep(spacing_seconds)
        items = search(build_query(batch, since))
        if items is None:
            unsearched.extend(batch)
            progress(f"[{index}/{len(batches)}] not answered: {' '.join(batch)}")
            continue
        progress(f"[{index}/{len(batches)}] {len(items)} from {len(batch)} repositories")
        rows.extend(_row(item) for item in items)
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
            rivals = rival_pull_requests(events)
            if events is None:
                unread.append(name)
            elif rivals:
                claimed[name] = rivals
            else:
                kept.append(row)
        rows = kept
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return {
        "claimed": claimed,
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
            f"  {row['repository'] + '#' + str(row['number']):44} {row['created_at']} "
            f"c={row['comments']:<3} {row['author_association'][:4]:4} {labels:40} {row['title'][:70]}"
        )
    lines.append(
        f"  {len(result['issues'])} issue(s) from {result['searched']} repositories searched; "
        f"{len(result['skipped'])} skipped"
    )
    for name, rivals in sorted(result.get("claimed", {}).items()):
        lines.append(f"  claimed {name}: {', '.join(rivals)}")
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
