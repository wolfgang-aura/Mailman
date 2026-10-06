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
import sys
import tomllib
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from mailman.claims import (
    classify_comment,
    classify_thread,
    maintainer_dispute,
    maintainer_labels,
    maintainer_touched_at,
    pull_request_references,
    rival_pull_requests,
)
from mailman.executor import CommandResult, execute
from mailman.maintainers import MERGERS_QUERY, is_maintainer, mergers
from mailman.target_intel import (
    FRESHNESS_WINDOW_DAYS,
    _Gh,
    fetch_page,
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

SCREEN_SCHEMA_VERSION = 4
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

#: A third way through: years of public history stand in for stars.
#: `jupyter/jupyter_client` (479 stars, 11 years) and `celery/billiard` (434,
#: 16 years) failed the 500-star floor, and nothing about them is a stranger's
#: unread `setup.py`. Mailman #318.
PROVENANCE_LONG_STANDING_AGE_DAYS = 1095
PROVENANCE_LONG_STANDING_STARS = 150

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

#: Enough other merged authors that the dominant one is a prolific contributor
#: beside an open door, not the whole door. pypa/pipx had 22 around an 82%
#: share and failed. https://github.com/wolfgang-aura/Mailman/issues/270
DOMINANT_EXCEPTION_AUTHORS = 5

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
#:
#: Ninety days still cut away the only work nobody races for. On 2026-09-25 a
#: 90-day cap left twelve screened repositories with no workable issue, while
#: `pytest-dev/pytest-xdist`, `ApeWorX/ape` and `conan-io/conan` held 231, 168
#: and 79 unclaimed issues just past it. Re-screened at 730 days, 43 of 68
#: sampled old bugs in six passing repositories had a maintainer in the thread.
#: Whether an old bug is still real is `reproduce`'s question, not this one.
ISSUE_WINDOW_DAYS = 730

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
#: Pull requests whose first maintainer response is read at the same time.
RESPONSIVENESS_WORKERS = 4

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

#: A repository that merges under this share of the outside pull requests it
#: decides is saying no as a habit. It was "more closed than merged" until
#: 2026-09-29; a famous repository closes a stream of low-effort pull requests
#: within a day, and huggingface_hub (20 merged, 21 closed, median answer 0.8
#: days) failed on one pull request. Repositories merging 5 to 16 percent
#: (black, pip, pytest, openai-agents-python) still fail.
REJECTION_MERGE_SHARE = 0.3
#: Below this many decided (merged or closed unmerged) pull requests the share
#: is not read, because two closes against one merge is a week, not a habit.
REJECTION_DECIDED_MINIMUM = 5

#: Which responsiveness rules judged a screen. 1: more closed than merged
#: failed (a screen without the stamp). 2: under 30% merged fails (685200d).
#: 3: an unanswered pull request younger than FIRST_RESPONSE_DAYS is left out
#: of the share (#224). Bump it whenever the gate's arithmetic changes, so
#: `hunt targets` can offer the failures an older rule produced. Mailman #227.
RESPONSIVENESS_RULES_VERSION = 3

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

#: Service containers this host does not run: no Postgres, MySQL, Redis or
#: broker listens here, and a screen does not start Docker. A test workflow
#: that starts one needs it. netbox-community/netbox. Mailman #255.
HOST_MISSING_SERVICES = (
    "postgres", "postgis", "mysql", "mariadb", "redis", "valkey", "mongo",
    "rabbitmq", "elasticsearch", "opensearch", "memcached", "kafka",
)
_SERVICE_IMAGE = re.compile(
    r"^\s*image:\s*['\"]?(?:[\w.-]+/)*("
    + "|".join(HOST_MISSING_SERVICES)
    + r")[\w-]*(?=[:'\"\s@]|$)",
    re.IGNORECASE | re.MULTILINE,
)

#: Files whose presence means a compiler may be in the build. The operator has
#: no Rust or MSVC toolchain, so `Cargo.toml` is fatal on its own; the others
#: only decide whether a few compiled bytes are fixtures or an extension. A
#: `Makefile` is not here: on sphinx it builds the docs, and it said "compiler"
#: on the record. https://github.com/wolfgang-aura/Mailman/issues/100
_COMPILED_MARKERS = ("Cargo.toml", "setup.py", "meson.build", "CMakeLists.txt")
_COMPILED_LANGUAGES = ("Cython", "Rust", "C", "C++", "Go", "Zig")
#: Cython or Rust bytes below this share of the Python are fixtures, not a
#: build, when nothing else names a compiler. sphinx carries 245 bytes of
#: Cython under 4.9 MB of Python, from a test of its own C-extension docs.
COMPILED_FIXTURE_SHARE = 0.005

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
    # docs.pretix.eu: "No AI-generated media is allowed (art, images, videos,
    # audio, etc.). Text and code are the only acceptable AI-generated
    # content". A ban on pictures is not a ban on the patch.
    r"no\s+ai[- ]generated(?!\s+(?:media|images?|art|artwork|videos?|audio|assets|graphics))"
    r"|ai[- ]generated\s+(?:code|pull requests?|prs?|contributions?)\s+"
    r"(?:are|will be)\s+(?:not\s+accepted|rejected|closed|banned)"
    r"|(?:do not|don't|please do not)\s+(?:use|submit)\s+(?:ai|llm|chatgpt|copilot)"
    # sunpy's pull request template: "Do not post the output from Large
    # Language Models or similar generative AI as code or comments". Mailman #153.
    r"|(?:do not|don't|please do not)\W+(?:post|submit|send|open)\s+(?:the\s+)?"
    r"output\s+(?:from|of)\s+(?:large\s+language\s+models?|llms?|generative\s+ai|ai\b|chatgpt)"
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
    # mne-tools/mne-python CONTRIBUTING.md: "You may not submit issues or pull
    # requests generated by fully-automated tools" and "It is not suitable for
    # automatic processing by AI tools". An agent hunting issues is both.
    # Mailman #158.
    r"|(?:may|must|should)\s+not\s+submit\b[^.]{0,80}"
    r"generated\s+by\s+(?:fully[- ])?automated\s+tools?"
    r"|not\s+suitable\s+for\s+automat(?:ic|ed)\s+processing\s+by\s+"
    r"(?:ai|llms?|agents?|bots?)"
    # mpmath's .github/CONTRIBUTING.rst: "Do not waste developers time by
    # submitting code that is fully or mostly generated by AI." Mailman #428.
    r"|(?:do\s+not|don't)\b[^.]{0,60}\bsubmit(?:ting)?\b[^.]{0,40}"
    r"(?:fully|mostly|entirely|primarily|wholly)\s+(?:or\s+\w+\s+)?"
    r"generated\s+by\s+(?:ai|llms?|generative\s+ai|artificial\s+intelligence)\b"
    # twisted's pull request template makes the contributor certify "I have
    # not directly included the output of any generative AI system in this
    # pull request", and its policy "does not allow the inclusion of the
    # outputs of generative AI tools". The gate passed both. Mailman #191.
    r"|have\s+not\s+(?:directly\s+)?(?:included|used|submitted)\s+"
    r"(?:the\s+)?outputs?\s+(?:of|from)\s+(?:any\s+)?"
    r"(?:generative\s+ai|ai|llms?|large\s+language\s+models?)"
    r"|(?:does|do)\s+not\s+allow\s+the\s+inclusion\s+of\s+(?:the\s+)?"
    r"outputs?\s+(?:of|from)\s+(?:any\s+)?(?:generative\s+ai|ai|llms?)"
    # streamlit CONTRIBUTING.md: "We have paused accepting pull requests from
    # outside the Streamlit maintainer team." Not an AI rule, but it refuses
    # every pull request this project could open. Mailman #238.
    r"|(?:paused|stopped|suspended|no\s+longer)\s+accepting\s+"
    r"(?:(?:external|outside|community|third[- ]party)\s+)?"
    r"(?:pull\s+requests?|prs?|code\s+contributions?|contributions?)"
    r"(?:\s+from\s+outside\b)?"
    # openai/openai-agents-python CONTRIBUTING.md: "Pull requests are limited
    # to repository collaborators. We do not accept pull requests from
    # non-collaborators". Mailman #418.
    r"|(?:pull\s+requests?|prs?|contributions?)\s+(?:are|is)\s+(?:limited|restricted)\s+"
    r"to\s+(?:repository\s+|project\s+)?(?:collaborators|maintainers|members)"
    r"|(?:do\s+not|don't|does\s+not|doesn't|cannot|can't)\s+accept\s+"
    r"(?:pull\s+requests?|prs?|code\s+contributions?|contributions?)\s+from\s+"
    r"(?:non[- ]?collaborators|non[- ]?members|outside(?:\s+contributors)?|"
    r"external\s+contributors|third[- ]part(?:y|ies)|the\s+community)"
    # SpikeInterface/spikeinterface AGENTS.md: "tell them this repo requires
    # human-authored contributions". Mailman #366.
    r"|requires?\s+human[- ](?:authored|written|made)\s+contributions?"
    r"|contributions?\s+must\s+be\s+human[- ](?:authored|written|made)"
    r")",
    re.IGNORECASE,
)

