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
already fetched. Responsiveness runs late because it is the dearest gate, three
calls per outside pull request, and it asks the question freshness cannot: not
whether outside work merges here, but how long a stranger waits for a first
word. Stars run last because they
have never once changed a decision: `OpenBB-finance/OpenBB` has 72.6k of them and
has merged nothing from outside in six weeks.

Every gate reports its numbers next to the threshold that judged them. A screen
that prints `fail` without the count it counted is a screen nobody trusts twice.
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
import statistics
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mailman.claims import (
    MAINTAINER_ASSOCIATIONS,
    classify_comment,
    classify_thread,
    maintainer_touched_at,
    pull_request_references,
)
from mailman.executor import CommandResult, execute
from mailman.target_intel import (
    _Gh,
    _is_bot,
    classify_claims,
    enforcement_markers,
    is_outside_human,
    repository_slug,
)
from mailman.shortlist import (
    MAINTAINER_INVITED,
    rank_issue,
    render_shortlist,
    sort_shortlist,
)
from mailman.toolchain import resolve_tool

SCREEN_SCHEMA_VERSION = 3
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

#: How old an unclaimed issue may be and still count as work. This is not the
#: freshness window and must not be set from it. Freshness asks whether the
#: maintainers merge outside work now, and answers it from merges. Issue age
#: answers a different question badly: a three-week-old bug in a repository that
#: merged outside work last Tuesday is a normal backlog, not a closed door.
#: Reading the merge window as an age cap rejected eighteen repositories that
#: failed nothing else, among them `fsspec/filesystem_spec` with 282 unclaimed
#: issues. See https://github.com/wolfgang-aura/Mailman/issues/95.
ISSUE_WINDOW_DAYS = 90

#: How many recent default-branch commits to trace back to a pull request. Each
#: one costs an API call, so the sample is small and is recorded next to the
#: share it produced. A share computed over four commits is arithmetic.
DIRECT_PUSH_SAMPLE = 20

#: Above this share of direct pushes, the maintainer's habit is to write the
#: change himself on the default branch rather than review one. pdm-project/pdm
#: closed our correct one-line documentation pull request after Frost Ming made
#: the same change directly in `dc4e314`. The share decides nothing on its own;
#: `prescreen` reads it together with the size of the fix. See
#: https://github.com/wolfgang-aura/Mailman/issues/79.
DIRECT_PUSH_LIMIT = 0.5

#: Below this many readable commits the share is not evidence of a habit.
DIRECT_PUSH_SAMPLE_MINIMUM = 10

#: How far back to sample outside pull requests for the responsiveness gate.
#: Freshness asks whether outside work merges here at all; this asks how long
#: a stranger waits for a maintainer to say anything. Of the twelve pull
#: requests filed since 2026-09-01, one merged and six were closed unmerged,
#: and the closes came from repositories where an outside pull request waits
#: weeks for a first word: poetry, pdm, rqalpha. Every one of them passed
#: freshness, because a collaborator's merge counts there and a stranger's
#: silence does not.
RESPONSIVENESS_WINDOW_DAYS = 90

#: How many outside pull requests the gate reads in full. Each one costs three
#: API calls (reviews, review comments, issue comments), so the sample is
#: capped and the count read is recorded beside the numbers it produced.
RESPONSIVENESS_SAMPLE = 50

#: Below this many outside pull requests in the window, the numbers are
#: arithmetic, not evidence. The gate reports `unknown`, and unknown fails:
#: a repository nobody outside has written to in three months is not one
#: where our pull request will be read quickly.
RESPONSIVENESS_SAMPLE_MINIMUM = 3

#: A first maintainer response is on time inside this many days. The median
#: wait has to sit under it, and at least `FIRST_RESPONSE_SHARE` of the sample
#: has to have been answered inside it.
FIRST_RESPONSE_DAYS = 14
FIRST_RESPONSE_SHARE = 0.5

#: A repository that closes more outside pull requests than it merges is
#: saying no more often than yes. Below this many decided (merged or closed
#: unmerged) pull requests the ratio is not read, because two closes against
#: one merge is a week, not a habit.
REJECTION_DECIDED_MINIMUM = 5

#: Python has to be the language the repository is actually written in. On
#: `ccxt/ccxt` the Python is generated from TypeScript, and a patch to it is
#: thrown away by the next build.
MINIMUM_PYTHON_SHARE = 0.5

#: Languages that cannot generate Python source and therefore do not count
#: against the share. A notebook with saved outputs is mostly base64 images,
#: and on `domokane/FinancePy` it made 22% of a pure-Python library. See
#: https://github.com/wolfgang-aura/Mailman/issues/92.
_NOT_SOURCE_LANGUAGES = frozenset({
    "Jupyter Notebook", "HTML", "CSS", "SCSS", "Less", "TeX", "Markdown",
    "reStructuredText", "Batchfile", "Shell", "PowerShell", "Dockerfile",
    "Makefile", "CMake", "Roff", "Rich Text Format", "Jinja", "Smarty",
})

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
    # python-attrs/.github/AI_POLICY.md: "Absolutely **no** unsupervised
    # agentic tools". A ban on the tool rather than on the output, and this
    # project is the tool.
    r"|\bno\b[^.]{0,30}\bagentic\s+(?:tools?|agents?|coding|workflows?)"
    # quantumlib/Cirq CONTRIBUTING.md: "Code generated by artificial
    # intelligence tools does not qualify as your original creation", a ban
    # phrased as a CLA consequence. The gate read it as permission.
    r"|(?:ai|artificial intelligence)[- ]?(?:generated|tools?)[^.]{0,80}"
    r"(?:does|do)\W+not\W+qualify"
    r"|generated\s+by\s+(?:ai|artificial intelligence)[^.]{0,80}"
    r"(?:does|do)\W+not\W+qualify"
    r")",
    re.IGNORECASE,
)

