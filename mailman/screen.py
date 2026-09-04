"""Screen a repository before any run is spent on it.

`check-target` works one level down: it judges an *issue* against searches a run
already recorded. Nothing judged the *repository*, so the same GitHub queries got
retyped for every candidate and the answer was never written down. Screening
ninety candidates by hand was most of one session. See
https://github.com/wolfgang-aura/Mailman/issues/35.

The gates run in the order a candidate actually dies in. Freshness kills most of
them, and it costs two API calls, so it runs first. Provenance is reported ahead
of it because it answers a different question, whether this repository's code
should execute on the host at all, but it is computed from what freshness
already fetched. Stars run last because they
have never once changed a decision: `OpenBB-finance/OpenBB` has 72.6k of them and
has merged nothing from outside in six weeks.

Every gate reports its numbers next to the threshold that judged them. A screen
that prints `fail` without the count it counted is a screen nobody trusts twice.
"""

from __future__ import annotations

import base64
import json
import re
import statistics
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from mailman.executor import CommandResult, execute
from mailman.target_intel import (
    _Gh,
    _is_bot,
    classify_claims,
    is_outside_human,
    repository_slug,
)
from mailman.toolchain import resolve_tool

SCREEN_SCHEMA_VERSION = 1
SCREENS_DIRECTORY = "screens"

#: How far back to look for the pattern of outside merges, as opposed to the
#: freshness window. One recent merge means nothing if the same person wrote
#: every outside merge for three years, which is `freqtrade/freqtrade`.
PATTERN_DAYS = 90

#: Provenance thresholds. These do not ask whether a target is worth a run, which
#: is what every other gate asks. They ask whether its code has been read by
#: enough people that installing it and running its test suite on the host is
#: reasonable. `prepare-environment` executes the target's build back end and
#: verification imports its `conftest.py`, both as the invoking user, so the
#: decision to point pip at a stranger is made here or nowhere. See
#: https://github.com/wolfgang-aura/Mailman/issues/48.
#:
#: Stars decide nothing about quality, which is why gate 6 only reports them.
#: They mean something different here: a repository with a thousand of them,
#: standing for a year, has been read by people who were not us.
PROVENANCE_MINIMUM_AGE_DAYS = 365
PROVENANCE_MINIMUM_STARS = 500

#: The other way through. A young project with a broad contributor base has also
#: been read widely, and `pydantic/pydantic-ai` is that shape. `pmorissette/ffn`
#: passes the age route with 11 authors and would fail this one, which is why
#: either route is enough on its own.
PROVENANCE_MINIMUM_AUTHORS = 10


#: A repository whose entire outside contribution is one person is closed in
#: practice however busy it looks.
MINIMUM_OUTSIDE_AUTHORS = 2

#: One collaborator plus a trickle is still one collaborator. `freqtrade` sits
#: at 0.44 with fourteen authors and is genuinely open; the bar is for the shape
#: where a second name appears once and the same person writes everything else.
DOMINANT_AUTHOR_SHARE = 0.8

#: When every merge inside the freshness window is by one person, that person's
#: share of the longer window decides whether the window is evidence of an open
#: door or of one recurring collaborator. `freqtrade/freqtrade` on 2026-09-04
#: passed on three merges inside fourteen days, all by `stash86`, who wrote 48%
#: of the outside merges in ninety days. See
#: https://github.com/wolfgang-aura/Mailman/issues/42.
WINDOW_SINGLE_AUTHOR_SHARE = 0.35

#: A share computed over two merges is arithmetic, not evidence. Below this many
#: outside merges in the pattern window, the single-author rule does not apply.
SHARE_SAMPLE_MINIMUM = 8

#: Python has to be the language the repository is actually written in. On
#: `ccxt/ccxt` the Python is generated from TypeScript, and a patch to it is
#: thrown away by the next build.
MINIMUM_PYTHON_SHARE = 0.5