#: Files written to coding agents. There, an unconditional "do not open pull
#: requests" is addressed to this project and refuses it outright; in a human
#: guide the same words usually end "without an issue". spikeinterface's
#: AGENTS.md: "Do not open pull requests against this repository" and "You may
#: not: push branches, open PRs". Mailman #366.
_AGENT_FILES = ("AGENTS.md",)
_AGENT_FILE_BANS = re.compile(
    r"(?:"
    r"(?:do\s+not|don't|never|must\s+not|may\s+not)\s+"
    r"(?:open|create|submit|file|send|make)\s+(?:any\s+|a\s+|an\s+)?"
    r"(?:pull\s+requests|pull\s+request|prs|pr)\b"
    r"(?!\s+(?:without|unless|until|before|if|when|that|which|with)\b)"
    r"|you\s+may\s+not\W+[^.]{0,60}\bopen\s+(?:prs|pull\s+requests)\b"
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
    # optuna: "PRs from first-time contributors that appear to be primarily
    # generated by LLMs are generally not accepted unless...". Mailman #425.
    r"|(?:are|is)\s+(?:generally\s+|usually\s+|typically\s+)?not\s+"
    r"(?:reviewed|accepted|merged|considered)"
    r")",
    re.IGNORECASE,
)

#: What the refusal has to be about before it counts. "We will close stale PRs"
#: is every repository on GitHub; "we won't review AI-generated PRs" is this
#: project.
_AI_SUBJECT = re.compile(
    r"\b(?:ai|a\.i\.|llms?|agents?|agentic|copilot|chatgpt|generated|"
    # BeeWare refuses PRs "that result from running an autonomous tool" and
    # never says AI in that sentence. Mailman #422.
    r"machine[- ]written|autonomous(?:ly)?)\b",
    re.IGNORECASE,
)

#: A maintainer refusing AI work in a comment rather than a file.
#: davidhalter/jedi#2091: "I decided to not work at all with AI generated pull
#: requests/content"; jedi#2100: "don't use an LLM to answer me"; sunpy#8487:
#: "not compliant with our (recently updated) AI policy". Mailman #154.
_AI_CLOSING = re.compile(
    r"(?:"
    r"not\s+(?:to\s+)?(?:work|deal|engage)\s+(?:at\s+all\s+)?with\s+"
    r"(?:ai|llm)[- ]generated"
    r"|(?:do not|don't|please do not)\s+use\s+(?:an?\s+)?(?:ai|llms?|chatgpt)\b"
    r"|(?:not\s+compliant\s+with|violat\w*|against)\s+(?:our\s+)?"
    r"(?:\([^)]*\)\s+)?(?:\w+\s+){0,2}ai\s+policy"
    r")",
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
    # BeeWare: "we require you to **disclose the tools used**". Mailman #422.
    r"|disclose\s+the\s+tools?\b"
    r"|\b(?:ai|llm)\b.{0,40}must\s+be\s+disclosed"
    r"|declare\s+.{0,30}\bai\b"
    r"|state\s+.{0,30}\bai[- ]assisted"
    # mne-python: "If you used AI tools, state so in your PR description and
    # disclose the tools". The AI comes first. Mailman #158.
    r"|if\s+you\s+used\s+(?:ai|llms?|an?\s+llm)\b[^.]{0,60}"
    r"\b(?:state|disclose|declare|mention)"
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

#: A project that needs a signed Contributor License Agreement before a first
#: pull request merges. pretix/pretix's guide says so in its first lines and
#: nothing read it; cla-bot failed the check the minute #6564 was filed.
#: Signing is the operator's act, so it has to be asked before filing. See
#: https://github.com/wolfgang-aura/Mailman/issues/122.
REQUIRES_CLA = "cla"
_POLICY_CLA = re.compile(
    r"(?:"
    r"sign(?:ed|ing)?\s+[^.]{0,40}?"
    r"(?:contributor\s+license\s+agreement|\bcla\b)"
    r"|(?:contributor\s+license\s+agreement|\bcla\b)[^.]{0,80}"
    r"(?:must\s+be\s+signed|before\s+[^.]{0,40}(?:merge|accept))"
    r")",
    re.IGNORECASE,
)

_POLICY_PATHS = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    # mpmath keeps its AI policy in .github/CONTRIBUTING.rst, and the gate
    # read no guide at all. GitHub looks in the root, docs/ and .github/.
    # Mailman #428.
    ".github/CONTRIBUTING.rst",
    "docs/CONTRIBUTING.rst",
    # urllib3 keeps its guide here, lower case, and the gate read none of it.
    "docs/contributing.rst",
    # zarr keeps its "must be in your own words" rule here. Mailman #271.
    "docs/contributing.md",
    "AGENTS.md",
    # kornia keeps its AI rules in a root AI_POLICY.md that no guide links.
    # Mailman #421.
    "AI_POLICY.md",
    ".github/AI_POLICY.md",
)