#: A refusal phrased as what happens to the pull request rather than as a ban
#: on writing it. `getsentry/sentry-python` says "we won't review visibly
#: AI-generated PRs from an agent instructed to look for and "fix" open issues
#: in the repo", which is this project described exactly, and passed the gate
#: with `constraints: []`. See https://github.com/wolfgang-aura/Mailman/issues/99.
_REFUSAL_OUTCOME = re.compile(
    r"(?:"
    r"(?:wo|will|would|do|does|did)\s?n[o']?t\s+(?:be\s+)?"
    r"(?:review|accept|merge|consider|look at)"
    r"|will\s+(?:be\s+)?close(?:d)?"
    r"|(?:are|is|get|gets)\s+(?:automatically\s+)?closed"
    r"|automatically\s+closed"
    r"|clos(?:e|ed|ing)\s+(?:them\s+|it\s+|the\s+pr\s+)?outright"
    r"|closed\s+without\s+review"
    # "Pull requests that have an LLM product listed as co-author can't be
    # merged" is python-attrs's refusal, and `can't` was the one spelling of a
    # refusal this pattern did not know.
    r"|(?:can\s?n[o']?t|cannot|may\s+not)\s+(?:be\s+)?"
    r"(?:reviewed?|accepted?|merged?|considered?)"
    r")",
    re.IGNORECASE,
)

#: What the refusal has to be about before it counts. "We will close stale PRs"
#: is every repository on GitHub; "we won't review AI-generated PRs" is this
#: project.
_AI_SUBJECT = re.compile(
    r"\b(?:ai|a\.i\.|llms?|agents?|agentic|copilot|chatgpt|generated|"
    r"machine[- ]written)\b",
    re.IGNORECASE,
)

#: A guide that declines to review the *tool* is talking about who is
#: accountable for the patch, not refusing the patch. `securo-finance/securo`
#: says "We don't review the AI, we review you", and reading that as a ban
#: would quote a sentence that says the opposite of the rejection.
_REFUSES_THE_TOOL = re.compile(
    r"\s*(?:the\s+)?(?:ai|a\.i\.|llms?|models?|agents?|tools?|bots?)\b",
    re.IGNORECASE,
)

#: The constraint kind that is a gate on filing rather than a rule about how the
#: submission is written. Recorded as `requires_prior_discussion` in the gate's
#: data, which is what `prescreen` reads.
PRIOR_DISCUSSION = "prior-discussion"

#: A guide that requires the maintainers to have answered the issue before a
#: pull request exists. This is not a rule about how the patch is written, which
#: is what #43 added; it decides whether we may file at all.
_POLICY_PRIOR_DISCUSSION = re.compile(
    r"(?:"
    r"prior discussion required"
    r"|must show a conversation between you and a maintainer"
    r"|a maintainer must have (?:also )?responded"
    r"|(?:discuss|discussion)\s+(?:it\s+|this\s+)?"
    r"(?:with|in)\s+(?:a\s+|the\s+)?(?:maintainers?|issue)[^.]{0,60}"
    r"before\s+(?:opening|submitting|filing|raising|sending)"
    r"|(?:wait|waiting)\s+for\s+(?:a\s+)?maintainer[^.]{0,40}"
    r"(?:repl(?:y|ies)|respon(?:se|ds?)|confirm)"
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

#: The constraint that says a second pull request for an issue is refused
#: whatever it contains. Recorded as `forbids_duplicate_pull_requests`, which
#: is what `prescreen` and `check-target` read before deciding that a dormant
#: attempt may be superseded.
NO_DUPLICATE_PULL_REQUESTS = "no-duplicate-pull-requests"

#: urllib3's `docs/contributing.rst` and README: "Duplicate pull requests for
#: the same issue, including alternative solutions, will be rejected without
#: review unless a maintainer has approved opening an alternative pull request
#: in advance." Nothing read this, so the stale-attempt rule walked straight
#: into it and offered to supersede a dormant attempt in a repository that
#: closes the second pull request unread.
_POLICY_NO_DUPLICATES = re.compile(
    r"(?:"
    r"duplicate\s+pull\s+requests?[^.]{0,200}"
    r"(?:reject|close|closed|without\s+review|will\s+not\s+be\s+reviewed)"
    r"|(?:reject(?:ed)?|closed)[^.]{0,120}\bduplicate\s+pull\s+requests?"
    r"|alternative\s+pull\s+requests?[^.]{0,160}"
    r"(?:reject|close|approved?\s+in\s+advance)"
    r"|\bduplicates?\b[^.]{0,80}will\s+be\s+closed"
    r"|will\s+be\s+closed[^.]{0,80}\bduplicates?\b"
    r")",
    re.IGNORECASE,
)

_POLICY_PATHS = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    # urllib3 keeps its guide here, lower case, and the gate read none of it.
    "docs/contributing.rst",
    "AGENTS.md",
)

#: The links a contributing guide carries, in the three spellings a guide
#: writes them: inline, reference-style with the target defined elsewhere, and
#: reStructuredText for the `.rst` guides.
_INLINE_LINK = re.compile(r"\[(?P<text>[^\]\n]{1,160})\]\((?P<url>[^)\s]{1,400})\)")
_REFERENCE_USE = re.compile(r"\[(?P<text>[^\]\n]{1,160})\]\[(?P<ref>[^\]\n]{0,160})\]")
_REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[(?P<ref>[^\]\n]{1,160})\]:\s*<?(?P<url>\S{1,400}?)>?\s*$",
    re.MULTILINE,
)
_RST_LINK = re.compile(r"`(?P<text>[^`<\n]{1,160})<(?P<url>[^>`\s]{1,400})>`_")

#: What makes a link worth one more API call. `ai` and `llm` are matched as
#: whole words, because every third path in a repository contains the letters
#: of "ai" and none of those are the policy.
_POLICY_LINK_WORD = re.compile(
    r"(?:^|[^a-z])(?:ai|llms?|gen[-_ ]?ai|polic(?:y|ies))(?:[^a-z]|$)",
    re.IGNORECASE,
)

#: No guide links twenty policies, and an uncapped walk turns one gate into an
#: API budget.
_POLICY_LINK_LIMIT = 3