#: Names that mean a workflow step ran a test suite, rather than publishing a
#: wheel or running a linter. Deliberately wide: a false negative here rejects a
#: good candidate, which is the expensive mistake. `ccxt/ccxt` runs its Python
#: suite as `npm run test-base-rest-py`, and the first version of this pattern
#: read that repository as having no tests at all.
_TEST_RUNNER = re.compile(
    r"(?:"
    r"\b(?:pytest|tox|nox|unittest|py\.test|phpunit|trial)\b"
    r"|\bcoverage\s+run\b"
    r"|\b(?:make|just)\s+(?:check|test)\b"
    r"|\bhatch\s+run\s+test"
    r"|\b(?:npm|yarn|pnpm)\s+run\s+[\w:.-]*test[\w:.-]*"
    r"|\b(?:npm|yarn|pnpm)\s+test\b"
    r"|\b(?:cargo|go|dotnet|mvn)\s+test\b"
    r"|\brun[-_]tests?\b"
    r"|^\s*(?:-\s*)?name:[^\n]*\btests?\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

#: Files whose presence means a compiler is in the build. The operator has no
#: Rust or MSVC toolchain, so these are fatal rather than inconvenient.
_COMPILED_MARKERS = ("Cargo.toml", "setup.py", "Makefile", "meson.build")
_COMPILED_LANGUAGES = ("Cython", "Rust", "C", "C++", "Go", "Zig")

#: Build requirements that mean a compiler runs at install time even when every
#: file on disk is a `.py`. `pmorissette/bt` is 100% Python by GitHub's count
#: and compiles `bt/core.py` with Cython through a hatch build hook, so it
#: passed the extension check and then failed `prepare-environment`. See
#: https://github.com/wolfgang-aura/Mailman/issues/44.
_BUILD_COMPILERS = (
    "cython",
    "cffi",
    "pybind11",
    "setuptools-rust",
    "setuptools_rust",
    "maturin",
    "scikit-build",
    "scikit_build",
    "meson-python",
    "nanobind",
)

#: The Cython build hook `bt` configures, as a table header. Matched on the
#: header alone: `hatch-cython` also appears in `[build-system].requires`, and a
#: project that lists it there without configuring the hook is not the same
#: case. A wheel-only compiler leaves the source tree importable, so the target
#: stays usable; a build back end that compiles the package does not.
_WHEEL_ONLY_HOOK_HEADER = re.compile(
    r"^\[[^\]]*\bhooks\.cython\b[^\]]*\]", re.IGNORECASE
)

#: A policy that closes an AI-assisted pull request unread. Phrased as several
#: narrow patterns rather than one loose one: "AI" alone matches every machine
#: learning library's contributing guide.
_POLICY_BANS = re.compile(
    r"(?:"
    r"no\s+ai[- ]generated"
    r"|ai[- ]generated\s+(?:code|pull requests?|prs?|contributions?)\s+"
    r"(?:are|will be)\s+(?:not\s+accepted|rejected|closed|banned)"
    r"|(?:do not|don't|please do not)\s+(?:use|submit)\s+(?:ai|llm|chatgpt|copilot)"
    r"|we\s+(?:do not|don't)\s+accept\s+ai"
    r"|ai[- ]?(?:assisted|written)\s+contributions?\s+are\s+not"
    r"|zero[- ]tolerance\s+.{0,40}\bai\b"
    r")",
    re.IGNORECASE,
)

#: A policy that allows the work but requires it to be declared.
_POLICY_DISCLOSURE = re.compile(
    r"(?:"
    r"disclose\s+.{0,40}\b(?:ai|llm|assistant)"
    r"|\b(?:ai|llm)\b.{0,40}must\s+be\s+disclosed"
    r"|declare\s+.{0,30}\bai\b"
    r"|state\s+.{0,30}\bai[- ]assisted"
    r")",
    re.IGNORECASE,
)

#: A policy that permits the code and constrains the prose. Our pull request
#: bodies are model-written, so on these projects the body is itself the
#: violation however good the patch is. `freqtrade/freqtrade` enforces this:
#: on issue #13479 the maintainer quoted the rule at a reporter, who apologised.
#: See https://github.com/wolfgang-aura/Mailman/issues/43.
_POLICY_OWN_WORDS = re.compile(
    r"(?:"
    r"in\s+your\s+own\s+words"
    r"|never\s+let\s+an?\s+(?:llm|ai)\s+(?:speak|think)\s+for\s+you"
    r"|written\s+by\s+you,?\s+not"
    r"|(?:comments|issues|descriptions?)\s+.{0,60}your\s+own\s+words"
    r"|do\s+not\s+.{0,30}(?:llm|ai)[- ]generated\s+(?:text|prose|descriptions?)"
    r")",
    re.IGNORECASE,
)

#: A policy that wants the commit tied to a person, not to a tool's account.
_POLICY_HUMAN_ACCOUNT = re.compile(
    r"(?:"
    r"commits?\s+.{0,60}(?:human|personal|your own)\s+account"
    r"|(?:not|never)\s+.{0,40}generic\s+ai\s+account"
    r"|author\s+.{0,40}must\s+be\s+a\s+(?:human|person)"
    r")",
    re.IGNORECASE,
)

_POLICY_PATHS = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "AGENTS.md",
)


