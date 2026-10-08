"""Read how a target repository actually hands out and accepts work.

Every screen in here was run by hand on 2026-09-03 across ninety candidate
repositories, and the hand pass was most of that session's cost. Worse, it was
not recorded, so the next session would have re-derived it or, more likely,
skipped it. Three findings made the difference and none of them is a judgement
call:

- Counting outside merges by `author_association` treats dependabot as a
  `CONTRIBUTOR`. `PyCQA/bandit` looked alive on one such merge and has merged no
  human outside pull request since May 2026.
- Treating any referencing pull request as a claim reads a bot-policed target as
  fully saturated. On `langchain-ai/langchain` that subtraction left 3 unassigned
  open bugs; counting only open or merged pull requests as a claim left 53.
- The enforcement bot says what it enforces, in plain text, in the comment it
  leaves when it closes a pull request. Reading that is one API call and is worth
  more than every heuristic above.

See https://github.com/wolfgang-aura/Mailman/issues/34 and
https://github.com/wolfgang-aura/Mailman/issues/35.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from mailman.executor import CommandResult, execute
from mailman.issue import load_issue_record
from mailman.toolchain import resolve_tool

#: How recent an outside human merge has to be to count as fresh. Fourteen
#: days failed `copier-org/copier` and `ipython/ipython`, which merge outside
#: work every few weeks and answer strangers inside the responsiveness bar.
#: Freshness asks whether the door is open; `responsiveness` asks how fast.
#: Forty-five days failed `jd/tenacity`, nine outside authors in 90 days with
#: the latest merge 55 days old: release-burst repositories. Mailman #284.
#: Sixty days left 96 of 679 screens failing on freshness alone and PRHunt
#: with no candidates; 56 of them merged an outside pull request inside 180
#: days. Mailman #317.
#: By 2026-10-08 the pool was dry at 180 days across the top 12,000 packages;
#: the operator chose a year, accepting that a slow repository may leave a
#: pull request unreviewed for weeks.
FRESHNESS_WINDOW_DAYS = 365

TARGET_INTEL_FILENAME = "target-intel.json"
TARGET_INTEL_MARKDOWN = "target-intel.md"
TARGET_INTEL_SCHEMA_VERSION = 1

#: Author associations that mean the author is not on the maintainer team.
OUTSIDE_ASSOCIATIONS = frozenset(
    {"CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"}
)

#: A bot leaves an HTML marker in the comment it posts, so its rules can be
#: counted rather than read one at a time. `<!-- require-issue-link -->` and
#: `<!-- block-fork-main -->` are two of langchain's; sqlfluff's agentscan uses
#: `<!-- agentscan:possible-bot-comment:v1 -->`.
_MARKER = re.compile(r"<!--\s*([a-z0-9][a-z0-9:._-]{2,60})\s*-->", re.IGNORECASE)
#: A marker whose last segment is a hex id names one comment, not a rule:
#: CodeRabbit's `cr-comment:v1:a2ffbe0291ffaaafd86b4451`.
_HASH_TAIL = re.compile(r"[:._-][0-9a-f]{12,}$")
_LAYOUT_ENDS = ("_start", "_end")

#: `#7`, `#4321`, or `owner/repo#7`. The repository, when written, is kept so
#: another repository's issue is not read as this one's (#346).
_ISSUE_REFERENCE = re.compile(
    r"(?:(?<![\w.-])([\w.-]+/[\w.-]+)|(?<![\w/&]))#(\d{1,7})\b"
)
#: `github.com/owner/repo/issues/7`; a bare `issues/7` names no repository.
_ISSUE_URL_REFERENCE = re.compile(
    r"(?:github\.com/([\w.-]+/[\w.-]+)/)?issues/(\d{1,7})\b"
)
#: Branch names carry the reference when the body forgets to: `fix/issue-104`,
#: `issue_104`. Mailman's own convention, `mailman/issue-3497`, is one of these.
_BRANCH_REFERENCE = re.compile(r"issue[-_]?(\d{1,7})", re.IGNORECASE)


#: Tools an issue thread names when it says how everybody else behaves. The
#: list leaves out names that are also ordinary words ("requests", "black",
#: "git"), because a false comparison seeds a question nobody needs.
#: https://github.com/wolfgang-aura/Mailman/issues/124
_KNOWN_TOOLS = (
    "poppler", "pdftotext", "mutool", "mupdf", "pdf.js", "pdfjs", "pdfium",
    "qpdf", "ghostscript", "xpdf", "pdfminer", "pymupdf", "pikepdf",
    "pdfplumber", "pypdf", "pypdf2", "acrobat", "firefox", "chromium",
    "safari", "curl", "wget", "httpx", "aiohttp", "urllib3", "numpy",
    "pandas", "polars", "pyarrow", "scipy", "gcc", "clang", "msvc", "ruff",
    "flake8", "pylint", "isort", "mypy", "pyright", "prettier", "eslint",
    "openssl", "libxml2", "lxml", "sqlite", "postgres", "postgresql",
    "mysql", "zarr", "h5py", "netcdf4", "xarray", "dask", "fsspec", "s3fs",
    "boto3", "pyyaml", "ruamel", "tomli", "tomllib", "orjson", "ujson",
    "simplejson", "pydantic", "marshmallow", "django", "flask", "fastapi",
    "starlette", "pytest", "ffmpeg", "imagemagick", "pillow", "libvips",
)
_TOOL_NAME = re.compile(
    r"(?<![\w.-])(?:"
    + "|".join(re.escape(name) for name in _KNOWN_TOOLS)
    + r")(?![\w-])",
    re.IGNORECASE,
)
_OTHER_TOOLS = re.compile(
    r"\bother (?:tools|libraries|implementations|projects|parsers|readers|"
    r"viewers|browsers|clients|packages|renderers|engines)\b",
    re.IGNORECASE,
)
_QUANTIFIER = re.compile(r"\b(?:all|both|every|each|most|none|neither)\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)


def tool_comparisons(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sentences in an issue thread that say how other tools behave.

    A sentence counts when it names two known tools, or "other tools" (or
    libraries, implementations...), together with "all", "both", "every" or
    "most". pypdf#4035 read "poppler, mutool and pdf.js all do X", and the
    requested change went the other way; the operator should see that before
    approving a patch.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        body = _CODE_FENCE.sub(" ", str(comment.get("body") or ""))
        for sentence in _SENTENCE_SPLIT.split(body):
            sentence = " ".join(sentence.split())
            if not sentence or not _QUANTIFIER.search(sentence):
                continue
            names = sorted(
                {match.group(0).lower() for match in _TOOL_NAME.finditer(sentence)}
            )
            if len(names) < 2 and not _OTHER_TOOLS.search(sentence):
                continue
            quote = sentence if len(sentence) <= 300 else sentence[:297] + "..."
            if quote in seen:
                continue
            seen.add(quote)
            user = comment.get("user") or {}
            found.append(
                {
                    "author": user.get("login") if isinstance(user, dict) else None,
                    "association": comment.get("author_association"),
                    "tools": names,
                    "quote": quote,
                }
            )
    return found


def _thread_tool_comparisons(
    gh: _Gh, run_directory: Path, slug: str
) -> list[dict[str, Any]] | None:
    """Read the run's own issue thread for tool comparisons; None when there is none."""
    issue = load_issue_record(run_directory) or {}
    reference = issue.get("reference") or {}
    number = reference.get("number")
    owner_name = f"{reference.get('owner')}/{reference.get('repository')}"
    if not number or owner_name.lower() != slug.lower():
        return None
    payload = gh.json(f"repos/{slug}/issues/{number}")
    if not isinstance(payload, dict):
        return None
    comments = gh.pages(f"repos/{slug}/issues/{number}/comments", pages=2)
    return tool_comparisons([payload, *comments])