def _policy_links(body: str) -> list[tuple[str, str]]:
    """Every (link text, url) pair the guide carries, in document order."""
    definitions = {
        match.group("ref").strip().lower(): match.group("url").strip()
        for match in _REFERENCE_DEFINITION.finditer(body)
    }
    links: list[tuple[str, str]] = []
    for match in _INLINE_LINK.finditer(body):
        links.append((match.group("text"), match.group("url")))
    for match in _REFERENCE_USE.finditer(body):
        # `[text][]` is the shorthand whose reference is its own text.
        key = (match.group("ref").strip() or match.group("text")).strip().lower()
        target = definitions.get(key)
        if target:
            links.append((match.group("text"), target))
    for match in _RST_LINK.finditer(body):
        links.append((match.group("text"), match.group("url")))
    return links


def _linked_document(url: str, *, slug: str, guide: str) -> tuple[str, str] | None:
    """The contents path for a link, and the name to record the document under.

    Three shapes, because a project writes the same pointer three ways: an
    absolute `github.com` blob URL into another repository, which is where an
    organization keeps its `.github/AI_POLICY.md`; the `raw` host; and a path
    relative to the guide's own directory. Anything else is somebody's web
    page, and this gate reads repository files.
    """
    link = url.strip().split("#", 1)[0].split("?", 1)[0]
    if not link or link.startswith(("mailto:", "tel:")):
        return None
    ref: str | None = None
    if link.lower().startswith(("http://", "https://")):
        host, _, tail = link.split("://", 1)[1].partition("/")
        parts = [part for part in tail.split("/") if part]
        if host.lower() in ("github.com", "www.github.com"):
            if len(parts) < 5 or parts[2] not in ("blob", "raw", "tree"):
                return None
            owner, repository, _, ref = parts[:4]
            path = "/".join(parts[4:])
        elif host.lower() in ("raw.githubusercontent.com", "raw.github.com"):
            if len(parts) < 4:
                return None
            owner, repository, ref = parts[:3]
            path = "/".join(parts[3:])
        else:
            return None
    else:
        owner, _, repository = slug.partition("/")
        base = "" if link.startswith("/") else guide.rpartition("/")[0]
        path = posixpath.normpath(posixpath.join(base, link.lstrip("/")))
        if path.startswith("..") or path in (".", ""):
            return None
    if not path or path.endswith("/") or not owner or not repository:
        return None
    api = f"repos/{owner}/{repository}/contents/{path}"
    if ref:
        api += f"?ref={ref}"
    source = path if f"{owner}/{repository}" == slug else f"{owner}/{repository}/{path}"
    return api, source