def _gate(
    name: str,
    *,
    passed: bool,
    blocking: bool,
    detail: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "blocking": blocking,
        "detail": detail,
        "data": data or {},
    }


def _decoded(payload: Any) -> str:
    """Read a GitHub contents payload, whatever encoding it arrived in."""
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    if payload.get("encoding") == "base64":
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""
    return content


def _provenance_gate(meta: dict[str, Any], freshness: dict[str, Any]) -> dict[str, Any]:
    """Gate 0. Has anyone but us read this code before we execute it?

    Runs after freshness because it reuses the author count freshness already
    paid for, and is reported first because it is the gate that decides whether
    the host runs a stranger's `setup.py`.
    """
    created = str(meta.get("created_at") or "")[:10]
    age_days: int | None = None
    if created:
        try:
            age_days = (datetime.now(UTC).date() - date.fromisoformat(created)).days
        except ValueError:
            age_days = None
    stars = meta.get("stargazers_count")
    authors = freshness.get("data", {}).get("distinct_outside_authors", 0)
    forked = bool(meta.get("fork"))
    data = {
        "created_at": created or None,
        "age_days": age_days,
        "stars": stars,
        "distinct_outside_authors": authors,
        "fork": forked,
        "minimum_age_days": PROVENANCE_MINIMUM_AGE_DAYS,
        "minimum_stars": PROVENANCE_MINIMUM_STARS,
        "minimum_authors": PROVENANCE_MINIMUM_AUTHORS,
    }
    if forked:
        return _gate(
            "provenance",
            passed=False,
            blocking=True,
            detail=(
                "this is a fork, so its code is one push away from whoever owns "
                "the fork and carries none of the upstream's history"
            ),
            data=data,
        )
    established = (
        age_days is not None
        and age_days >= PROVENANCE_MINIMUM_AGE_DAYS
        and isinstance(stars, int)
        and stars >= PROVENANCE_MINIMUM_STARS
    )
    broad = authors >= PROVENANCE_MINIMUM_AUTHORS
    if established or broad:
        reason = (
            f"{authors} outside author(s) in {PATTERN_DAYS} days"
            if broad
            else f"{stars} star(s) over {age_days} day(s)"
        )
        return _gate(
            "provenance",
            passed=True,
            blocking=True,
            detail=(
                f"{reason}; safe enough to install and run its suite on this host"
            ),
            data=data,
        )
    return _gate(
        "provenance",
        passed=False,
        blocking=True,
        detail=(
            f"{stars} star(s), {age_days} day(s) old, {authors} outside author(s) "
            f"in {PATTERN_DAYS} days. Too few people have read this code to run "
            "its build back end and its test suite on this machine."
        ),
        data=data,
    )