#: Where a project keeps the text every pull request opens with. A ban written
#: there binds as surely as one in the guide: sunpy has no contributing guide in
#: the repository and says "Do not post the output from Large Language Models"
#: in its template, and the gate passed it. Mailman #153.
_TEMPLATE_PATHS = (
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
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


#: How a linked policy is read: `api` through `gh api` for a repository
#: file, `page` over https for a document the project keeps on its own site.
_API_DOCUMENT = "api"
_PAGE_DOCUMENT = "page"


def _linked_document(
    url: str, *, slug: str, guide: str
) -> tuple[str, str, str] | None:
    """How to read a link, where, and the name to record the document under.

    Four shapes, because a project writes the same pointer four ways: an
    absolute `github.com` blob URL into another repository, which is where an
    organization keeps its `.github/AI_POLICY.md`; the `raw` host; a path
    relative to the guide's own directory; and a page on the project's own
    documentation site, which is where pretix keeps its "AI-assisted
    contribution policy" and where the gate did not go until the run that
    read it by hand. Mailman #107.
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
        elif host:
            return _PAGE_DOCUMENT, link, f"{host}/{tail}".rstrip("/")
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
    return _API_DOCUMENT, api, source


class _PageText(HTMLParser):
    """The prose of an HTML page, with scripts, styles and navigation dropped."""

    _SKIPPED = ("script", "style", "nav", "header", "footer", "noscript")

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1
        elif tag in ("p", "li", "br", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def page_text(html: str) -> str:
    """What a reader sees of a page, for the same patterns the guide gets."""
    parser = _PageText()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


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
        kind, locator, source = resolved
        if source == guide or locator in seen:
            continue
        seen.add(locator)
        if len(read) + len(unread) >= _POLICY_LINK_LIMIT:
            break
        entry = {"source": source, "url": url.strip(), "link_text": text.strip()}
        if kind == _PAGE_DOCUMENT:
            document = page_text(gh.page(locator) or "")
        else:
            payload = gh.json(locator)
            inner = _directory_document(payload)
            if inner is not None:
                # A link to a folder: read the file inside it written for
                # agents. Mailman #419.
                path, _, query = locator.partition("?")
                locator = f"{path}/{inner}" + (f"?{query}" if query else "")
                if locator in seen:
                    continue
                seen.add(locator)
                source = f"{source}/{inner}"
                entry["source"] = source
                payload = gh.json(locator)
            document = _decoded(payload)
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


#: A closure for leaving out the AI disclosure enforces disclosure; it does
#: not refuse the work. Pyomo/pyomo#4026: "deleted a required part of the
#: template (AI disclosure), so this will be closed by policy". Mailman #391.
_DISCLOSURE_RULE = re.compile(r"\b(?:un)?disclos\w*", re.IGNORECASE)


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
        if _AI_SUBJECT.search(sentence) and not _DISCLOSURE_RULE.search(sentence):
            return sentence
    return None


#: The file a linked folder is read through, first match wins. huggingface/
#: diffusers links `.ai/`, whose rules for agents are in `.ai/AGENTS.md`.
_DIRECTORY_DOCUMENTS = ("AGENTS.md", "AI_POLICY.md", "README.md")


def _directory_document(payload: Any) -> str | None:
    """The policy file inside a contents listing, or None for a file."""
    if not isinstance(payload, list):
        return None
    names = {
        entry.get("name"): entry
        for entry in payload
        if isinstance(entry, dict) and entry.get("type") == "file"
    }
    return next((name for name in _DIRECTORY_DOCUMENTS if name in names), None)


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
    pattern_days = freshness.get("data", {}).get("pattern_days", PATTERN_DAYS)
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
        "long_standing_age_days": PROVENANCE_LONG_STANDING_AGE_DAYS,
        "long_standing_stars": PROVENANCE_LONG_STANDING_STARS,
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
        and isinstance(stars, int)
        and (
            (age_days >= PROVENANCE_MINIMUM_AGE_DAYS
             and stars >= PROVENANCE_MINIMUM_STARS)
            or (age_days >= PROVENANCE_LONG_STANDING_AGE_DAYS
                and stars >= PROVENANCE_LONG_STANDING_STARS)
        )
    )
    broad = authors >= PROVENANCE_MINIMUM_AUTHORS
    if established or broad:
        reason = (
            f"{authors} outside author(s) in {pattern_days} days"
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
            f"in {pattern_days} days. Too few people have read this code to run "
            "its build back end and its test suite on this machine."
        ),
        data=data,
    )


def _is_staff(row: dict[str, Any], slug: str, maintainers: Collection[str]) -> bool:
    """Whether a CONTRIBUTOR-labelled author has write access after all.

    GitHub says CONTRIBUTOR for staff whose org membership is private. Two
    things give them away: they merge other people's pull requests, or they
    push their branch to the repository itself rather than to a fork. #239.
    """
    login = str((row.get("user") or {}).get("login") or "").lower()
    if login and login in {name.lower() for name in maintainers}:
        return True
    head = row.get("head") if isinstance(row.get("head"), dict) else {}
    repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    return str(repo.get("full_name") or "").lower() == slug.lower()


def _freshness_gate(
    gh: _Gh, slug: str, window_days: int, maintainers: Collection[str] = ()
) -> dict[str, Any]:
    """Gate 1. Does outside work actually merge here, and by more than one person?"""
    path = f"repos/{slug}/pulls?state=closed&sort=updated&direction=desc"
    failed_before = len(gh.failures)
    closed = gh.pages(path, pages=6)
    # A page that failed to arrive or parse reads as an empty list, which the
    # gate below would report as "no merges". Say it was not read (#242).
    unread = any(
        label.startswith(path) for label in gh.failures[failed_before:]
    )
    now = datetime.now(UTC)
    window = (now - timedelta(days=window_days)).date().isoformat()
    # The pattern window is never shorter than the freshness window: a merge
    # 120 days old passed a 180-day window and then found no authors in 90
    # days. Mailman #317.
    pattern_days = max(PATTERN_DAYS, window_days)
    pattern = (now - timedelta(days=pattern_days)).date().isoformat()

    merged_human = [
        row for row in closed if row.get("merged_at") and is_outside_human(row)
    ]
    excluded_staff = sorted(
        {
            str((row.get("user") or {}).get("login"))
            for row in merged_human
            if _is_staff(row, slug, maintainers)
        },
        key=str.lower,
    )
    merged_outside = [
        row for row in merged_human if not _is_staff(row, slug, maintainers)
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
        "excluded_staff_authors": excluded_staff,
        "top_author": authors.most_common(1)[0][0] if authors else None,
        "top_author_share": share,
        "latest_outside_merge": latest,
        "pull_requests_scanned": len(closed),
        "window_days": window_days,
        "pattern_days": pattern_days,
        "closed_pulls_unread": unread,
    }
    if unread and not recent:
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"closed pull requests could not be read after {len(closed)} "
                "row(s); freshness is unknown, not failed"
            ),
            data=data,
        )
    if not recent:
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"no outside human merge in {window_days} days; latest is "
                f"{latest or 'none found'}"
                + (
                    f"; excluded staff {', '.join(excluded_staff)}"
                    if excluded_staff
                    else ""
                )
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
                f"merge in {pattern_days} days is by {data['top_author']}. One "
                "recurring collaborator is not an open door."
            ),
            data=data,
        )
    if (
        share is not None
        and share >= DOMINANT_AUTHOR_SHARE
        and distinct - 1 < DOMINANT_EXCEPTION_AUTHORS
    ):
        return _gate(
            "freshness",
            passed=False,
            blocking=True,
            detail=(
                f"{data['top_author']} wrote {share:.0%} of the {len(longer)} "
                f"outside merge(s) in {pattern_days} days. The other "
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
                f"outside merge(s) in {pattern_days} days. That is a recurring "
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
            f"author(s) in {pattern_days} days, top author {share:.0%}"
            + (f"; excluded {', '.join(excluded_bots)}" if excluded_bots else "")
            + (
                f"; excluded staff {', '.join(excluded_staff)}"
                if excluded_staff
                else ""
            )
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
    services: dict[str, list[str]] = {}
    for name in names:
        body = _decoded(gh.json(f"repos/{slug}/contents/.github/workflows/{name}"))
        if _TEST_RUNNER.search(body):
            running.append(name)
            if re.search(r"^\s*services:", body, re.MULTILINE):
                images = sorted(
                    {image.lower() for image in _SERVICE_IMAGE.findall(body)}
                )
                if images:
                    services[name] = images
    data = {
        "workflows_read": len(names),
        "workflows_running_tests": running,
        # The databases and brokers each test workflow starts. Mailman #255.
        "service_images": services,
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


def _pure_wheel(gh: _Gh, slug: str, pyproject: str) -> str | None:
    """The `py3-none-any` wheel of the project's latest release, if PyPI lists one."""
    name = ""
    try:
        project = tomllib.loads(pyproject).get("project") if pyproject else None
    except (tomllib.TOMLDecodeError, ValueError):
        project = None
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        name = project["name"]
    name = name or slug.split("/")[-1]
    for filename in _wheel_files(gh, name) or []:
        if filename.endswith("-none-any.whl") and "-py3" in filename:
            return filename
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
    python_bytes = languages.get("Python", 0) or 0
    compiled_bytes = compiled.get("Cython", 0) + compiled.get("Rust", 0)
    compiled_share = (compiled_bytes / python_bytes) if python_bytes else 0.0
    # A stray `.pyx` fixture is not a build. It counts as one only when a
    # marker or the build back end could turn it into an extension.
    fixture_only = (
        compiled_bytes > 0
        and compiled_share < COMPILED_FIXTURE_SHARE
        and not markers
        and not build_compilers
    )
    data = {
        "python_share": round(python_share, 3),
        "compiled_languages": compiled,
        "compiled_share": round(compiled_share, 3),
        "root_markers": markers,
        "languages": languages,
        "build_requires_compilers": build_compilers,
        "build_requires_line": requires_line,
        "wheel_only_hook": wheel_hook,
        "environment_plan": None,
        "pyproject": pyproject,
        "root_names": sorted(name for name in root_names if isinstance(name, str)),
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
    # falconry/falcon compiles Cython when it can and ships a pure wheel for
    # everyone else. A `py3-none-any` release says the extension is optional,
    # so the source tree runs without a compiler (#175). Rust never is.
    rust = "Cargo.toml" in markers or "Rust" in compiled
    needs_compiler = (
        ("Cython" in compiled and not fixture_only) or build_compilers
    ) and not wheel_hook
    if needs_compiler and not rust:
        pure_wheel = _pure_wheel(gh, slug, pyproject)
        if pure_wheel:
            data["environment_plan"] = "source-tree"
            data["pure_wheel"] = pure_wheel
            return _gate(
                "pure-python",
                passed=True,
                blocking=True,
                detail=(
                    f"Python is {python_share:.0%} of the source and the "
                    f"extension is optional: PyPI ships {pure_wheel}. This host "
                    "has no MSVC, so use the `source-tree` environment plan: "
                    "install the dependencies, put the workspace on the path, "
                    "never install the package."
                ),
                data=data,
            )
    if rust or (
        ("Cython" in compiled or "Rust" in compiled) and not fixture_only
    ):
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
    detail = f"Python is {python_share:.0%} of the source, no compiler markers"
    if fixture_only:
        detail = (
            f"Python is {python_share:.0%} of the source; the "
            f"{compiled_bytes:,} bytes of {', '.join(sorted(compiled))} are "
            f"fixtures ({compiled_share:.2%} of the Python, no compiler in the "
            "build back end)"
        )
    return _gate(
        "pure-python",
        passed=True,
        blocking=True,
        detail=detail,
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
    (REQUIRES_CLA, _POLICY_CLA),
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

#: Import names Windows Application Control blocks on this host at import
#: time, after pip has installed them without complaint: numba (pymc#8441,
#: 2026-09-17) and PyQt6 (electrum#10969, 2026-09-18, "DLL load failed while
#: importing QtWidgets: An Application Control policy has blocked this file").
#: Keyed by the distribution name as a requirement spells it, lower case.
#: See https://github.com/wolfgang-aura/Mailman/issues/121.
HOST_BLOCKED_PACKAGES: dict[str, str] = {
    "numba": "numba's DLLs are blocked by Application Control on this host",
    "pyqt6": "PyQt6's DLLs are blocked by Application Control on this host",
    "pyqt6-qt6": "PyQt6's DLLs are blocked by Application Control on this host",
    # ApeWorX/ape#2581, 2026-09-25: `import eth_account` loads ckzg at module
    # import ("DLL load failed while importing ckzg: An Application Control
    # policy has blocked this file"), and web3 and py-evm import it in turn,
    # so every web3 project is unrunnable here. The environment had built.
    "ckzg": "ckzg's DLL is blocked by Application Control on this host",
    "eth-account": "eth-account imports ckzg, whose DLL Application Control blocks on this host",
    "web3": "web3 imports eth-account and ckzg, whose DLL Application Control blocks on this host",
    "py-evm": "py-evm imports ckzg, whose DLL Application Control blocks on this host",
    # pyro-ppl/numpyro#2301, 2026-10-02: "DLL load failed while importing
    # _jax" under jaxlib 0.11.2 on 3.14, and "_sdy" under 0.10.2 on 3.12.
    # The environment had built. Mailman #373.
    "jaxlib": "jaxlib's DLLs are blocked by Application Control on this host",
    "jax": "jax imports jaxlib, whose DLLs Application Control blocks on this host",
}

#: Frameworks whose test suite needs a running service the host does not
#: have. frappe/erpnext passed every gate on 2026-09-18 and could not run one
#: test: a Frappe app is tested inside a bench, with MariaDB, Redis and a site.
HOST_UNRUNNABLE_FRAMEWORKS: dict[str, str] = {
    "frappe": "a Frappe app is tested inside a bench (MariaDB, Redis, a site)",
    # A universal wheel, so the wheel check passes it; ansible/ansible-lint
    # built an environment and then could not import it. Mailman #228.
    "ansible-core": "ansible-core imports fcntl and grp, which Windows does not have",
}

#: Packages that are a thin Python binding over a C library the host does not
#: ship. electrum_ecc installs from its sdist and then raises "Failed to load
#: libsecp256k1" at import (electrum#10969, 2026-09-16). Mailman #94.
NATIVE_LIBRARY_SHIMS: dict[str, str] = {
    "electrum-ecc": "electrum-ecc loads libsecp256k1, which this host does not have",
    "coincurve": "coincurve loads libsecp256k1, which this host does not have",
    "secp256k1": "secp256k1 loads libsecp256k1, which this host does not have",
}

#: Requirement files read beside the pyproject, relative to the repository
#: root, keyed by the root entry that must exist before the file is asked for.
REQUIREMENT_FILES = (
    ("requirements.txt", "requirements.txt"),
    ("contrib", "contrib/requirements/requirements.txt"),
)

#: At most this many required packages are looked up on PyPI per screen.
WHEEL_CHECK_LIMIT = 30

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PLATFORM_MARKER = re.compile(r"\b(sys_platform|platform_system|os_name)\b")
_WINDOWS_MARKER = re.compile(
    r"(sys_platform|platform_system|os_name)\s*==\s*['\"](win32|Windows|nt)['\"]"
)


def _requirement_name(requirement: str) -> str:
    """The distribution a requirement names, normalised the way pip does."""
    match = _REQUIREMENT_NAME.match(requirement)
    if not match:
        return ""
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _pyproject_requirements(pyproject: str) -> tuple[set[str], set[str], bool]:
    """Required names, optional names, and whether `[tool.bench]` is present."""
    try:
        table = tomllib.loads(pyproject)
    except (tomllib.TOMLDecodeError, ValueError):
        return set(), set(), False
    project = table.get("project") if isinstance(table.get("project"), dict) else {}
    required = {
        _requirement_name(item)
        for item in (project.get("dependencies") or [])
        if isinstance(item, str) and _installs_here(item)
    }
    optional: set[str] = set()
    extras = project.get("optional-dependencies")
    if isinstance(extras, dict):
        for items in extras.values():
            optional |= {
                _requirement_name(item) for item in (items or [])
                if isinstance(item, str) and _installs_here(item)
            }
    tool = table.get("tool") if isinstance(table.get("tool"), dict) else {}
    return required - {""}, optional - {""}, "bench" in tool


def _project_name(pyproject: str) -> str:
    """The normalized `[project] name`, or an empty string."""
    try:
        project = tomllib.loads(pyproject).get("project") if pyproject else None
    except (tomllib.TOMLDecodeError, ValueError):
        return ""
    name = project.get("name") if isinstance(project, dict) else None
    return _requirement_name(name) if isinstance(name, str) else ""


def _installs_here(requirement: str) -> bool:
    """False for a requirement whose platform marker excludes Windows. Mailman #382."""
    marker = requirement.split(";", 1)[1] if ";" in requirement else ""
    return not (_PLATFORM_MARKER.search(marker) and not _WINDOWS_MARKER.search(marker))


def _requirement_lines(text: str) -> set[str]:
    """Names a requirements file installs on Windows, options and comments skipped."""
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        if not _installs_here(line):
            continue
        names.add(_requirement_name(line))
    return names - {""}


def _runtime_requirements(gh: _Gh, slug: str, root_names: set[str]) -> set[str]:
    """Names the repository's requirement files pin, beside its pyproject."""
    names: set[str] = set()
    for marker, path in REQUIREMENT_FILES:
        if marker in root_names:
            names |= _requirement_lines(_decoded(gh.json(f"repos/{slug}/contents/{path}")))
    return names


def _wheel_files(gh: _Gh, name: str) -> list[str] | None:
    """The files PyPI lists for the latest release, or None when unread."""
    name = re.sub(r"[-_.]+", "-", name).lower()
    body = gh.page(f"https://pypi.org/pypi/{quote(name)}/json")
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        # sqlalchemy's document lists every release and runs past the page
        # cap (#175). Its `info` block comes first and names the latest
        # version, whose own document carries only that release's files.
        version = re.search(r'"version"\s*:\s*"([^"]+)"', body)
        if not version:
            return None
        body = gh.page(
            f"https://pypi.org/pypi/{quote(name)}/{quote(version.group(1))}/json"
        )
        try:
            payload = json.loads(body) if body is not None else None
        except ValueError:
            return None
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        return None
    return [
        str(entry["filename"])
        for entry in urls
        if isinstance(entry, dict) and entry.get("filename")
    ]


def _wheel_fits_host(filename: str) -> bool:
    """Whether pip on this Windows host and Python could install this wheel."""
    if not filename.endswith(".whl"):
        return False
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return False
    python_tags = set(parts[-3].split("."))
    abi_tags = set(parts[-2].split("."))
    platforms = set(parts[-1].split("."))
    current = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if "any" in platforms:
        return bool(python_tags & {"py3", current, f"py{sys.version_info.major}{sys.version_info.minor}"})
    if "win_amd64" not in platforms:
        return False
    if current in python_tags:
        return True
    if "abi3" in abi_tags:
        return any(
            tag.startswith("cp3") and tag[3:].isdigit()
            and int(tag[3:]) <= sys.version_info.minor
            for tag in python_tags
        )
    return "py3" in python_tags and "none" in abi_tags


def _host_gate(
    pyproject: str,
    *,
    requirements: set[str] | frozenset[str] = frozenset(),
    wheel_files: Callable[[str], list[str] | None] | None = None,
    ci: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate 3b. Can the target's tests run on this host at all?

    Two of the five repositories that passed the screen on 2026-09-18 could
    not be reproduced here, after the pre-screen and, for one, a full
    environment build. The screen knew the dependency list and never read it
    against what the host is known to refuse.

    `requirements` adds what the repository's requirement files pin, and
    `wheel_files` looks a package up on PyPI. A required package with no
    wheel this host can install fails, because the environment step installs
    binaries only; a package PyPI did not answer for is recorded as unchecked.

    `ci` is the CI gate's data. When every workflow that runs tests starts a
    database or broker service, the suite needs a server this host does not
    run. Mailman #255.
    """
    required, optional, bench = _pyproject_requirements(pyproject)
    required = required | set(requirements)
    # The framework's own repository needs what it is, not what it lists.
    own_name = _project_name(pyproject)
    if own_name in HOST_UNRUNNABLE_FRAMEWORKS:
        required.add(own_name)
    optional = optional - required
    shims = sorted(name for name in NATIVE_LIBRARY_SHIMS if name in required)
    no_wheel: list[dict[str, Any]] = []
    unchecked: list[str] = []
    if wheel_files is not None:
        for name in sorted(required - set(shims))[:WHEEL_CHECK_LIMIT]:
            files = wheel_files(name)
            if files is None:
                unchecked.append(name)
            elif not any(_wheel_fits_host(filename) for filename in files):
                no_wheel.append({"package": name, "files": files})
    frameworks = sorted(
        name for name in HOST_UNRUNNABLE_FRAMEWORKS if name in required or (bench and name == "frappe")
    )
    blocked = sorted(name for name in HOST_BLOCKED_PACKAGES if name in required)
    optional_blocked = sorted(
        name for name in HOST_BLOCKED_PACKAGES if name in optional and name not in required
    )
    data = {
        "required_unrunnable": frameworks,
        "required_blocked": blocked,
        "optional_blocked": optional_blocked,
        "native_shims": shims,
        "no_wheel": no_wheel,
        "pypi_unchecked": unchecked,
    }
    reasons = [HOST_UNRUNNABLE_FRAMEWORKS[name] for name in frameworks]
    reasons += [HOST_BLOCKED_PACKAGES[name] for name in blocked]
    reasons += [NATIVE_LIBRARY_SHIMS[name] for name in shims]
    reasons += [
        f"{row['package']} has no wheel for this platform and Python on PyPI"
        for row in no_wheel
    ]
    running = (ci or {}).get("workflows_running_tests") or []
    services = (ci or {}).get("service_images") or {}
    if running and all(name in services for name in running):
        needed = sorted({image for name in running for image in services[name]})
        data["services_needed"] = needed
        reasons.append(
            "every test workflow starts a "
            + ", ".join(needed)
            + " service, and this host runs none"
        )
    if reasons:
        return _gate(
            "host",
            passed=False,
            blocking=True,
            detail="; ".join(dict.fromkeys(reasons)),
            data=data,
        )
    if optional_blocked:
        return _gate(
            "host",
            passed=False,
            blocking=False,
            detail=(
                "an extra needs "
                + ", ".join(optional_blocked)
                + ", which Application Control blocks here; an issue in that "
                "part of the code cannot be reproduced on this host"
            ),
            data=data,
        )
    return _gate(
        "host",
        passed=True,
        blocking=False,
        detail="no dependency this host is known to refuse",
        data=data,
    )


#: The branch a guide or template tells contributors to open pull requests
#: against. stanza: "create a pull request **against the `dev` branch**", and
#: its template adds "We cannot accept pull requests against the `main`
#: branch", which the negation check drops. Mailman #258.
_PULL_REQUEST_BASE = re.compile(
    r"(?:pull[- ]requests?|\bPRs?)\b[^.\n]{0,40}?\b(?:against|into|to|targeting)"
    r"\s+(?:the\s+)?[*`'\"]*([A-Za-z0-9][\w./-]*?)[*`'\"]*\s+branch\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"\b(?:cannot|can't|not|never|don't|do not|won't)\b", re.IGNORECASE
)
#: Python-Markdown: "an outstanding pull request, pushing new commits to the
#: related branch" names the contributor's own branch, not a base, and
#: "related" is no branch name. Mailman #379.
_PUSHED = re.compile(r"\b(?:push\w*|commit\w*)\b", re.IGNORECASE)
_NOT_A_BRANCH_NAME = frozenset(
    {
        "related", "relevant", "same", "appropriate", "correct", "right",
        "proper", "corresponding", "your", "own", "feature", "topic", "new",
        "this", "that", "a", "an", "their", "my", "upstream", "target",
    }
)


def _pull_request_base(texts: Sequence[str]) -> dict[str, Any] | None:
    """The first branch a document says pull requests go to, with its quote."""
    for text in texts:
        flat = " ".join(text.split())
        for match in _PULL_REQUEST_BASE.finditer(flat):
            if _NEGATED.search(flat[max(0, match.start() - 30) : match.end()]):
                continue
            if _PUSHED.search(match.group(0)):
                continue
            if match.group(1).lower() in _NOT_A_BRANCH_NAME:
                continue
            return {
                "branch": match.group(1),
                "quote": _sentence(flat, match.start(), match.end()),
            }
    return None


def _policy_gate(gh: _Gh, slug: str) -> dict[str, Any]:
    """Gate 4. Does the guide, the policy it links, or the pull request template close an AI-assisted pull request?"""
    template = ""
    template_source = ""
    for relative in _TEMPLATE_PATHS:
        body = _decoded(gh.json(f"repos/{slug}/contents/{relative}"))
        if not body:
            continue
        template = body
        template_source = relative
        ban = _POLICY_BANS.search(" ".join(body.split()))
        if ban:
            return _gate(
                "policy",
                passed=False,
                blocking=True,
                detail=f"{relative} refuses AI-assisted work: {ban.group(0)!r}",
                data={
                    "source": relative,
                    "guide": relative,
                    "result": "refused",
                    "quote": ban.group(0),
                },
            )
        break
    guides = {
        relative: _decoded(gh.json(f"repos/{slug}/contents/{relative}"))
        for relative in _POLICY_PATHS
    }
    # Every guide is read for a ban, not only the first: a CONTRIBUTING.md
    # used to hide the AGENTS.md behind it. Mailman #366.
    for relative, body in guides.items():
        if not body:
            continue
        flat = " ".join(body.split())
        ban = _POLICY_BANS.search(flat)
        if not ban and relative in _AGENT_FILES:
            ban = _AGENT_FILE_BANS.search(flat)
        if ban:
            return _gate(
                "policy",
                passed=False,
                blocking=True,
                detail=f"{relative} refuses AI-assisted work: {ban.group(0)!r}",
                data={
                    "source": relative,
                    "guide": relative,
                    "result": "refused",
                    "quote": ban.group(0),
                },
            )
    for relative in _POLICY_PATHS:
        body = guides[relative]
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
            if not ban and posixpath.basename(document["source"]) in _AGENT_FILES:
                ban = _AGENT_FILE_BANS.search(flat)
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
        # The template binds every pull request as the guide does: zarr says
        # "must be in your own words" there behind a one-line guide. #271.
        if template:
            _constraints(template_source, " ".join(template.split()), constraints)
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
            if REQUIRES_CLA in kinds:
                detail += (
                    " A signed CLA is needed before a first pull request "
                    "merges; the operator signs it before filing."
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
                "requires_cla": REQUIRES_CLA in kinds,
                "constraints": constraints,
                "quote": _quoted(constraints, "disclosure"),
                "pull_request_base": _pull_request_base(
                    [entry["body"] for entry in documents] + [template]
                ),
                **trail,
            },
        )
    constraints = []
    if template:
        _constraints(template_source, " ".join(template.split()), constraints)
    kinds = {entry["kind"] for entry in constraints}
    detail = "no contributing guide found, so nothing forbids the work in writing"
    if constraints:
        detail += "; the pull request template constrains the submission - " + "; ".join(
            f"{entry['kind']}: {entry['quote']!r}" for entry in constraints
        )
    return _gate(
        "policy",
        passed=True,
        blocking=False,
        detail=detail,
        data={
            "source": None,
            "result": "no-guide",
            "requires_disclosure": "disclosure" in kinds,
            "requires_own_words": "own-words" in kinds,
            "requires_human_account": "human-account" in kinds,
            "requires_prior_discussion": PRIOR_DISCUSSION in kinds,
            "forbids_duplicate_pull_requests": NO_DUPLICATE_PULL_REQUESTS in kinds,
            "requires_cla": REQUIRES_CLA in kinds,
            "constraints": constraints,
            "pull_request_base": _pull_request_base([template]),
        },
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


#: Words that mark an issue as something other than a defect, wherever they
#: sit in a label: `enhancement`, `Issue Type: Feature Request`, `kind/feature`,
#: `type:documentation`. A tracker can carry forty unclaimed requests,
#: questions and announcements and still have no work a bug run could take;
#: celery, moto and haystack shortlists led with them (#184).
_NOT_A_BUG_WORDS = frozenset(
    {
        "enhancement",
        "enhancements",
        "feature",
        "features",
        "question",
        "questions",
        "announcement",
        "documentation",
        "docs",
        "epic",
        "discussion",
        "proposal",
    }
)

#: Words that reserve an issue for the project's own staff: zenml labels its
#: roadmap `core-team`, or `planned` with `gtm-team`, and an outside pull
#: request against one is not taken (#264). Whole words only, so `teamwork`
#: stays a bug.
_STAFF_WORDS = frozenset({"team", "planned", "roadmap", "internal"})

#: Reading one issue's comments costs one API call, so the reads stop after
#: this many unclaimed candidates. A tracker with more unclaimed issues than
#: this is not saturated in any sense this gate needs to measure precisely;
#: the cap is recorded rather than hidden.
_COMMENT_THREAD_LIMIT = 40


#: A title that tags itself as something other than a defect. Only the tag
#: forms count, so "Feature X crashes" stays a bug: haystack's "RFC: Retrieval
#: Diagnostics API", peft's "[RFC, I can do a PR] Make lora_alpha a float",
#: marimo's "Track ty readiness" and scapy's "2.8.0 release" carried no such
#: label and led `hunt targets` on 2026-09-29. Mailman #188.
_REQUEST_TITLE = re.compile(
    r"^\s*(?:"
    r"\[\s*(?:rfc|proposal|feature(?:[ -]request)?|question|discussion|psa|epic"
    r"|enhancement|idea)\b[^\]]*\]"
    r"|(?:rfc|proposal|feature(?:[ -]request)?|question|discussion|psa|epic"
    r"|enhancement|idea)\s*:"
    r"|track(?:ing)?\s"
    r"|.*\bv?\d+\.\d+(?:\.\d+)*\b.*\breleases?\s*$"
    r")",
    re.IGNORECASE,
)


def is_request_row(row: dict[str, Any]) -> bool:
    """Whether a shortlist row is a request, question, tracker or docs item.

    Stored screens are read with it too, so a screen recorded before a word
    was added here still stops offering the row. Mailman #188.
    """
    return _is_enhancement(row) or bool(
        _REQUEST_TITLE.match(str(row.get("title") or ""))
    )


def _is_enhancement(row: dict[str, Any]) -> bool:
    """Whether a label says the issue is a request, question, docs or staff item."""
    labels = row.get("labels")
    if not isinstance(labels, list):
        return False
    for entry in labels:
        name = str(entry.get("name") or "") if isinstance(entry, dict) else str(entry)
        words = re.split(r"[^a-z]+", name.lower())
        if "enhancement" in name.lower() or _NOT_A_BUG_WORDS.intersection(words):
            return True
        if _STAFF_WORDS.intersection(words):
            return True
    return False


def _age_in_days(row: dict[str, Any], now: datetime) -> int | None:
    created = str(row.get("created_at") or "")[:10]
    if not created:
        return None
    try:
        return (now - datetime.fromisoformat(created).replace(tzinfo=UTC)).days
    except ValueError:
        return None


#: Comment pages read per thread. A claim can sit past comment 100 (#344); a
#: thread past 1,000 reads as unread rather than as one with nothing more.
_THREAD_PAGES = 10


def _read_thread(
    gh: _Gh, slug: str, number: str, maintainers: Collection[str] = ()
) -> dict[str, Any] | None:
    """Read one issue's thread for a claim, and for what ranks it.

    The claim is the form GitHub itself does not track: a comment saying
    "I'm on it" that no maintainer has answered. `mailman claims` reads the
    same thread for a single run; saturation applies the same judgement, so
    the two gates cannot disagree about what a claim is. The same page also
    says whether a maintainer asked for the pull request, when a maintainer
    last wrote, and which pull requests the thread cites, and the shortlist
    ranks on those. One read answers all four.

    None when the thread could not all be read: an unread thread is not an
    empty one, and the row then carries `thread_read` False (#344).
    """
    comments = gh.every_page(
        f"repos/{slug}/issues/{number}/comments", pages=_THREAD_PAGES
    )
    if comments is None:
        return None
    return {
        "comments": comments,
        "claimed": any(
            kind in {"claim", "assignment"}
            for kind in classify_thread(comments, maintainers=maintainers)
        ),
        "maintainer_touched_at": maintainer_touched_at(
            comments, maintainers=maintainers
        ),
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
    maintainers: Collection[str] = (),
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
        thread_read=thread is not None,
        maintainers=maintainers,
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
        # Who has spoken, so a coordinator does not refetch every thread to
        # find the triaged rows. None when the thread was past the read cap.
        # https://github.com/wolfgang-aura/Mailman/issues/135
        "maintainer_filed": is_maintainer(row, maintainers),
        "maintainer_replied": (
            any(
                isinstance(comment, dict) and is_maintainer(comment, maintainers)
                for comment in thread.get("comments") or []
            )
            if thread is not None
            else None
        ),
        # A reply that says "cannot reproduce" is not triage. Mailman #150.
        "maintainer_disputed": (
            maintainer_dispute(thread.get("comments") or [], maintainers=maintainers)
            if thread is not None
            else None
        ),
        "score": ranked["score"],
        "reasons": ranked["reasons"],
    }


#: Timeline pages read per shortlist row. A rival cross-reference can sit
#: past event 100 on a long thread (#339); one past 1,000 reads as unknown.
_TIMELINE_PAGES = 10


def _read_timelines(
    gh: _Gh, slug: str, shortlist: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> None:
    """Add label triage and rival pull requests to each shortlist row, in place.

    A maintainer's label is triage in another form (#139), and `claims`
    already counts it for a run; the screen counted only comments, so
    `hunt targets --engaged-only` hid plotly/dash's P1-P3 bugs, which are
    triaged by label alone. The same timeline page carries GitHub's own
    cross-references, which is where a rival pull request usually shows.
    One read per row, up to the thread cap; a row past it stays None.
    """
    reporters = {
        str(row["number"]): (row.get("user") or {}).get("login")
        for row in issues
        if isinstance(row.get("user"), dict)
    }
    rows = [row for row in shortlist if row.get("thread_read")][:_COMMENT_THREAD_LIMIT]

    def read(row: dict[str, Any]) -> list[Any] | None:
        return gh.every_page(
            f"repos/{slug}/issues/{row['number']}/timeline", pages=_TIMELINE_PAGES
        )

    with ThreadPoolExecutor(max_workers=RESPONSIVENESS_WORKERS) as pool:
        timelines = list(pool.map(read, rows))
    for row in shortlist:
        row.setdefault("maintainer_labelled", None)
        row.setdefault("rival_pull_requests", None)
    for row, timeline in zip(rows, timelines):
        # A timeline that could not be read is no answer about rivals, and
        # `hunt targets` does not offer the row. Mailman #339.
        row["timeline_read"] = timeline is not None
        if timeline is None:
            continue
        row["maintainer_labelled"] = bool(
            maintainer_labels(timeline, reporter=reporters.get(str(row["number"])))
        )
        row["rival_pull_requests"] = rival_pull_requests(timeline)


def _saturation_gate(
    gh: _Gh,
    slug: str,
    window_days: int,
    issue_window_days: int,
    maintainers: Collection[str] = (),
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
    threads_unread = 0
    for row in candidates[:_COMMENT_THREAD_LIMIT]:
        number = str(row["number"])
        thread = _read_thread(gh, slug, number, maintainers)
        if thread is None:
            threads_unread += 1
            continue
        threads[number] = thread
        if thread["claimed"]:
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
        if is_request_row(row):
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
                maintainers=maintainers,
            )
        )
    _read_timelines(gh, slug, shortlist, unclaimed)
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
        "comment_threads_unread": threads_unread,
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
                f"none is workable: {enhancement_labelled} labelled as a request, "
                f"question or docs item, "
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


def _is_maintainer(row: dict[str, Any], maintainers: Collection[str] = ()) -> bool:
    return is_maintainer(row, maintainers) and not _is_bot(row.get("user"))


def _first_maintainer_response(
    gh: _Gh,
    slug: str,
    number: int,
    opened: datetime,
    maintainers: Collection[str] = (),
) -> tuple[float | None, str | None]:
    """Days from a pull request opening to the first word from a maintainer.

    A review, an inline review comment and an issue comment are three
    different endpoints, and a maintainer's first word can be any of them.
    Whichever came first counts. The author's own comments are never a
    response, and neither is a bot's.

    The same rows are read for a maintainer refusing AI work, at no extra
    cost: a refusal written only in a closing comment is still the policy.
    Mailman #154.
    """
    stamps: list[datetime] = []
    refusal: str | None = None
    for path, field in (
        (f"repos/{slug}/pulls/{number}/reviews", "submitted_at"),
        (f"repos/{slug}/pulls/{number}/comments", "created_at"),
        (f"repos/{slug}/issues/{number}/comments", "created_at"),
    ):
        for row in gh.pages(path, pages=1):
            if not isinstance(row, dict) or not _is_maintainer(row, maintainers):
                continue
            stamp = _timestamp(row.get(field))
            if stamp is not None and stamp >= opened:
                stamps.append(stamp)
            if refusal is None:
                refusal = _ai_refusal(str(row.get("body") or ""))
    if not stamps:
        return None, refusal
    return (min(stamps) - opened).total_seconds() / 86400, refusal


def _ai_refusal(body: str) -> str | None:
    """The sentence of a maintainer comment that refuses AI work, if any."""
    flat = " ".join(body.split())
    match = _AI_CLOSING.search(flat) or _POLICY_BANS.search(flat)
    if match:
        return _sentence(flat, match.start(), match.end())
    return _outcome_refusal(flat)


def _responsiveness_gate(
    gh: _Gh, slug: str, responsiveness_days: int, maintainers: Collection[str] = ()
) -> dict[str, Any]:
    """Gate 8. How long does a stranger's pull request wait for a first word?

    Freshness is satisfied by a collaborator's merge and says nothing about a
    stranger's silence. This gate reads the outside pull requests opened in
    the window and asks three things of them: how long the median one waited
    for a maintainer to review or comment, what share got any response inside
    `FIRST_RESPONSE_DAYS`, and whether under `REJECTION_MERGE_SHARE` of the decided ones merged.
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
    still_in_window = 0
    merged = 0
    closed_unmerged = 0
    # Three reads per pull request, 150 in all, were two thirds of a screen's
    # wall time run one after another. Four at a time stays under GitHub's
    # burst limit, which `_Gh.json` retries anyway.
    with ThreadPoolExecutor(max_workers=RESPONSIVENESS_WORKERS) as pool:
        first_waits = list(pool.map(
            lambda item: _first_maintainer_response(
                gh, slug, int(item[0].get("number") or 0), item[1], maintainers
            ),
            sample,
        ))
    refusals: list[dict[str, Any]] = []
    for (row, opened), (wait, refusal) in zip(sample, first_waits):
        if refusal and not row.get("merged_at"):
            refusals.append(
                {
                    "number": row.get("number"),
                    "url": row.get("html_url")
                    or f"https://github.com/{slug}/pull/{row.get('number')}",
                    "quote": refusal,
                }
            )
        merged_at = _timestamp(row.get("merged_at"))
        if merged_at is not None:
            # A merge is a maintainer's answer even when nobody wrote a word;
            # fsspec merges most outside work silently and read as unanswered.
            merge_wait = max((merged_at - opened).total_seconds() / 86400, 0.0)
            wait = merge_wait if wait is None else min(wait, merge_wait)
        if wait is None:
            age = (now - opened).total_seconds() / 86400
            waits.append(age)
            if age < FIRST_RESPONSE_DAYS:
                # Its window has not closed; silence so far is not a miss (#224).
                still_in_window += 1
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
    judged = sampled - still_in_window
    share = round(responded_within / judged, 2) if judged else None
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
        "still_in_window": still_in_window,
        "response_share": share,
        "median_first_response_days": median,
        "merged": merged,
        "closed_unmerged": closed_unmerged,
        "still_open": sampled - decided,
        "excluded_bot_authors": sorted(excluded_bots),
        "ai_refusals": refusals,
        "first_response_days": FIRST_RESPONSE_DAYS,
        "first_response_share": FIRST_RESPONSE_SHARE,
        "sample_minimum": RESPONSIVENESS_SAMPLE_MINIMUM,
        "decided_minimum": REJECTION_DECIDED_MINIMUM,
        "merge_share_minimum": REJECTION_MERGE_SHARE,
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
        f"{responded_within} of {judged} answered within {FIRST_RESPONSE_DAYS} "
        f"days ({'none judged yet' if share is None else format(share, '.0%')}), "
        f"{merged} merged, {closed_unmerged} closed unmerged"
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
    if decided >= REJECTION_DECIDED_MINIMUM and merged / decided < REJECTION_MERGE_SHARE:
        reasons.append(
            f"under {REJECTION_MERGE_SHARE:.0%} of decided outside pull requests merged"
        )
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


def pull_request_base(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The branch pull requests must target, when it is not the default one.

    `None` when the guide names none, names the default branch, or the screen
    predates the field. Mailman #258.
    """
    if not isinstance(record, dict):
        return None
    base = None
    default = None
    for gate in record.get("gates") or []:
        if not isinstance(gate, dict):
            continue
        data = gate.get("data") or {}
        if gate.get("name") == "policy":
            base = data.get("pull_request_base")
        default = default or data.get("default_branch")
    if not isinstance(base, dict) or not base.get("branch"):
        return None
    if default and str(base["branch"]).lower() == str(default).lower():
        return None
    return {**base, "default_branch": default}


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


def screen_is_current(
    record: dict[str, Any],
    *,
    window_days: int = FRESHNESS_WINDOW_DAYS,
    issue_window_days: int = ISSUE_WINDOW_DAYS,
    responsiveness_days: int = RESPONSIVENESS_WINDOW_DAYS,
) -> bool:
    """Whether a recorded verdict was read with the windows now asked for.

    A verdict is an answer to a question with its windows in it. When the
    defaults widened on 2026-09-25, 294 cached screens still carried the old
    answer, and reusing them would have kept every repository the change was
    for on the reject pile.
    """
    return (
        record.get("window_days") == window_days
        and record.get("issue_window_days") == issue_window_days
        and record.get("responsiveness_days") == responsiveness_days
    )


#: Gates that read no time window: a refusal on one stands whatever the
#: windows were. python/mypy's policy refusal was waved through once the
#: windows moved. Mailman #387.
WINDOW_INDEPENDENT_GATES = frozenset({"policy", "pure-python", "host", "archived"})


def refusal_stands(record: dict[str, Any]) -> bool:
    """Whether a failed screen still refuses: read with today's windows, or
    failed on a gate no window affects."""
    return screen_is_current(record) or bool(
        WINDOW_INDEPENDENT_GATES & set(record.get("failed_gates") or [])
    )


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
    window_days: int = FRESHNESS_WINDOW_DAYS,
    issue_window_days: int = ISSUE_WINDOW_DAYS,
    responsiveness_days: int = RESPONSIVENESS_WINDOW_DAYS,
    executable: str | None = None,
    timeout_seconds: float = 120,
    working_directory: Path | None = None,
    _execute: Callable[..., CommandResult] = execute,
    _fetch: Callable[[str, float], CommandResult] = fetch_page,
) -> dict[str, Any]:
    """Run every repository-level gate and write the verdict where it is reusable."""
    slug = repository_slug(repository)
    data_root.mkdir(parents=True, exist_ok=True)
    home = working_directory or data_root
    gh = _Gh(
        executable or resolve_tool(home, "gh"), home, timeout_seconds, _execute, _fetch
    )
    record: dict[str, Any] = {
        "schema_version": SCREEN_SCHEMA_VERSION,
        "repository": slug,
        "screened_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "issue_window_days": issue_window_days,
        "responsiveness_days": responsiveness_days,
        "responsiveness_rules": RESPONSIVENESS_RULES_VERSION,
        "gates": [],
        "success": False,
        # Who merged recent pull requests: maintainers GitHub may report as
        # CONTRIBUTOR because their membership is private. Read only for a
        # repository no cheap gate refused. Mailman #203.
        "maintainer_logins": [],
        "maintainer_logins_read": False,
    }

    meta = gh.json(f"repos/{slug}")
    if not isinstance(meta, dict) or "full_name" not in meta:
        return _unread(data_root, slug, record, gh, f"{slug} could not be read")
    # `repos/OLD/NAME` follows a rename; issue search answers 422 for the old
    # name, which the assignment gate read as "unavailable". Mailman #301.
    current = repository_slug(str(meta["full_name"]))
    if current.lower() != slug.lower():
        record["renamed_from"] = slug
        record["repository"] = slug = current
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

    # One GraphQL call, read first: freshness needs it to tell private-member
    # staff from outsiders (#239), and every later "is this a maintainer?"
    # reads it too.
    owner, _, name = slug.partition("/")
    answer = gh.graphql(MERGERS_QUERY % (owner, name))
    maintainers = mergers(answer)
    record["maintainer_logins"] = maintainers
    record["maintainer_logins_read"] = answer is not None
    freshness = _freshness_gate(gh, slug, window_days, maintainers)
    python = _python_gate(gh, slug)
    ci = _ci_gate(gh, slug)
    gates = [
        _provenance_gate(meta, freshness),
        freshness,
        ci,
        python,
        _host_gate(
            python["data"].pop("pyproject", ""),
            requirements=_runtime_requirements(
                gh, slug, set(python["data"].pop("root_names", []))
            ),
            wheel_files=lambda name: _wheel_files(gh, name),
            ci=ci["data"],
        ),
        _policy_gate(gh, slug),
        _assignment_gate(gh, slug),
    ]
    stars = _stars_gate(meta)
    # The last three gates cost about 150 of a screen's ~220 reads and
    # cannot turn a fail into a pass, so a repository an earlier gate
    # already refused keeps them as unread placeholders.
    # https://github.com/wolfgang-aura/Mailman/issues/170
    refused = [
        gate["name"]
        for gate in [*gates, stars]
        if gate["blocking"] and not gate["passed"]
    ]
    if refused:
        gates += [_skipped_gate(name, refused) for name in EXPENSIVE_GATES]
    else:
        gates += [
            _saturation_gate(
                gh, slug, window_days, issue_window_days, maintainers
            ),
            _direct_push_gate(gh, slug, meta),
            _responsiveness_gate(gh, slug, responsiveness_days, maintainers),
        ]
        _policy_from_comments(gates)
        _policy_from_proposals(gates, gh, slug, maintainers)
    gates.append(stars)
    if gh.rate_limited:
        # A gate whose reads were refused reports a verdict it never saw:
        # on 2026-09-29 pypa/hatch "failed" ci, host, saturation and
        # responsiveness in the minutes the hourly budget was spent.
        # https://github.com/wolfgang-aura/Mailman/issues/169
        return _unread(data_root, slug, record, gh, RATE_LIMITED_DETAIL)
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


def _policy_from_comments(gates: list[dict[str, Any]]) -> None:
    """Fail the policy gate on a maintainer's AI refusal in a pull request.

    davidhalter/jedi passed the policy gate with no file saying anything,
    while its maintainer closed AI pull requests with "I decided to not work
    at all with AI generated pull requests/content". Mailman #154.
    """
    responsiveness = next(
        (gate for gate in gates if gate["name"] == "responsiveness"), None
    )
    refusals = (responsiveness or {}).get("data", {}).get("ai_refusals") or []
    if not refusals:
        return
    first = refusals[0]
    for index, gate in enumerate(gates):
        if gate["name"] == "policy" and gate["passed"]:
            gates[index] = _gate(
                "policy",
                passed=False,
                blocking=True,
                detail=(
                    f"a maintainer refused AI-assisted work on {first['url']}: "
                    f"{first['quote']!r}"
                ),
                data={
                    **gate.get("data", {}),
                    "source": first["url"],
                    "result": "refused",
                    "quote": first["quote"],
                    "comment_refusals": refusals,
                },
            )


#: A title that proposes an AI policy, and one that proposes refusing AI work.
_AI_POLICY_TITLE = re.compile(
    r"\b(?:ai|a\.i\.|llms?|genai|generative|agents?|agentic|copilot)\b"
    r"[^.]{0,40}\bpolic(?:y|ies)\b",
    re.IGNORECASE,
)
_REFUSING_TITLE = re.compile(
    r"\bno[- ](?:ai|llms?|genai)\b|\b(?:ban|bans|banning|prohibit\w*|"
    r"forbid\w*|reject\w*|refus\w*|disallow\w*)\b",
    re.IGNORECASE,
)


def _policy_from_proposals(
    gates: list[dict[str, Any]],
    gh: _Gh,
    slug: str,
    maintainers: Collection[str] = (),
) -> None:
    """Read open issues and pull requests that propose an AI policy.

    biopython/biopython#5241, "First draft of no-AI policy.", had been open
    since June by a maintainer. No file said anything yet, so the gate passed,
    and the pull request filed there was put on hold the day it arrived. A
    maintainer's proposal to refuse AI work fails the gate; a neutral one is
    named in the detail. Mailman #220.
    """
    index = next(
        (
            position
            for position, gate in enumerate(gates)
            if gate["name"] == "policy" and gate["passed"]
        ),
        None,
    )
    if index is None:
        return
    query = quote(f"repo:{slug} is:open in:title policy")
    result = gh.json(f"search/issues?q={query}&per_page=30")
    items = result.get("items") if isinstance(result, dict) else None
    proposals = []
    for row in items if isinstance(items, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        login = str((row.get("user") or {}).get("login") or "")
        if not _AI_POLICY_TITLE.search(title):
            continue
        if not is_maintainer(row, maintainers):
            continue
        proposals.append(
            {
                "number": row.get("number"),
                "title": title,
                "url": row.get("html_url"),
                "author": login,
                "refusing": bool(_REFUSING_TITLE.search(title)),
            }
        )
    if not proposals:
        return
    gate = gates[index]
    refusing = next((row for row in proposals if row["refusing"]), None)
    if refusing:
        gates[index] = _gate(
            "policy",
            passed=False,
            blocking=True,
            detail=(
                f"a maintainer's open proposal would refuse AI-assisted work: "
                f"{refusing['url']} {refusing['title']!r}"
            ),
            data={
                **gate.get("data", {}),
                "source": refusing["url"],
                "result": "pending-refusal",
                "quote": refusing["title"],
                "pending_proposals": proposals,
            },
        )
        return
    named = ", ".join(f"{row['url']} {row['title']!r}" for row in proposals)
    gates[index] = {
        **gate,
        "detail": f"{gate['detail']}; an AI policy is proposed and still open: {named}",
        "data": {**gate.get("data", {}), "pending_proposals": proposals},
    }


#: Gates skipped once a cheaper gate has already failed the repository.
EXPENSIVE_GATES = ("saturation", "direct-push", "responsiveness")


def _skipped_gate(name: str, refused: list[str]) -> dict[str, Any]:
    """A gate left unread because the repository had already failed."""
    return _gate(
        name,
        passed=False,
        blocking=False,
        detail=f"not read: the repository already failed {', '.join(refused)}",
        data={"skipped": True},
    )


#: What an unread screen says when GitHub's hourly core budget ran out.
RATE_LIMITED_DETAIL = (
    "rate limited: the hourly GitHub core budget ran out during the screen; "
    "rerun after it resets (`gh api -i` shows X-Ratelimit-Reset; the "
    "rate_limit endpoint can still report it full)"
)


def _unread(
    data_root: Path, slug: str, record: dict[str, Any], gh: _Gh, detail: str
) -> dict[str, Any]:
    """Record a screen that read too little to judge, keeping any verdict.

    A refresh that read nothing is not a verdict. On 2026-09-17 a
    burst-limited batch replaced 34 full screens with empty ones; the
    previous verdict stays, with the failed attempt beside it.
    https://github.com/wolfgang-aura/Mailman/issues/117
    """
    record["detail"] = detail
    record["read_failures"] = gh.failures
    record["rate_limited"] = gh.rate_limited
    previous = load_screen(data_root, slug)
    if previous and previous.get("success"):
        unread = {
            "attempted_at": record["screened_at"],
            "detail": record["detail"],
            "read_failures": gh.failures,
        }
        _write(data_root, {**previous, "unread": unread})
        record["unread"] = unread
        record["previous"] = {
            "verdict": previous.get("verdict"),
            "failed_gates": previous.get("failed_gates", []),
            "screened_at": previous.get("screened_at"),
        }
        return record
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
        line = f"{slug}: unread, {record.get('detail', 'unknown failure')}"
        previous = record.get("previous")
        if isinstance(previous, dict):
            line += (
                f"; kept the {previous.get('verdict')} verdict screened at "
                f"{previous.get('screened_at')}"
            )
        return line
    lines = [f"screen {slug}"]
    unread = record.get("unread")
    if isinstance(unread, dict):
        lines.append(
            f"  unread refresh at {unread.get('attempted_at')}: "
            f"{unread.get('detail')}; the verdict below is from "
            f"{record.get('screened_at')}"
        )
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