def repository_slug(repository: str) -> str:
    """Reduce a clone URL or a slug to `owner/name`."""
    slug = repository.removesuffix(".git").rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        slug = slug.removeprefix(prefix)
    return slug


def _is_bot(user: dict[str, Any] | None) -> bool:
    """Decide whether an account is a bot, by type and by name.

    `author_association` does not carry this. Dependabot merges as a
    `CONTRIBUTOR`, which is what made `PyCQA/bandit` score a fresh outside merge
    while it had merged no human contribution in four months. Prow's
    `k8s-ci-robot` is a `User` and a `CONTRIBUTOR`, so its help-wanted
    boilerplate read as a maintainer dispute. Mailman #437.
    """
    if not isinstance(user, dict):
        return True
    if (user.get("type") or "") == "Bot":
        return True
    login = (user.get("login") or "").lower()
    return login.endswith(("[bot]", "-bot", "-robot")) or login.startswith("dependabot")


def is_outside_human(row: dict[str, Any]) -> bool:
    return (
        row.get("author_association") in OUTSIDE_ASSOCIATIONS
        and not _is_bot(row.get("user"))
    )


def referenced_issues(
    row: dict[str, Any], repository: str | None = None
) -> set[str]:
    """Every issue number of `repository` a pull request's text points at.

    `repository` defaults to the pull request's own base repository. A
    reference that names another repository is not counted; one that names
    none is. The whole body is read and `#7` counts (#346).
    """
    head = row.get("head") or {}
    if repository is None:
        base = row.get("base") or {}
        repo = base.get("repo") if isinstance(base, dict) else None
        repository = (repo or {}).get("full_name") if isinstance(repo, dict) else None
    own = (repository or "").lower()
    text = " ".join(
        [
            row.get("title") or "",
            row.get("body") or "",
            head.get("ref") or "" if isinstance(head, dict) else "",
        ]
    )
    found = {
        number
        for pattern in (_ISSUE_REFERENCE, _ISSUE_URL_REFERENCE)
        for named, number in pattern.findall(text)
        if not named or not own or named.lower() == own
    }
    return found | set(_BRANCH_REFERENCE.findall(text))