def _followed_policies(
    gh: _Gh, slug: str, guide: str, body: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The documents the guide points at for its AI rules, read and unread.

    python-attrs/cattrs keeps one sentence in `CONTRIBUTING.md` — "If you use
    LLM / "AI" tools for your contributions, please read and follow our
    [_Generative AI / LLM Policy_][llm]" — and the rule itself in another
    repository, `python-attrs/.github/AI_POLICY.md`: "Absolutely **no**
    unsupervised agentic tools". Stopping at the first file recorded
    `constraints: []` and passed a repository that refuses this project by
    name. See https://github.com/wolfgang-aura/Mailman/issues/99.
    """
    read: list[dict[str, Any]] = []
    unread: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text, url in _policy_links(body):
        if not (_POLICY_LINK_WORD.search(text) or _POLICY_LINK_WORD.search(url)):
            continue
        resolved = _linked_document(url, slug=slug, guide=guide)
        if resolved is None:
            continue
        api, source = resolved
        if source == guide or api in seen:
            continue
        seen.add(api)
        if len(read) + len(unread) >= _POLICY_LINK_LIMIT:
            break
        entry = {"source": source, "url": url.strip(), "link_text": text.strip()}
        document = _decoded(gh.json(api))
        if document.strip():
            read.append({**entry, "body": document})
        else:
            # Unread, so the policy is unknown. A gate that reads that as
            # permission is the gate that passed cattrs.
            unread.append(entry)
    return read, unread


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


def _sentence(flat: str, start: int, end: int, *, limit: int = 400) -> str:
    """The sentence a match sits in, so the record quotes a readable claim."""
    left = flat.rfind(". ", 0, start)
    opening = 0 if left == -1 else left + 2
    right = flat.find(". ", end)
    closing = len(flat) if right == -1 else right + 1
    return flat[opening:closing].strip()[:limit]


def _outcome_refusal(flat: str) -> str | None:
    """A refusal stated as what happens to the pull request, about AI work.

    One sentence, not a window. Read against the 121 contributing guides this
    hunt has screened, a 200-character window around the refusal found one more
    repository and it was a false one: `agronholm/anyio` closes a pull request
    that erases the template, two sentences from an unrelated mention of AI.
    Every real refusal among those guides names its subject in its own
    sentence, sentry's included.
    """
    for match in _REFUSAL_OUTCOME.finditer(flat):
        if _REFUSES_THE_TOOL.match(flat, match.end()):
            continue
        sentence = _sentence(flat, match.start(), match.end())
        if _AI_SUBJECT.search(sentence):
            return sentence
    return None


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
    source = {
        name: value
        for name, value in languages.items()
        if isinstance(value, (int, float)) and name not in _NOT_SOURCE_LANGUAGES
    }
    total = sum(source.values())
    python_share = (source.get("Python", 0) / total) if total else 0.0
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
        dominant = max(source, key=source.get)
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


#: Each constraint the gate reads, in the order the record lists them. A
#: project can permit the code and still refuse a model-written body or a
#: commit under a tool's account, and those have to reach the run rather than
#: be summarised away into the word "pass".
_CONSTRAINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("disclosure", _POLICY_DISCLOSURE),
    ("own-words", _POLICY_OWN_WORDS),
    ("human-account", _POLICY_HUMAN_ACCOUNT),
    (PRIOR_DISCUSSION, _POLICY_PRIOR_DISCUSSION),
    (NO_DUPLICATE_PULL_REQUESTS, _POLICY_NO_DUPLICATES),
)

#: The constraints whose quote is the whole sentence rather than the matched
#: phrase, because the phrase alone does not say what the rule is.
_SENTENCE_CONSTRAINTS = frozenset({PRIOR_DISCUSSION, NO_DUPLICATE_PULL_REQUESTS})


def _constraints(source: str, flat: str, found: list[dict[str, Any]]) -> None:
    """Add this document's constraints to `found`, first document winning."""
    known = {entry["kind"] for entry in found}
    for kind, pattern in _CONSTRAINT_PATTERNS:
        if kind in known:
            continue
        match = pattern.search(flat)
        if not match:
            continue
        found.append(
            {
                "kind": kind,
                "quote": (
                    _sentence(flat, match.start(), match.end())
                    if kind in _SENTENCE_CONSTRAINTS
                    else match.group(0)
                ),
                "source": source,
            }
        )


def _quoted(constraints: list[dict[str, Any]], kind: str) -> str | None:
    for entry in constraints:
        if entry["kind"] == kind:
            return entry["quote"]
    return None


def _policy_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Gate 4. Does the guide, or the policy it links, close an AI-assisted pull request?"""
    for relative in _POLICY_PATHS:
        body = _decoded(gh.json(f"repos/{slug}/contents/{relative}"))
        if not body:
            continue
        followed, unread = _followed_policies(gh, slug, relative, body)
        documents = [{"source": relative, "body": body}, *followed]
        trail = {
            "followed_documents": [
                {key: entry[key] for key in ("source", "url", "link_text")}
                for entry in followed
            ],
            "unread_documents": unread,
        }
        for document in documents:
            flat = " ".join(document["body"].split())
            ban = _POLICY_BANS.search(flat)
            # A ban is written two ways: as a rule about what may be submitted,
            # and as a statement of what will happen to it. The second is the
            # one sentry-python uses, and reading only the first spent a whole
            # hunt on a patch its automation closes on arrival. Mailman #99.
            refusal = ban.group(0) if ban else _outcome_refusal(flat)
            if not refusal:
                continue
            return _gate(
                "policy",
                passed=False,
                blocking=True,
                detail=(
                    f"{document['source']} refuses AI-assisted work: {refusal!r}"
                ),
                data={
                    "source": document["source"],
                    "guide": relative,
                    "result": "refused",
                    "quote": refusal,
                    **trail,
                },
            )
        if unread:
            # The guide points at the document that decides and the document
            # could not be read. Unknown is not permission.
            named = ", ".join(entry["source"] for entry in unread)
            return _gate(
                "policy",
                passed=False,
                blocking=True,
                detail=(
                    f"{relative} sends contributors to {named}, which could not "
                    "be read, so whether this project closes AI-assisted work "
                    "is unknown"
                ),
                data={
                    "source": relative,
                    "guide": relative,
                    "result": "unknown",
                    "quote": None,
                    **trail,
                },
            )
        constraints: list[dict[str, Any]] = []
        for document in documents:
            flat = " ".join(document["body"].split())
            _constraints(document["source"], flat, constraints)
        kinds = {entry["kind"] for entry in constraints}
        read = ", ".join(entry["source"] for entry in documents)
        if constraints:
            summary = "; ".join(
                f"{entry['kind']}: {entry['quote']!r}" for entry in constraints
            )
            detail = f"{read} permits the code and constrains the submission - {summary}"
            if "own-words" in kinds:
                detail += (
                    ". Set `requires_own_words` in the target policy so "
                    "prepare-submission refuses a generated body."
                )
            if PRIOR_DISCUSSION in kinds:
                detail += (
                    " A maintainer has to have answered the issue before a "
                    "pull request exists here, so `prescreen` refuses an "
                    "unanswered one."
                )
            if NO_DUPLICATE_PULL_REQUESTS in kinds:
                detail += (
                    " A second pull request for an issue is rejected here "
                    "without review, so a dormant open attempt is still the "
                    "claim and cannot be superseded."
                )
        else:
            detail = f"{read} says nothing that closes AI-assisted work"
        return _gate(
            "policy",
            passed=True,
            blocking=True,
            detail=detail,
            data={
                "source": relative,
                "guide": relative,
                "result": "permitted",
                "requires_disclosure": "disclosure" in kinds,
                "requires_own_words": "own-words" in kinds,
                "requires_human_account": "human-account" in kinds,
                "requires_prior_discussion": PRIOR_DISCUSSION in kinds,
                "forbids_duplicate_pull_requests": (
                    NO_DUPLICATE_PULL_REQUESTS in kinds
                ),
                "constraints": constraints,
                "quote": _quoted(constraints, "disclosure"),
                **trail,
            },
        )
    return _gate(
        "policy",
        passed=True,
        blocking=False,
        detail="no contributing guide found, so nothing forbids the work in writing",
        data={"source": None, "result": "no-guide"},
    )


#: What a workflow says when it closes a pull request whose issue is not
#: assigned to the author. pydantic-ai's pr-guard.yml: "Contributors should
#: discuss and be assigned an issue before opening a PR" and "please wait to
#: be assigned before opening a PR ... closed automatically because issue #N
#: is not assigned to you". langchain says it with a marker instead; the
#: comment search below covers that spelling.
_ASSIGNMENT_RULE = re.compile(
    r"wait to be assigned"
    r"|is not assigned to you"
    r"|must be assigned"
    r"|be assigned (?:an|the|to an|to the) issue before opening",
    re.IGNORECASE,
)
_ISSUE_AUTHOR_EXEMPT = re.compile(
    r"issue (?:and bot )?authors? (?:are|is) exempt|author of the issue", re.IGNORECASE
)


def _workflow_assignment_rule(gh: _Gh, slug: str) -> dict[str, Any] | None:
    """The workflow that closes unassigned outside pull requests, if one is written down.

    pydantic/pydantic-ai passed the screen on 2026-09-14 (#89) because its rule is
    in `.github/workflows/pr-guard.yml`, in plain words, and the gate only
    knew langchain's bot marker. A workflow that names the rule is better
    evidence than a closed pull request: it is the rule, not one enforcement.
    """
    listing = gh.json(f"repos/{slug}/contents/.github/workflows")
    if not isinstance(listing, list):
        return None
    for entry in listing:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.endswith((".yml", ".yaml")):
            continue
        body = _decoded(gh.json(f"repos/{slug}/contents/.github/workflows/{name}"))
        match = _ASSIGNMENT_RULE.search(body)
        if match and re.search(r"clos", body, re.IGNORECASE):
            return {
                "workflow": name,
                "phrase": match.group(0),
                "issue_author_exempt": bool(_ISSUE_AUTHOR_EXEMPT.search(body)),
            }
    return None


def _assignment_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Reject repositories whose bot closes unassigned outside pull requests.

    Two readings, either one enough. The workflows are read for the rule in
    the project's own words. Then GitHub issue search, which indexes comments
    as well as the pull request body, shortlists closed pull requests; the
    gate reads those comments and requires the exact marker from a bot,
    avoiding GitHub's loose token matches.
    """
    rule = _workflow_assignment_rule(gh, slug)
    if rule:
        exempt = (
            "; the workflow exempts issue authors, so a self-reported defect stays filable"
            if rule["issue_author_exempt"]
            else ""
        )
        return _gate(
            "assignment",
            passed=False,
            blocking=True,
            detail=(
                f"{rule['workflow']} closes pull requests whose issue is not assigned "
                f"to the author (\"{rule['phrase']}\"){exempt}"
            ),
            data={"marker": None, "workflow_rule": rule},
        )
    query = quote(f'repo:{slug} is:pr is:closed "require-issue-link"')
    result = gh.json(f"search/issues?q={query}&per_page=5")
    candidates = result.get("items") if isinstance(result, dict) else None
    comments: list[dict[str, Any]] = []
    if isinstance(candidates, list):
        for pull in candidates[:5]:
            number = pull.get("number") if isinstance(pull, dict) else None
            if not isinstance(number, int):
                continue
            rows = gh.json(f"repos/{slug}/issues/{number}/comments")
            if isinstance(rows, list):
                for row in rows:
                    row["_pull_request"] = number
                comments.extend(rows)
    markers = enforcement_markers(comments)
    enforcement = next(
        (row for row in markers if row["marker"] == "require-issue-link"), None
    )
    data = {
        "marker": "require-issue-link",
        "workflow_rule": None,
        "search_matches": (
            result.get("total_count") if isinstance(result, dict) else None
        ),
        "verified_occurrences": enforcement["count"] if enforcement else 0,
        "seen_on": enforcement["seen_on"] if enforcement else [],
    }
    if enforcement:
        return _gate(
            "assignment",
            passed=False,
            blocking=True,
            detail=(
                "the require-issue-link bot marker was verified on closed pull "
                f"request(s) {', '.join('#' + str(n) for n in enforcement['seen_on'])}; "
                "external contributors must be assigned before opening a pull request"
            ),
            data=data,
        )
    if result is None:
        return _gate(
            "assignment",
            passed=False,
            blocking=True,
            detail="assignment enforcement search was unavailable",
            data=data,
        )
    return _gate(
        "assignment",
        passed=True,
        blocking=True,
        detail=(
            "no workflow names an assignment rule and no require-issue-link bot "
            "marker was found in the search samples"
        ),
        data=data,
    )


#: Label spellings that mark an issue as a request rather than a defect. A
#: tracker can carry forty unclaimed enhancement requests and still have no
#: work a bug run could take.
_ENHANCEMENT_LABELS = ("enhancement", "feature", "feature-request")

#: Reading one issue's comments costs one API call, so the reads stop after
#: this many unclaimed candidates. A tracker with more unclaimed issues than
#: this is not saturated in any sense this gate needs to measure precisely;
#: the cap is recorded rather than hidden.
_COMMENT_THREAD_LIMIT = 40


def _is_enhancement(row: dict[str, Any]) -> bool:
    labels = row.get("labels")
    if not isinstance(labels, list):
        return False
    names = []
    for entry in labels:
        if isinstance(entry, dict):
            names.append(str(entry.get("name") or "").lower())
        elif isinstance(entry, str):
            names.append(entry.lower())
    return any(
        "enhancement" in name or name in _ENHANCEMENT_LABELS for name in names
    )


def _age_in_days(row: dict[str, Any], now: datetime) -> int | None:
    created = str(row.get("created_at") or "")[:10]
    if not created:
        return None
    try:
        return (now - datetime.fromisoformat(created).replace(tzinfo=UTC)).days
    except ValueError:
        return None


def _read_thread(gh: _Gh, slug: str, number: str) -> dict[str, Any]:
    """Read one issue's thread for a claim, and for what ranks it.

    The claim is the form GitHub itself does not track: a comment saying
    "I'm on it" that no maintainer has answered. `mailman claims` reads the
    same thread for a single run; saturation applies the same judgement, so
    the two gates cannot disagree about what a claim is. The same page also
    says whether a maintainer asked for the pull request, when a maintainer
    last wrote, and which pull requests the thread cites, and the shortlist
    ranks on those. One read answers all four.
    """
    comments = gh.pages(
        f"repos/{slug}/issues/{number}/comments?per_page=100", pages=1
    )
    return {
        "comments": comments,
        "claimed": any(
            kind in {"claim", "assignment"} for kind in classify_thread(comments)
        ),
        "maintainer_touched_at": maintainer_touched_at(comments),
        "cited": [
            comment.get("body") for comment in comments if isinstance(comment, dict)
        ],
    }


def _shortlist_row(
    row: dict[str, Any],
    thread: dict[str, Any] | None,
    *,
    slug: str,
    age: int,
    cited_elsewhere: bool,
    now: datetime,
) -> dict[str, Any]:
    """One ranked shortlist entry, with the reasons that put it where it is."""
    cited = pull_request_references(
        [row.get("body"), *((thread or {}).get("cited") or [])],
        repository=slug,
        exclude=[(slug, int(row["number"]))],
    )
    ranked = rank_issue(
        row,
        (thread or {}).get("comments") or [],
        linked_pull_requests=cited_elsewhere or bool(cited),
        maintainer_touched_at=(thread or {}).get("maintainer_touched_at"),
        now=now,
    )
    return {
        "number": row["number"],
        "title": row.get("title"),
        "age_days": age,
        "labels": [
            str(label.get("name") or "") if isinstance(label, dict) else str(label)
            for label in row.get("labels") or []
        ],
        "thread_read": thread is not None,
        "score": ranked["score"],
        "reasons": ranked["reasons"],
    }


def _saturation_gate(
    gh: _Gh, slug: str, window_days: int, issue_window_days: int
) -> dict[str, Any]:
    """Gate 5. Is there any unclaimed work left, or has the tracker been mined?

    A claim is counted from three sources, because no one of them covers the
    field. An open pull request that names the issue in its title, body, or
    branch is one. A cross-reference from an open pull request is GitHub's own
    record of the same thing. And an unanswered work claim in the issue's
    comments is the claim that has not become a pull request yet - on
    `openai/openai-agents-python` on 2026-09-06, seventeen of twenty-two
    unassigned issues were claimed by an open pull request whose body merely
    mentioned the issue in prose, which GitHub does not treat as a claim and a
    maintainer does. See
    https://github.com/wolfgang-aura/Mailman/issues/53.

    `window_days` is the merge window and is used here only to be recorded
    beside the answer. Issue age is capped by `issue_window_days`, which is a
    separate and much longer window, because the two measure different things.
    See https://github.com/wolfgang-aura/Mailman/issues/95.
    """
    issues = gh.pages(
        f"repos/{slug}/issues?state=open&sort=created&direction=desc", pages=4
    )
    open_issues = [row for row in issues if "pull_request" not in row]
    open_pulls = [row for row in issues if "pull_request" in row]
    unassigned = [row for row in open_issues if not row.get("assignee")]
    claims = classify_claims(
        gh.pages(f"repos/{slug}/pulls?state=open&sort=updated&direction=desc", pages=4)
    )
    claimed = set(claims["claiming"])
    claimed_by_comment: set[str] = set()
    # An issue already claimed by a pull request needs no comment read, so the
    # extra API calls are spent only where they can change the answer.
    candidates = [
        row for row in unassigned if str(row["number"]) not in claimed
    ]
    threads_capped = max(0, len(candidates) - _COMMENT_THREAD_LIMIT)
    threads: dict[str, dict[str, Any]] = {}
    for row in candidates[:_COMMENT_THREAD_LIMIT]:
        number = str(row["number"])
        threads[number] = _read_thread(gh, slug, number)
        if threads[number]["claimed"]:
            claimed_by_comment.add(number)
    claimed |= claimed_by_comment

    unclaimed = [
        row for row in unassigned if str(row["number"]) not in claimed
    ]
    now = datetime.now(UTC)
    # Unclaimed is not the same as workable. An enhancement request is not a
    # bug run, and an issue nobody has opened in years is a different kind of
    # backlog; letting either into the median age turned nine nominal openings
    # into one real one on openai/openai-agents-python. The age cap is the
    # issue window, not the merge window: whether an old bug is still real is
    # decided later, by `prescreen` and by `reproduce` at the base commit.
    workable = []
    shortlist: list[dict[str, Any]] = []
    enhancement_labelled = 0
    stale_beyond_window = 0
    for row in unclaimed:
        if _is_enhancement(row):
            enhancement_labelled += 1
            continue
        age = _age_in_days(row, now)
        if age is None or age > issue_window_days:
            stale_beyond_window += 1
            continue
        workable.append(age)
        # The count alone was what the record used to keep, and a coordinator
        # rebuilt the list by hand from it. Now the issues themselves are
        # kept, ranked by whether a maintainer asked for them, how recently
        # anybody touched them, and whether any pull request is on record.
        # https://github.com/wolfgang-aura/Mailman/issues/102
        number = str(row["number"])
        shortlist.append(
            _shortlist_row(
                row,
                threads.get(number),
                slug=slug,
                age=age,
                cited_elsewhere=number in claims["abandoned"],
                now=now,
            )
        )
    shortlist = sort_shortlist(shortlist)
    data = {
        "shortlist": shortlist,
        "maintainer_invited": sum(
            1 for row in shortlist if MAINTAINER_INVITED in row["reasons"]
        ),
        "open_issues": len(open_issues),
        "open_pull_requests_seen": len(open_pulls),
        "unassigned": len(unassigned),
        "claimed_by_pull_request": len(claims["claiming"]),
        "claimed_by_comment": len(claimed_by_comment),
        "unclaimed": len(unclaimed),
        "workable": len(workable),
        "enhancement_labelled": enhancement_labelled,
        "stale_beyond_window": stale_beyond_window,
        "median_workable_age_days": (
            round(statistics.median(workable)) if workable else None
        ),
        "comment_threads_read": min(len(candidates), _COMMENT_THREAD_LIMIT),
        "comment_threads_capped": threads_capped,
        "claimed_share": (
            round(1 - len(unclaimed) / len(unassigned), 2) if unassigned else None
        ),
        "window_days": window_days,
        "issue_window_days": issue_window_days,
    }
    if not unclaimed:
        return _gate(
            "saturation",
            passed=False,
            blocking=True,
            detail=(
                f"{len(unassigned)} unassigned open issue(s) and an open pull "
                "request or a comment already names every one of them"
            ),
            data=data,
        )
    if not workable:
        return _gate(
            "saturation",
            passed=False,
            blocking=True,
            detail=(
                f"{len(unclaimed)} issue(s) carry no claim of any kind, but "
                f"none is workable: {enhancement_labelled} enhancement-labelled, "
                f"{stale_beyond_window} older than the {issue_window_days}-day "
                f"issue window (outside merges are counted over "
                f"{window_days} days)"
            ),
            data=data,
        )
    return _gate(
        "saturation",
        passed=True,
        blocking=False,
        detail=(
            f"{len(unclaimed)} of {len(unassigned)} unassigned issue(s) have no "
            f"claim of any kind, {len(workable)} of them workable, "
            f"{data['maintainer_invited']} asked for by a maintainer, median "
            f"workable age {data['median_workable_age_days']} day(s)"
        ),
        data=data,
    )


def _direct_push_gate(gh: _Gh, slug: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Gate 7. How often does a change reach this branch without a review?

    Reported, never blocking on its own. A maintainer who pushes most of his
    commits straight to the default branch will fix a one-line issue himself
    faster than he will read a stranger's pull request for it, and our run is
    spent either way. `prescreen` is where the two halves meet: this share and
    the estimated size of the fix. See
    https://github.com/wolfgang-aura/Mailman/issues/79.
    """
    branch = str(meta.get("default_branch") or "")
    commits = gh.pages(
        f"repos/{slug}/commits?sha={quote(branch, safe='')}", pages=1
    )[:DIRECT_PUSH_SAMPLE]
    direct = 0
    reviewed = 0
    unread = 0
    for commit in commits:
        sha = str(commit.get("sha") or "")
        if not sha:
            unread += 1
            continue
        pulls = gh.json(f"repos/{slug}/commits/{sha}/pulls")
        if not isinstance(pulls, list):
            unread += 1
        elif pulls:
            reviewed += 1
        else:
            direct += 1
    sample = direct + reviewed
    share = round(direct / sample, 2) if sample else None
    data = {
        "default_branch": branch,
        "commits_sampled": sample,
        "commits_unread": unread,
        "direct_pushes": direct,
        "through_pull_request": reviewed,
        "direct_push_share": share,
        "direct_push_limit": DIRECT_PUSH_LIMIT,
        "sample_minimum": DIRECT_PUSH_SAMPLE_MINIMUM,
    }
    if share is None:
        return _gate(
            "direct-push",
            passed=True,
            blocking=False,
            detail=f"no commit on {branch or 'the default branch'} could be read",
            data=data,
        )
    if sample >= DIRECT_PUSH_SAMPLE_MINIMUM and share >= DIRECT_PUSH_LIMIT:
        return _gate(
            "direct-push",
            passed=False,
            blocking=False,
            detail=(
                f"{direct} of {sample} recent commit(s) on {branch} arrived "
                f"outside a pull request, a share of {share}; a small fix here "
                "is likely to be written rather than reviewed"
            ),
            data=data,
        )
    return _gate(
        "direct-push",
        passed=True,
        blocking=False,
        detail=(
            f"{direct} of {sample} recent commit(s) on {branch} arrived outside "
            f"a pull request, a share of {share}"
        ),
        data=data,
    )


def _timestamp(value: Any) -> datetime | None:
    """Parse one GitHub timestamp, or nothing when the row has none."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_maintainer(row: dict[str, Any]) -> bool:
    return (
        row.get("author_association") in MAINTAINER_ASSOCIATIONS
        and not _is_bot(row.get("user"))
    )


def _first_maintainer_response(
    gh: _Gh, slug: str, number: int, opened: datetime
) -> float | None:
    """Days from a pull request opening to the first word from a maintainer.

    A review, an inline review comment and an issue comment are three
    different endpoints, and a maintainer's first word can be any of them.
    Whichever came first counts. The author's own comments are never a
    response, and neither is a bot's.
    """
    stamps: list[datetime] = []
    for path, field in (
        (f"repos/{slug}/pulls/{number}/reviews", "submitted_at"),
        (f"repos/{slug}/pulls/{number}/comments", "created_at"),
        (f"repos/{slug}/issues/{number}/comments", "created_at"),
    ):
        for row in gh.pages(path, pages=1):
            if not isinstance(row, dict) or not _is_maintainer(row):
                continue
            stamp = _timestamp(row.get(field))
            if stamp is not None and stamp >= opened:
                stamps.append(stamp)
    if not stamps:
        return None
    return (min(stamps) - opened).total_seconds() / 86400


def _responsiveness_gate(
    gh: _Gh, slug: str, responsiveness_days: int
) -> dict[str, Any]:
    """Gate 8. How long does a stranger's pull request wait for a first word?

    Freshness is satisfied by a collaborator's merge and says nothing about a
    stranger's silence. This gate reads the outside pull requests opened in
    the window and asks three things of them: how long the median one waited
    for a maintainer to review or comment, what share got any response inside
    `FIRST_RESPONSE_DAYS`, and whether more were closed unmerged than merged.
    An unanswered pull request has waited its whole age, and is counted at
    that, because a silence that has not ended is not a short wait.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(days=responsiveness_days)
    rows = gh.pages(
        f"repos/{slug}/pulls?state=all&sort=created&direction=desc", pages=2
    )
    outside: list[tuple[dict[str, Any], datetime]] = []
    excluded_bots: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        opened = _timestamp(row.get("created_at"))
        if opened is None or opened < window_start:
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else None
        if user and _is_bot(user) and user.get("login"):
            excluded_bots.add(str(user["login"]))
        if is_outside_human(row):
            outside.append((row, opened))
    in_window = len(outside)
    sample = outside[:RESPONSIVENESS_SAMPLE]

    waits: list[float] = []
    responded = 0
    responded_within = 0
    merged = 0
    closed_unmerged = 0
    for row, opened in sample:
        number = int(row.get("number") or 0)
        wait = _first_maintainer_response(gh, slug, number, opened)
        merged_at = _timestamp(row.get("merged_at"))
        if merged_at is not None:
            # A merge is a maintainer's answer even when nobody wrote a word;
            # fsspec merges most outside work silently and read as unanswered.
            merge_wait = max((merged_at - opened).total_seconds() / 86400, 0.0)
            wait = merge_wait if wait is None else min(wait, merge_wait)
        if wait is None:
            waits.append((now - opened).total_seconds() / 86400)
        else:
            responded += 1
            waits.append(wait)
            if wait <= FIRST_RESPONSE_DAYS:
                responded_within += 1
        if row.get("merged_at"):
            merged += 1
        elif (row.get("state") or "") == "closed":
            closed_unmerged += 1
    sampled = len(sample)
    median = round(statistics.median(waits), 1) if waits else None
    share = round(responded_within / sampled, 2) if sampled else None
    decided = merged + closed_unmerged
    data = {
        "result": "unknown",
        "window_days": responsiveness_days,
        "pull_requests_scanned": len(rows),
        "outside_pull_requests_in_window": in_window,
        "sampled": sampled,
        "sample_cap": RESPONSIVENESS_SAMPLE,
        "responded": responded,
        "responded_within_days": responded_within,
        "response_share": share,
        "median_first_response_days": median,
        "merged": merged,
        "closed_unmerged": closed_unmerged,
        "still_open": sampled - decided,
        "excluded_bot_authors": sorted(excluded_bots),
        "first_response_days": FIRST_RESPONSE_DAYS,
        "first_response_share": FIRST_RESPONSE_SHARE,
        "sample_minimum": RESPONSIVENESS_SAMPLE_MINIMUM,
        "decided_minimum": REJECTION_DECIDED_MINIMUM,
    }
    if sampled < RESPONSIVENESS_SAMPLE_MINIMUM:
        return _gate(
            "responsiveness",
            passed=False,
            blocking=True,
            detail=(
                f"unknown: {sampled} outside pull request(s) opened in "
                f"{responsiveness_days} days, fewer than the "
                f"{RESPONSIVENESS_SAMPLE_MINIMUM} needed to measure a wait. "
                "Unknown is not responsive."
            ),
            data=data,
        )
    numbers = (
        f"median first maintainer response {median} day(s), "
        f"{responded_within} of {sampled} answered within {FIRST_RESPONSE_DAYS} "
        f"days ({share:.0%}), {merged} merged, {closed_unmerged} closed unmerged"
        + (f"; excluded {', '.join(sorted(excluded_bots))}" if excluded_bots else "")
    )
    reasons: list[str] = []
    if median is not None and median > FIRST_RESPONSE_DAYS:
        reasons.append(f"the median wait is over {FIRST_RESPONSE_DAYS} days")
    if share is not None and share < FIRST_RESPONSE_SHARE:
        reasons.append(
            f"under {FIRST_RESPONSE_SHARE:.0%} were answered within "
            f"{FIRST_RESPONSE_DAYS} days"
        )
    if decided >= REJECTION_DECIDED_MINIMUM and closed_unmerged > merged:
        reasons.append("more outside pull requests were closed unmerged than merged")
    data["result"] = "fail" if reasons else "pass"
    if reasons:
        return _gate(
            "responsiveness",
            passed=False,
            blocking=True,
            detail=f"{numbers}: " + "; ".join(reasons),
            data=data,
        )
    return _gate(
        "responsiveness", passed=True, blocking=True, detail=numbers, data=data
    )

def direct_push_share(record: dict[str, Any] | None) -> float | None:
    """The share a recorded screen measured, for a stage that has no API budget.

    `prescreen` needs the number and must not re-read the repository to get it.
    A repository with no screen, or a screen written before this gate existed,
    returns `None` and the caller treats the habit as unknown.
    """
    if not isinstance(record, dict):
        return None
    for gate in record.get("gates") or []:
        if isinstance(gate, dict) and gate.get("name") == "direct-push":
            share = (gate.get("data") or {}).get("direct_push_share")
            return share if isinstance(share, int | float) else None
    return None


def _recorded_constraint(
    record: dict[str, Any] | None, *, kind: str, flag: str
) -> dict[str, Any] | None:
    """One constraint out of a written screen, or `None` when it is not there.

    `prescreen` has the claims record and no API budget to re-read the guide,
    exactly as with `direct_push_share`. A repository with no screen, or one
    written before this constraint existed, returns `None`: unknown, not clear.
    """
    if not isinstance(record, dict):
        return None
    for gate in record.get("gates") or []:
        if not isinstance(gate, dict) or gate.get("name") != "policy":
            continue
        data = gate.get("data") or {}
        if not data.get(flag):
            return None
        for entry in data.get("constraints") or []:
            if isinstance(entry, dict) and entry.get("kind") == kind:
                return entry
        return {"kind": kind, "quote": None}
    return None


def requires_prior_discussion(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The constraint, when the guide wants a maintainer to answer first.

    See https://github.com/wolfgang-aura/Mailman/issues/99.
    """
    return _recorded_constraint(
        record, kind=PRIOR_DISCUSSION, flag="requires_prior_discussion"
    )


def forbids_duplicate_pull_requests(
    record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The constraint, when a second pull request for an issue is refused unread.

    Read before any stage decides that a dormant attempt has stopped claiming
    its issue: where this rule is in force, the attempt is still the pull
    request the maintainers count, and ours would be the duplicate.
    """
    return _recorded_constraint(
        record,
        kind=NO_DUPLICATE_PULL_REQUESTS,
        flag="forbids_duplicate_pull_requests",
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
    issue_window_days: int = ISSUE_WINDOW_DAYS,
    responsiveness_days: int = RESPONSIVENESS_WINDOW_DAYS,
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
        "issue_window_days": issue_window_days,
        "responsiveness_days": responsiveness_days,
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
        _assignment_gate(gh, slug),
        _saturation_gate(gh, slug, window_days, issue_window_days),
        _direct_push_gate(gh, slug, meta),
        _responsiveness_gate(gh, slug, responsiveness_days),
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


def screen_shortlist(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ranked workable issues a screen recorded, first to pre-screen first."""
    if not isinstance(record, dict):
        return []
    for gate in record.get("gates") or []:
        if isinstance(gate, dict) and gate.get("name") == "saturation":
            rows = (gate.get("data") or {}).get("shortlist") or []
            return [row for row in rows if isinstance(row, dict)]
    return []


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
    shortlist = screen_shortlist(record)
    if verdict == "pass" and shortlist:
        # Ranked, so the first line is the issue to pre-screen first. The
        # codes are maintainer-invited, recent and no-linked-pr, in that
        # order of weight; the procedure says what each one means.
        lines.append("  shortlist (ranked; pre-screen from the top):")
        lines += render_shortlist(shortlist)
    if verdict == "pass":
        lines.append("  verdict: worth a run")
    else:
        lines.append("  verdict: rejected on " + ", ".join(record["failed_gates"]))
    return "\n".join(lines)
