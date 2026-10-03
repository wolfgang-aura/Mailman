"""Build a list of repositories nobody has screened yet.

`discover` searches a tracked list and `hunt sweep` re-reads passing screens.
When both come back dry the procedure says to screen new repositories, but
nothing said where to find them. On 2026-10-03 the coordinator built a pool by
hand: the most-downloaded PyPI packages mapped to their GitHub repositories,
minus every screened one, then one GraphQL query per forty repositories kept
those that merge outside pull requests. That pool filled a hunt slot the
tracked list could not. Mailman #409.

The output is a plain slug list that `discover --repositories` reads.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mailman.screen import SCREENS_DIRECTORY

TOP_PACKAGES_URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
#: Repositories per GraphQL query; forty stays well under the node limit.
GRAPHQL_BATCH = 40
#: Merged pull requests read per repository.
MERGED_SAMPLE = 30
OUTSIDE_ASSOCIATIONS = frozenset({"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"})
_GITHUB = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.IGNORECASE)
#: Not repositories: sponsor pages, organisation-wide paths.
_NOT_REPOSITORIES = frozenset({"sponsors", "orgs", "apps", "features", "marketplace"})

TopPackages = Callable[[int], "list[str] | None"]
ProjectUrls = Callable[[str], "list[str] | None"]
GraphQL = Callable[[str], "dict[str, Any] | None"]


def repository_from_urls(urls: Iterable[str]) -> str | None:
    """The first owner/name GitHub slug among a package's project URLs."""
    for url in urls:
        match = _GITHUB.search(url or "")
        if match and match.group(1).lower() not in _NOT_REPOSITORIES:
            return f"{match.group(1)}/{match.group(2).removesuffix('.git')}"
    return None


def screened_repositories(data_root: Path) -> set[str]:
    """Lower-cased slugs with a stored screen, passing or not."""
    slugs: set[str] = set()
    for path in (data_root / SCREENS_DIRECTORY).glob("*.json"):
        try:
            slug = json.loads(path.read_text(encoding="utf-8")).get("repository")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if slug:
            slugs.add(str(slug).lower())
    return slugs


def build_query(slugs: Sequence[str]) -> str:
    parts = []
    for index, slug in enumerate(slugs):
        owner, name = slug.split("/", 1)
        parts.append(
            f"r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
            " nameWithOwner stargazerCount isArchived isFork primaryLanguage { name }"
            " issues(states: OPEN) { totalCount }"
            f" pullRequests(states: MERGED, last: {MERGED_SAMPLE},"
            " orderBy: {field: UPDATED_AT, direction: ASC}) {"
            " nodes { mergedAt authorAssociation author { login } } } }"
        )
    return "query { " + " ".join(parts) + " }"


def outside_authors(nodes: Iterable[dict[str, Any]], since: datetime) -> set[str]:
    """Human outside authors with a pull request merged after `since`."""
    authors = set()
    for node in nodes:
        login = str((node.get("author") or {}).get("login") or "")
        merged = node.get("mergedAt")
        if (
            not login
            or node.get("authorAssociation") not in OUTSIDE_ASSOCIATIONS
            or not merged
            or login.endswith("[bot]")
            or "bot" in login.lower()
        ):
            continue
        if datetime.fromisoformat(str(merged).replace("Z", "+00:00")) > since:
            authors.add(login)
    return authors