def classify_claims(pull_requests: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Split referenced issues by whether the referencing row is a live claim.

    An open or merged pull request claims its issue. A closed unmerged one does
    not: on a bot-policed target it is usually the record of someone being
    refused, which is the opposite of a claim. This is the same distinction
    https://github.com/wolfgang-aura/Mailman/issues/32 and
    https://github.com/wolfgang-aura/Mailman/issues/33 taught the duplicate gate.
    """
    claiming: set[str] = set()
    abandoned: set[str] = set()
    for row in pull_requests:
        target = (
            claiming
            if row.get("state") == "open" or row.get("merged_at")
            else abandoned
        )
        target.update(referenced_issues(row))
    return {"claiming": claiming, "abandoned": abandoned}


def _rule_markers(body: str) -> list[str]:
    """Keep the markers in one comment that could name a rule.

    A summary bot wraps each section of its comment in a `foo_start` /
    `foo_end` pair, and stamps each inline note with a hash. CodeRabbit leaves
    fourteen of those per pull request, which on securo-finance/securo made
    the rules section fourteen lines of layout and zero rules
    (https://github.com/wolfgang-aura/Mailman/issues/91).
    """
    markers = [marker.lower() for marker in _MARKER.findall(body)]
    present = set(markers)
    kept: list[str] = []
    for marker in markers:
        if _HASH_TAIL.search(marker):
            continue
        for end in _LAYOUT_ENDS:
            if marker.endswith(end):
                stem = marker[: -len(end)]
                other = stem + (_LAYOUT_ENDS[1] if end == _LAYOUT_ENDS[0] else _LAYOUT_ENDS[0])
                if other in present:
                    break
        else:
            kept.append(marker)
    return kept


def enforcement_markers(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name the automated rules a repository enforces, from the bot's own words."""
    found: dict[str, dict[str, Any]] = {}
    for comment in comments:
        if not _is_bot(comment.get("user")):
            continue
        body = comment.get("body") or ""
        for marker in _rule_markers(body):
            entry = found.setdefault(
                marker.lower(),
                {"marker": marker.lower(), "count": 0, "quote": "", "seen_on": []},
            )
            entry["count"] += 1
            if not entry["quote"]:
                entry["quote"] = " ".join(body.split())[:400]
            source = comment.get("_pull_request")
            if source and source not in entry["seen_on"]:
                entry["seen_on"].append(source)
    return sorted(found.values(), key=lambda entry: -entry["count"])


#: What a project's web server sees when the screen reads a policy page it
#: keeps outside GitHub. Some documentation hosts answer a bare urllib agent
#: with 403.
_PAGE_USER_AGENT = "mailman-screen (+https://github.com/wolfgang-aura/Mailman)"

#: A policy page is a few kilobytes of prose; a cap stops one link from
#: pulling a whole site export into a screen record.
_PAGE_BYTE_LIMIT = 2_000_000


def _page_result(
    url: str, exit_code: int, stdout: str, stderr: str, timeout_seconds: float, started: datetime
) -> CommandResult:
    return CommandResult(
        command=["fetch", url],
        working_directory="",
        started_at=started.isoformat(),
        duration_seconds=(datetime.now(UTC) - started).total_seconds(),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        timeout_seconds=timeout_seconds,
        environment={},
    )


def fetch_page(url: str, timeout_seconds: float) -> CommandResult:
    """Read one web page over https, reported the way a command is.

    The screen's policy gate follows a contributing guide to the document it
    links, and pretix keeps that document at docs.pretix.eu, where `gh api`
    cannot go. The result carries the page body as stdout so the same
    `commands` log records it beside the API calls.
    """
    started = datetime.now(UTC)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _PAGE_USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(_PAGE_BYTE_LIMIT)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        return _page_result(url, 1, "", f"HTTP {error.code} {error.reason}", timeout_seconds, started)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return _page_result(url, 1, "", str(error), timeout_seconds, started)
    return _page_result(url, 0, raw.decode(charset, errors="replace"), "", timeout_seconds, started)


#: What `gh api` prints when GitHub's secondary (burst) limit refuses a call.
#: `rate_limit` still reports the hourly budget as full when this happens.
_SECONDARY_LIMIT = re.compile(
    r"secondary rate limit|abuse detection|retry[- ]after", re.IGNORECASE
)
#: What `gh api` prints when the hourly core budget is spent. Waiting a
#: minute does not help, and `rate_limit` has reported the budget full while
#: every other call was refused (2026-09-29, Mailman #169).
_PRIMARY_LIMIT = re.compile(r"API rate limit exceeded", re.IGNORECASE)
SECONDARY_LIMIT_WAIT_SECONDS = 60
SECONDARY_LIMIT_RETRIES = 2


class _Gh:
    """One `gh api` caller that records every command it ran."""

    def __init__(
        self,
        executable: str,
        working_directory: Path,
        timeout_seconds: float,
        run: Callable[..., CommandResult] = execute,
        fetch: Callable[[str, float], CommandResult] = fetch_page,
    ) -> None:
        self.executable = executable
        self.working_directory = working_directory
        self.timeout_seconds = timeout_seconds
        self.run = run
        self.fetch = fetch
        self.commands: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.sleep: Callable[[float], None] = time.sleep
        #: True once any call was refused for the spent hourly budget.
        self.rate_limited = False

    def page(self, url: str) -> str | None:
        """The body of one web page, or None when it could not be read."""
        result = self.fetch(url, self.timeout_seconds)
        self.commands.append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            self.failures.append(url)
            return None
        return result.stdout

    def json(self, path: str) -> Any | None:
        return self._api([path], path)

    def graphql(self, query: str) -> Any | None:
        """One GraphQL read, on GitHub's GraphQL budget rather than REST core."""
        return self._api(["graphql", "-f", f"query={query}"], "graphql")

    def _api(self, arguments: list[str], label: str) -> Any | None:
        command = [self.executable, "api", *arguments]
        result: CommandResult = self.run(
            command,
            working_directory=self.working_directory,
            timeout_seconds=self.timeout_seconds,
        )
        self.commands.append(result.to_dict())
        for _ in range(SECONDARY_LIMIT_RETRIES):
            if result.exit_code == 0 or not _SECONDARY_LIMIT.search(
                str(getattr(result, "stderr", "") or "")
            ):
                break
            # GitHub's burst limit answers in seconds, not the hourly budget,
            # and failing fast turned 34 good screens into unread ones.
            # https://github.com/wolfgang-aura/Mailman/issues/117
            self.sleep(SECONDARY_LIMIT_WAIT_SECONDS)
            result = self.run(
                command,
                working_directory=self.working_directory,
                timeout_seconds=self.timeout_seconds,
            )
            self.commands.append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            if _PRIMARY_LIMIT.search(str(getattr(result, "stderr", "") or "")):
                self.rate_limited = True
            self.failures.append(label)
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.failures.append(label)
            return None

    def pages(self, path: str, *, pages: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        joiner = "&" if "?" in path else "?"
        for page in range(1, pages + 1):
            got = self.json(f"{path}{joiner}per_page=100&page={page}")
            if not isinstance(got, list):
                break
            rows += got
            if len(got) < 100:
                break
        return rows

    def every_page(self, path: str, *, pages: int) -> list[dict[str, Any]] | None:
        """Every row behind `path`, or None when it could not all be read.

        `pages` reads a failed page as the end of the list, which is right for
        a sample and wrong for evidence: a thread or timeline cut short reads
        as one with nothing more in it. A failed page, or a list still full at
        the cap, is None here (#339, #344).
        """
        rows: list[dict[str, Any]] = []
        joiner = "&" if "?" in path else "?"
        for page in range(1, pages + 1):
            got = self.json(f"{path}{joiner}per_page=100&page={page}")
            if not isinstance(got, list):
                return None
            rows += got
            if len(got) < 100:
                return rows
        return None


def _merge_path_rows(
    gh: _Gh, slug: str, merged: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """For each recent outside merge, read how its author got the work.

    This is the screen the operator asked to be non-negotiable: never judge a
    target by its policy text alone, read the pull requests that actually merged
    and the threads that preceded them. On `langchain-ai/langchain` it is what
    showed that every merged outside fix had its linked issue assigned to the
    pull request's author first, and that one of them was assigned to somebody
    other than the reporter, which the recorded policy denied.
    """
    rows: list[dict[str, Any]] = []
    for pull in merged[:limit]:
        number = pull.get("number")
        issues = sorted(referenced_issues(pull), key=int)
        author = ((pull.get("user") or {}).get("login")) or ""
        entry: dict[str, Any] = {
            "pull_request": number,
            "author": author,
            "merged_at": pull.get("merged_at"),
            "title": pull.get("title"),
            "linked_issues": [int(number_) for number_ in issues],
            "issues": [],
            "author_was_assigned": None,
            "assigned_to_reporter": None,
            "thread_before_the_pull_request": [],
        }
        for issue_number in issues[:2]:
            issue = gh.json(f"repos/{slug}/issues/{issue_number}")
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            assignees = [
                (assignee.get("login") or "")
                for assignee in issue.get("assignees") or []
            ]
            reporter = (issue.get("user") or {}).get("login") or ""
            entry["issues"].append(
                {
                    "number": issue.get("number"),
                    "reporter": reporter,
                    "assignees": assignees,
                    "created_at": issue.get("created_at"),
                    "title": issue.get("title"),
                }
            )
            entry["author_was_assigned"] = author in assignees
            entry["assigned_to_reporter"] = bool(assignees) and reporter in assignees
            comments = gh.json(f"repos/{slug}/issues/{issue_number}/comments")
            opened = pull.get("created_at") or ""
            if isinstance(comments, list):
                entry["thread_before_the_pull_request"] = [
                    {
                        "author": (comment.get("user") or {}).get("login"),
                        "association": comment.get("author_association"),
                        "created_at": comment.get("created_at"),
                        "won": ((comment.get("user") or {}).get("login")) == author,
                        "body": " ".join((comment.get("body") or "").split())[:900],
                    }
                    for comment in comments
                    if not _is_bot(comment.get("user"))
                    and (comment.get("created_at") or "") <= opened
                ]
        rows.append(entry)
    return rows


def collect_target_intel(
    run_directory: Path,
    *,
    repository: str,
    window_days: int = FRESHNESS_WINDOW_DAYS,
    merge_paths: int = 6,
    enforcement_samples: int = 8,
    executable: str | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Record how a target merges outside work, before a run is spent on it."""
    slug = repository_slug(repository)
    gh = _Gh(
        executable or resolve_tool(run_directory, "gh"), run_directory, timeout_seconds
    )
    since = (datetime.now(UTC) - timedelta(days=window_days)).date().isoformat()
    record: dict[str, Any] = {
        "schema_version": TARGET_INTEL_SCHEMA_VERSION,
        "collected_at": datetime.now(UTC).isoformat(),
        "repository": slug,
        "window_days": window_days,
        "since": since,
        "success": False,
    }

    meta = gh.json(f"repos/{slug}")
    if not isinstance(meta, dict):
        record["detail"] = f"{slug} could not be read"
        _write(run_directory, record)
        return record
    record["stars"] = meta.get("stargazers_count")
    record["default_branch"] = meta.get("default_branch")

    closed = gh.pages(f"repos/{slug}/pulls?state=closed&sort=updated&direction=desc", pages=8)
    opened = gh.pages(f"repos/{slug}/pulls?state=open&sort=updated&direction=desc", pages=8)
    if not closed and not opened:
        record["detail"] = "no pull requests could be read"
        _write(run_directory, record)
        return record

    merged_outside = sorted(
        [
            row
            for row in closed
            if row.get("merged_at") and is_outside_human(row)
        ],
        key=lambda row: row["merged_at"],
        reverse=True,
    )
    merged_recent = [row for row in merged_outside if row["merged_at"][:10] >= since]
    merged_bots = [
        row
        for row in closed
        if row.get("merged_at")
        and row["merged_at"][:10] >= since
        and _is_bot(row.get("user"))
    ]
    refused_recent = [
        row
        for row in closed
        if not row.get("merged_at")
        and is_outside_human(row)
        and (row.get("closed_at") or "")[:10] >= since
    ]
    record["freshness"] = {
        "human_outside_merges": len(merged_recent),
        "bot_merges_excluded": len(merged_bots),
        "outside_pull_requests_closed_unmerged": len(refused_recent),
        "latest_human_outside_merge": (
            merged_recent[0]["merged_at"] if merged_recent else None
        ),
        "pull_requests_scanned": len(closed) + len(opened),
    }

    claims = classify_claims(closed + opened)
    issues = gh.pages(
        f"repos/{slug}/issues?state=open&sort=created&direction=desc", pages=6
    )
    open_issues = [row for row in issues if "pull_request" not in row]
    unassigned = [row for row in open_issues if not row.get("assignee")]
    naive = [
        row
        for row in unassigned
        if str(row["number"]) not in (claims["claiming"] | claims["abandoned"])
    ]
    aware = [row for row in unassigned if str(row["number"]) not in claims["claiming"]]
    record["saturation"] = {
        "open_issues_read": len(open_issues),
        "unassigned": len(unassigned),
        "unclaimed_counting_any_pull_request": len(naive),
        "unclaimed_counting_only_open_or_merged": len(aware),
        "candidates": [
            {
                "number": row["number"],
                "created_at": row["created_at"],
                "comments": row.get("comments", 0),
                "labels": [label["name"] for label in row.get("labels") or []],
                "title": row.get("title"),
                "refused_attempts_exist": str(row["number"]) in claims["abandoned"],
            }
            for row in aware[:40]
        ],
    }

    bot_comments: list[dict[str, Any]] = []
    for pull in refused_recent[:enforcement_samples]:
        comments = gh.json(f"repos/{slug}/issues/{pull['number']}/comments")
        if isinstance(comments, list):
            for comment in comments:
                comment["_pull_request"] = pull["number"]
            bot_comments += comments
    record["enforcement"] = enforcement_markers(bot_comments)

    record["merge_path"] = _merge_path_rows(gh, slug, merged_recent, merge_paths)
    judged = [
        row for row in record["merge_path"] if row["author_was_assigned"] is not None
    ]
    assigned = [row for row in judged if row["author_was_assigned"]]
    record["assessment"] = {
        "merge_path_rows_read": len(judged),
        # Stated as a ratio as well as a boolean. Against langchain the strict
        # boolean is False only because a docs pull request merged unassigned
        # while every code fix was assigned, and a reader who sees the ratio
        # asks the right follow-up question where a bare False ends the thought.
        "merges_whose_author_held_the_assignment": len(assigned),
        "assignment_looks_required": bool(judged) and len(assigned) == len(judged),
        "assignment_seen_on_some_merges": bool(assigned),
        "assignment_reaches_non_reporters": any(
            row["author_was_assigned"] and row["assigned_to_reporter"] is False
            for row in judged
        ),
        "automated_enforcement": [entry["marker"] for entry in record["enforcement"]],
        "passes_freshness_bar": len(merged_recent) > 0,
    }
    # Other tools the issue thread names as behaving one way. Not a gate: it
    # seeds a question in `decision --init`. Mailman #124.
    record["tool_comparisons"] = _thread_tool_comparisons(gh, run_directory, slug)
    record["commands"] = gh.commands
    record["read_failures"] = gh.failures
    record["success"] = True
    _write(run_directory, record)
    (run_directory / TARGET_INTEL_MARKDOWN).write_text(
        render_target_intel(record), encoding="utf-8", newline="\n"
    )
    return record


def render_target_intel(record: dict[str, Any]) -> str:
    """Put the merge path in front of a human in the order it should be read."""
    lines = [f"# How {record.get('repository')} hands out work", ""]
    if not record.get("success"):
        lines += [f"The read failed: {record.get('detail', 'unknown')}", ""]
        return "\n".join(lines)
    freshness = record.get("freshness", {})
    saturation = record.get("saturation", {})
    assessment = record.get("assessment", {})
    lines += [
        f"- Stars: {record.get('stars')}",
        f"- Human outside merges in {record.get('window_days')} days: "
        f"{freshness.get('human_outside_merges')} "
        f"(bot merges excluded: {freshness.get('bot_merges_excluded')})",
        f"- Outside pull requests closed unmerged in the same window: "
        f"{freshness.get('outside_pull_requests_closed_unmerged')}",
        f"- Of {saturation.get('unassigned')} unassigned open issues, "
        f"{saturation.get('unclaimed_counting_only_open_or_merged')} have no open "
        f"or merged pull request against them. Counting any referencing pull "
        f"request as a claim, including ones closed unread, would say "
        f"{saturation.get('unclaimed_counting_any_pull_request')}. No label or "
        f"age filter is applied here.",
        "",
    ]
    if record.get("enforcement"):
        lines += ["## Automated rules the repository enforces", ""]
        for entry in record["enforcement"]:
            seen = ", ".join(f"#{number}" for number in entry.get("seen_on", [])[:4])
            lines.append(f"- `{entry['marker']}`, seen {entry['count']} time(s) on {seen}")
            lines.append(f"  > {entry['quote'][:300]}")
        lines.append("")
    if record.get("tool_comparisons"):
        lines += ["## Other tools the issue thread compares against", ""]
        for entry in record["tool_comparisons"]:
            lines.append(f"- **{entry.get('author')}**: {entry.get('quote')}")
        lines.append("")
    lines += ["## What the merges that landed actually did", ""]
    held = assessment.get("merges_whose_author_held_the_assignment", 0)
    read = assessment.get("merge_path_rows_read", 0)
    if read:
        lines.append(
            f"{held} of the {read} outside merges traced here had the linked issue "
            "assigned to the pull request's author."
        )
    if assessment.get("assignment_looks_required"):
        lines.append(
            "That is all of them, so treat assignment as a precondition rather "
            "than a formality: a pull request opened before it is closed unread."
        )
    elif assessment.get("assignment_seen_on_some_merges"):
        lines.append(
            "That is some of them, not all. Read the rows below before deciding "
            "whether the gate applies to the kind of change you intend; on "
            "langchain-ai/langchain it binds code fixes while documentation and "
            "chore pull requests merge without it."
        )
    if assessment.get("assignment_reaches_non_reporters"):
        lines.append(
            "At least one of them was assigned to somebody other than the "
            "issue's reporter, so asking on another person's report can work."
        )
    if read:
        lines.append("")
    for row in record.get("merge_path", []):
        lines.append(
            f"### #{row['pull_request']} by {row['author']} — {row.get('title', '')}"
        )
        for issue in row.get("issues", []):
            lines.append(
                f"- Linked issue #{issue['number']}, reported by "
                f"{issue['reporter']}, assigned to "
                f"{', '.join(issue['assignees']) or 'nobody'}"
            )
        thread = row.get("thread_before_the_pull_request") or []
        if thread:
            lines += ["", "The thread before the pull request opened:", ""]
            for comment in thread:
                mark = "WON" if comment.get("won") else "   "
                lines.append(
                    f"- `{mark}` **{comment['author']}** "
                    f"({comment['association']}): {comment['body'][:400]}"
                )
        lines.append("")
    return "\n".join(lines)


def _write(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / TARGET_INTEL_FILENAME
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_target_intel(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / TARGET_INTEL_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