def _freshness_gate(gh: _Gh, slug: str, window_days: int) -> dict[str, Any]:
    """Gate 1. Does outside work actually merge here, and by more than one person?"""
    closed = gh.pages(
        f"repos/{slug}/pulls?state=closed&sort=updated&direction=desc", pages=6
    )
    now = datetime.now(UTC)
    window = (now - timedelta(days=window_days)).date().isoformat()
    pattern = (now - timedelta(days=PATTERN_DAYS)).date().isoformat()

    merged_outside = [
        row
        for row in closed
        if row.get("merged_at") and is_outside_human(row)
    ]
    recent = [row for row in merged_outside if row["merged_at"][:10] >= window]
    longer = [row for row in merged_outside if row["merged_at"][:10] >= pattern]
    authors = Counter(
        (row.get("user") or {}).get("login")
        for row in longer
        if isinstance(row.get("user"), dict)
    )
    distinct = len(authors)
    window_authors = {
        (row.get("user") or {}).get("login")
        for row in recent
        if isinstance(row.get("user"), dict)
    }
    # Named, not just counted. A pass resting on three merges is a different
    # repository depending on whether one account or three wrote them, and the
    # reader cannot tell from a number.
    excluded_bots = sorted(
        {
            (row.get("user") or {}).get("login")
            for row in closed
            if row.get("merged_at")
            and isinstance(row.get("user"), dict)
            and _is_bot(row.get("user"))
            and (row.get("user") or {}).get("login")
        }
    )
    share = round(authors.most_common(1)[0][1] / len(longer), 2) if longer else None
    latest = max((row["merged_at"] for row in merged_outside), default=None)
    data = {
        "merges_in_window": len(recent),
        "merges_in_pattern_window": len(longer),
        "distinct_outside_authors": distinct,
        # Reported even when the gate passes on it. Three merges inside the
        # window written by one person is a different repository from three
        # written by three, and only the operator can weigh that.
        "distinct_authors_in_window": len(window_authors),
        "authors_in_window": sorted(name for name in window_authors if name),
        "excluded_bot_authors": excluded_bots,
        "top_author": authors.most_common(1)[0][0] if authors else None,
        "top_author_share": share,
        "latest_outside_merge": latest,
        "pull_requests_scanned": len(closed),
        "window_days": window_days,
        "pattern_days": PATTERN_DAYS,
    }
    if not recent:
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"no outside human merge in {window_days} days; latest is "
                f"{latest or 'none found'}"
            ),
            data=data,
        )
    if distinct < MINIMUM_OUTSIDE_AUTHORS:
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"{len(recent)} merge(s) in {window_days} days, but every outside "
                f"merge in {PATTERN_DAYS} days is by {data['top_author']}. One "
                "recurring collaborator is not an open door."
            ),
            data=data,
        )
    if share is not None and share >= DOMINANT_AUTHOR_SHARE:
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"{data['top_author']} wrote {share:.0%} of the {len(longer)} "
                f"outside merge(s) in {PATTERN_DAYS} days. The other "
                f"{distinct - 1} author(s) are a trickle around one collaborator."
            ),
            data=data,
        )
    # The whole freshness case can rest on one author while the ninety-day
    # spread looks broad. That author is a recurring collaborator, and their
    # merges say nothing about whether a stranger's pull request lands.
    sole = sorted(name for name in window_authors if name)
    if (
        len(sole) == 1
        and len(longer) >= SHARE_SAMPLE_MINIMUM
        and authors.get(sole[0], 0) / len(longer) >= WINDOW_SINGLE_AUTHOR_SHARE
    ):
        sole_share = authors[sole[0]] / len(longer)
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"every one of the {len(recent)} merge(s) in {window_days} days "
                f"is by {sole[0]}, who wrote {sole_share:.0%} of the {len(longer)} "
                f"outside merge(s) in {PATTERN_DAYS} days. That is a recurring "
                "collaborator, not evidence a stranger's pull request lands."
            ),
            data=data,
        )
    named = ", ".join(sole) if sole else "no named author"
    return _gate(
        "freshness",
        passed=True,
        blocking=True,
        detail=(
            f"{len(recent)} outside human merge(s) in {window_days} days by "
            f"{len(window_authors)} author(s) ({named}), {distinct} distinct "
            f"author(s) in {PATTERN_DAYS} days, top author {share:.0%}"
            + (f"; excluded {', '.join(excluded_bots)}" if excluded_bots else "")
        ),
        data=data,
    )