def build_pool(
    *,
    data_root: Path,
    top_packages: TopPackages,
    project_urls: ProjectUrls,
    graphql: GraphQL,
    top: int = 3000,
    excluded: Iterable[str] = (),
    language: str = "Python",
    min_stars: int = 500,
    min_outside_authors: int = 2,
    min_open_issues: int = 20,
    days: int = 30,
    now: datetime | None = None,
    workers: int = 16,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Unscreened repositories that merge outside work, most outside authors first.

    `unread` names every package or batch that could not be read; a caller
    treats a non-empty list as an incomplete pool, never as a dry one.
    """
    say = progress or (lambda _line: None)
    packages = top_packages(top)
    if packages is None:
        return {"pool": [], "unread": ["top packages"], "considered": 0, "read": 0}
    skip = screened_repositories(data_root) | {slug.lower() for slug in excluded}
    unread: list[str] = []
    candidates: dict[str, str] = {}
    with ThreadPoolExecutor(max(1, workers)) as executor:
        for index, (package, urls) in enumerate(
            zip(packages, executor.map(project_urls, packages)), start=1
        ):
            if index % 500 == 0:
                say(f"pool pypi {index}/{len(packages)}")
            if urls is None:
                unread.append(package)
                continue
            slug = repository_from_urls(urls)
            if slug and slug.lower() not in skip and slug.lower() not in candidates:
                candidates[slug.lower()] = slug
    slugs = list(candidates.values())
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    for start in range(0, len(slugs), GRAPHQL_BATCH):
        batch = slugs[start:start + GRAPHQL_BATCH]
        data = graphql(build_query(batch))
        say(f"pool graphql {min(start + GRAPHQL_BATCH, len(slugs))}/{len(slugs)}")
        if data is None:
            unread.append(f"graphql batch {start // GRAPHQL_BATCH + 1}")
            continue
        for index in range(len(batch)):
            # A renamed or deleted repository answers null; that is an answer.
            node = data.get(f"r{index}")
            if not node:
                continue
            rows.append({
                "repository": node["nameWithOwner"],
                "stars": node["stargazerCount"],
                "archived": node["isArchived"],
                "fork": node["isFork"],
                "language": (node.get("primaryLanguage") or {}).get("name"),
                "open_issues": node["issues"]["totalCount"],
                "outside_authors": len(outside_authors(node["pullRequests"]["nodes"], since)),
            })
    pool = [
        row for row in rows
        if not row["archived"] and not row["fork"]
        and row["language"] == language
        and row["stars"] >= min_stars
        and row["outside_authors"] >= min_outside_authors
        and row["open_issues"] >= min_open_issues
    ]
    pool.sort(key=lambda row: (-row["outside_authors"], -row["stars"]))
    return {"pool": pool, "unread": unread, "considered": len(slugs), "read": len(rows)}


def render_pool(result: dict[str, Any]) -> str:
    lines = [
        f"# {len(result['pool'])} unscreened repositories merging outside work"
        f" ({result['read']} of {result['considered']} read)",
        "# repository  outside-authors  stars",
    ]
    lines += [
        f"{row['repository']}  # {row['outside_authors']} outside, {row['stars']} stars"
        for row in result["pool"]
    ]
    if result["unread"]:
        lines.append(f"# UNREAD {len(result['unread'])}: {', '.join(result['unread'][:10])}")
    return "\n".join(lines) + "\n"


def pypi_top_packages(timeout_seconds: float = 60) -> TopPackages:
    def fetch(top: int) -> list[str] | None:
        try:
            with urllib.request.urlopen(TOP_PACKAGES_URL, timeout=timeout_seconds) as response:
                rows = json.load(response)["rows"]
        except (OSError, ValueError, KeyError):
            return None
        return [str(row["project"]) for row in rows[:top]]

    return fetch


def pypi_project_urls(timeout_seconds: float = 20) -> ProjectUrls:
    def fetch(package: str) -> list[str] | None:
        try:
            url = f"https://pypi.org/pypi/{package}/json"
            with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
                info = json.load(response)["info"]
        except urllib.error.HTTPError as error:
            # A removed package is an answer, not a failed read.
            return [] if error.code == 404 else None
        except (OSError, ValueError, KeyError):
            return None
        return [str(value) for value in (info.get("project_urls") or {}).values()] + [
            str(info.get("home_page") or "")
        ]

    return fetch


def gh_graphql(executable: str = "gh", timeout_seconds: float = 90) -> GraphQL:
    def query(text: str) -> dict[str, Any] | None:
        try:
            result = subprocess.run(
                [executable, "api", "graphql", "-f", f"query={text}"],
                capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds,
            )
            payload = json.loads(result.stdout or "{}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        # Partial data with NOT_FOUND errors is a normal answer for a renamed
        # repository; no data at all is a failed read.
        return data if isinstance(data, dict) else None

    return query