def _ci_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Gate 2. Is there a workflow that runs tests, not only publish and lint?"""
    listing = gh.json(f"repos/{slug}/contents/.github/workflows")
    if not isinstance(listing, list):
        return _gate(
            "ci",
            passed=False,
            blocking=True,
            detail="no .github/workflows directory could be read",
            data={"workflows_read": 0},
        )
    names = [
        entry.get("name")
        for entry in listing
        if isinstance(entry, dict) and str(entry.get("name", "")).endswith((".yml", ".yaml"))
    ]
    running: list[str] = []
    for name in names:
        body = _decoded(gh.json(f"repos/{slug}/contents/.github/workflows/{name}"))
        if _TEST_RUNNER.search(body):
            running.append(name)
    data = {
        "workflows_read": len(names),
        "workflows_running_tests": running,
    }
    if not running:
        return _gate(
            "ci",
            passed=False,
            blocking=True,
            detail=(
                f"{len(names)} workflow(s) and none of them runs a test suite. "
                "A patch here is verified by nobody but us."
            ),
            data=data,
        )
    shown = ", ".join(running[:3])
    if len(running) > 3:
        shown += f" and {len(running) - 3} more"
    return _gate(
        "ci",
        passed=True,
        blocking=True,
        detail=f"tests run in {shown}",
        data=data,
    )


def _build_requires(pyproject: str) -> tuple[list[str], str | None]:
    """Compiler names in `[build-system].requires`, with the line that said so.

    Parsed by reading the table rather than the whole file, because `Cython`
    appears in the dependencies of plenty of projects that never compile.
    """
    section = re.search(
        r"^\[build-system\]\s*$(.*?)(?=^\[|\Z)",
        pyproject,
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        return [], None
    requires = re.search(
        r"requires\s*=\s*\[(.*?)\]", section.group(1), re.DOTALL | re.IGNORECASE
    )
    if not requires:
        return [], None
    value = requires.group(1)
    lowered = value.lower()
    found = sorted({name for name in _BUILD_COMPILERS if name in lowered})
    evidence = "requires = [" + " ".join(value.split()) + "]"
    return found, evidence


def _wheel_only_hook(pyproject: str) -> str | None:
    """The configured build hook that compiles only the wheel, if there is one."""
    for line in pyproject.split("\n"):
        stripped = line.strip()
        if _WHEEL_ONLY_HOOK_HEADER.match(stripped):
            return stripped
    return None


def _python_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Gate 3. Is this Python we can build, and Python that is not generated?"""
    languages = gh.json(f"repos/{slug}/languages")
    languages = languages if isinstance(languages, dict) else {}
    total = sum(value for value in languages.values() if isinstance(value, (int, float)))
    python_share = (languages.get("Python", 0) / total) if total else 0.0
    compiled = {
        name: languages[name] for name in _COMPILED_LANGUAGES if name in languages
    }
    root = gh.json(f"repos/{slug}/contents")
    root_names = {
        entry.get("name")
        for entry in root
        if isinstance(entry, dict)
    } if isinstance(root, list) else set()
    markers = sorted(root_names & set(_COMPILED_MARKERS))
    pyproject = (
        _decoded(gh.json(f"repos/{slug}/contents/pyproject.toml"))
        if "pyproject.toml" in root_names
        else ""
    )
    build_compilers, requires_line = _build_requires(pyproject)
    wheel_hook = _wheel_only_hook(pyproject)
    data = {
        "python_share": round(python_share, 3),
        "compiled_languages": compiled,
        "root_markers": markers,
        "languages": languages,
        "build_requires_compilers": build_compilers,
        "build_requires_line": requires_line,
        "wheel_only_hook": wheel_hook,
        "environment_plan": None,
    }
    if total and python_share < MINIMUM_PYTHON_SHARE:
        dominant = max(languages, key=languages.get)
        return _gate(
            "pure-python",
            passed=False,
            blocking=True,
            detail=(
                f"Python is {python_share:.0%} of the source and {dominant} is the "
                "majority. The Python here may be generated from it."
            ),
            data=data,
        )
    if "Cargo.toml" in markers or "Cython" in compiled or "Rust" in compiled:
        return _gate(
            "pure-python",
            passed=False,
            blocking=True,
            detail=(
                "a compiler is in the build ("
                + ", ".join(markers + sorted(compiled))
                + ") and this host has no Rust or MSVC toolchain"
            ),
            data=data,
        )
    # Every file is a `.py` and a compiler still runs at install time. When the
    # hook only builds the wheel, the source tree itself imports, so the target
    # stays usable under a plan that never installs the package.
    if build_compilers or wheel_hook:
        data["environment_plan"] = "source-tree" if wheel_hook else None
        evidence = wheel_hook or requires_line or ", ".join(build_compilers)
        if wheel_hook:
            return _gate(
                "pure-python",
                passed=True,
                blocking=True,
                detail=(
                    f"Python is {python_share:.0%} of the source, but the wheel "
                    f"compiles ({evidence}). This host has no MSVC, so use the "
                    "`source-tree` environment plan: install the dependencies, "
                    "put the workspace on the path, never install the package."
                ),
                data=data,
            )
        return _gate(
            "pure-python",
            passed=False,
            blocking=True,
            detail=(
                f"Python is {python_share:.0%} of the source, but a compiler is "
                f"in the build back end ({evidence}) and this host has no Rust "
                "or MSVC toolchain"
            ),
            data=data,
        )
    return _gate(
        "pure-python",
        passed=True,
        blocking=True,
        detail=f"Python is {python_share:.0%} of the source, no compiler markers",
        data=data,
    )


def _policy_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Gate 4. Does the contributing guide close an AI-assisted pull request?"""
    for relative in _POLICY_PATHS:
        body = _decoded(gh.json(f"repos/{slug}/contents/{relative}"))
        if not body:
            continue
        flat = " ".join(body.split())
        ban = _POLICY_BANS.search(flat)
        if ban:
            return _gate(
                "policy",
                passed=False,
                blocking=True,
                detail=f"{relative} refuses AI-assisted work: {ban.group(0)!r}",
                data={"source": relative, "quote": ban.group(0)},
            )
        # Three separate questions, not one. A project can permit the code and
        # still refuse a model-written body or a commit under a tool's account,
        # and those constraints have to reach the run rather than be summarised
        # away into the word "pass".
        disclosure = _POLICY_DISCLOSURE.search(flat)
        own_words = _POLICY_OWN_WORDS.search(flat)
        human_account = _POLICY_HUMAN_ACCOUNT.search(flat)
        constraints = [
            {"kind": kind, "quote": match.group(0)}
            for kind, match in (
                ("disclosure", disclosure),
                ("own-words", own_words),
                ("human-account", human_account),
            )
            if match
        ]
        if constraints:
            summary = "; ".join(
                f"{entry['kind']}: {entry['quote']!r}" for entry in constraints
            )
            detail = f"{relative} permits the code and constrains the submission - {summary}"
            if own_words:
                detail += (
                    ". Set `requires_own_words` in the target policy so "
                    "prepare-submission refuses a generated body."
                )
        else:
            detail = f"{relative} says nothing that closes AI-assisted work"
        return _gate(
            "policy",
            passed=True,
            blocking=True,
            detail=detail,
            data={
                "source": relative,
                "requires_disclosure": bool(disclosure),
                "requires_own_words": bool(own_words),
                "requires_human_account": bool(human_account),
                "constraints": constraints,
                "quote": disclosure.group(0) if disclosure else None,
            },
        )
    return _gate(
        "policy",
        passed=True,
        blocking=False,
        detail="no contributing guide found, so nothing forbids the work in writing",
        data={"source": None},
    )


def _saturation_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Gate 5. Is there any unclaimed work left, or has the tracker been mined?"""
    issues = gh.pages(
        f"repos/{slug}/issues?state=open&sort=created&direction=desc", pages=4
    )
    open_issues = [row for row in issues if "pull_request" not in row]
    open_pulls = [row for row in issues if "pull_request" in row]
    unassigned = [row for row in open_issues if not row.get("assignee")]
    claims = classify_claims(
        gh.pages(f"repos/{slug}/pulls?state=open&sort=updated&direction=desc", pages=4)
    )
    unclaimed = [
        row for row in unassigned if str(row["number"]) not in claims["claiming"]
    ]
    now = datetime.now(UTC)
    ages = []
    for row in unclaimed:
        created = str(row.get("created_at") or "")[:10]
        if created:
            try:
                ages.append(
                    (now - datetime.fromisoformat(created).replace(tzinfo=UTC)).days
                )
            except ValueError:
                continue
    data = {
        "open_issues": len(open_issues),
        "open_pull_requests_seen": len(open_pulls),
        "unassigned": len(unassigned),
        "unclaimed": len(unclaimed),
        "claimed_share": (
            round(1 - len(unclaimed) / len(unassigned), 2) if unassigned else None
        ),
        "median_unclaimed_age_days": (
            round(statistics.median(ages)) if ages else None
        ),
    }
    if not unclaimed:
        return _gate(
            "saturation",
            passed=False,
            blocking=True,
            detail=(
                f"{len(unassigned)} unassigned open issue(s) and an open pull "
                "request already names every one of them"
            ),
            data=data,
        )
    return _gate(
        "saturation",
        passed=True,
        blocking=False,
        detail=(
            f"{len(unclaimed)} of {len(unassigned)} unassigned issue(s) have no "
            f"open pull request, median age "
            f"{data['median_unclaimed_age_days']} day(s)"
        ),
        data=data,
    )


def _stars_gate(meta: dict[str, Any]) -> dict[str, Any]:
    """Gate 6. Reported, never decisive. It runs last because it decides nothing."""
    stars = meta.get("stargazers_count")
    return _gate(
        "stars",
        passed=True,
        blocking=False,
        detail=f"{stars} star(s)",
        data={"stars": stars, "default_branch": meta.get("default_branch")},
    )


def screen_path(data_root: Path, slug: str) -> Path:
    owner, _, name = slug.partition("/")
    return data_root / SCREENS_DIRECTORY / f"{owner}__{name}.json"


def load_screen(data_root: Path, slug: str) -> dict[str, Any] | None:
    path = screen_path(data_root, slug)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _write(data_root: Path, record: dict[str, Any]) -> Path:
    destination = screen_path(data_root, record["repository"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def screen_repository(
    repository: str,
    *,
    data_root: Path,
    window_days: int = 14,
    executable: str | None = None,
    timeout_seconds: float = 120,
    working_directory: Path | None = None,
    _execute: Callable[..., CommandResult] = execute,
) -> dict[str, Any]:
    """Run every repository-level gate and write the verdict where it is reusable."""
    slug = repository_slug(repository)
    data_root.mkdir(parents=True, exist_ok=True)
    home = working_directory or data_root
    gh = _Gh(
        executable or resolve_tool(home, "gh"), home, timeout_seconds, _execute
    )
    record: dict[str, Any] = {
        "schema_version": SCREEN_SCHEMA_VERSION,
        "repository": slug,
        "screened_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "gates": [],
        "success": False,
    }

    meta = gh.json(f"repos/{slug}")
    if not isinstance(meta, dict) or "full_name" not in meta:
        record["detail"] = f"{slug} could not be read"
        _write(data_root, record)
        return record
    record["archived"] = bool(meta.get("archived"))
    if record["archived"]:
        record["gates"] = [
            _gate(
                "archived",
                passed=False,
                blocking=True,
                detail="the repository is archived and accepts nothing",
            )
        ]
        record["verdict"] = "fail"
        record["failed_gates"] = ["archived"]
        record["success"] = True
        _write(data_root, record)
        return record

    freshness = _freshness_gate(gh, slug, window_days)
    gates = [
        _provenance_gate(meta, freshness),
        freshness,
        _ci_gate(gh, slug),
        _python_gate(gh, slug),
        _policy_gate(gh, slug),
        _saturation_gate(gh, slug),
        _stars_gate(meta),
    ]
    failed = [
        gate["name"] for gate in gates if gate["blocking"] and not gate["passed"]
    ]
    record["gates"] = gates
    record["failed_gates"] = failed
    record["verdict"] = "fail" if failed else "pass"
    record["commands"] = gh.commands
    record["read_failures"] = gh.failures
    record["success"] = True
    _write(data_root, record)
    return record


def render_screen(record: dict[str, Any]) -> str:
    """One line per gate, with the numbers that decided it."""
    slug = record.get("repository")
    if not record.get("success"):
        return f"{slug}: unread, {record.get('detail', 'unknown failure')}"
    lines = [f"screen {slug}"]
    for gate in record.get("gates", []):
        mark = "pass" if gate["passed"] else ("FAIL" if gate["blocking"] else "warn")
        lines.append(f"  {mark:<5} {gate['name']:<13} {gate['detail']}")
    verdict = record.get("verdict")
    if verdict == "pass":
        lines.append("  verdict: worth a run")
    else:
        lines.append("  verdict: rejected on " + ", ".join(record["failed_gates"]))
    return "\n".join(lines)
