"""Read who has already claimed the target issue, in the issue's own comments.

Every other prior-art gate reads pull requests. That misses the claim that has
not become one yet, which is the earlier and more common form: on
`openai/openai-agents-python`, of twenty unassigned open issues fourteen already
had an open pull request and several of the rest had been claimed in a comment.
`check-target` called one of those unclaimed. See
https://github.com/wolfgang-aura/Mailman/issues/36.

Three states are worth telling apart, because they carry different weight:

- The issue carries a GitHub assignee. Somebody owns it. Nothing to argue with.
- A maintainer answered a claim by handing the work over. Same conclusion,
  reached in prose rather than in the assignee field.
- Somebody offered and nobody answered. That is worth a human reading, not a
  hard stop, so it is the one state a flag can clear.

The comments are read for this judgement only. They are never written into
`issue.md`, which is the same rule that keeps a merged pull request's diff out
of an agent's prompt: Mailman must not hand an agent somebody else's answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from mailman.executor import CommandResult, execute
from mailman.issue import load_issue_record
from mailman.maintainers import MAINTAINER_ASSOCIATIONS, is_maintainer
from mailman.target_intel import _is_bot
from mailman.toolchain import resolve_tool

CLAIMS_FILENAME = "claims.json"
CLAIMS_SCHEMA_VERSION = 1

#: Who can hand out the work. `author_association` is GitHub's own answer to
#: that question, and `maintainers` below is the repository screen's recorded
#: set for the maintainers it hides (Mailman #203). The set itself lives in
#: `mailman.maintainers`; it is re-exported here for existing readers.

_QUOTE_CHARACTER_LIMIT = 400

#: The words a maintainer uses to ask for the pull request. One spelling, so
#: the claim gate (where "PRs welcome" means nobody has claimed it) and the
#: shortlist (where it means the maintainer wants it) cannot disagree about
#: what an invitation is.
_WELCOME = (
    r"(?:pull requests?|prs?|patch(?:es)?|contributions?) "
    r"(?:are |would be |very |always )?welcome"
)
_FEEL_FREE = r"feel free to (?:open|submit|send|raise|pick|take|work)"
_INVITATION = re.compile(
    r"\b(?:"
    + _WELCOME
    + r"|"
    + _FEEL_FREE
    + r"|(?:happy|glad|willing|open) to (?:accept|review|merge|take|consider) "
    r"(?:a |an |any |the )?(?:pr|pull request|patch|fix|contribution)"
    r"|(?:i|we)(?:'d| would|'ll| will) (?:gladly |happily )?"
    r"(?:accept|review|merge|take) (?:a |the )?(?:pr|pull request|patch|contribution)"
    r"|(?:a |the )?(?:pr|pull request|patch) (?:would be|is) "
    r"(?:welcome|appreciated|accepted)"
    r")",
    re.IGNORECASE,
)

#: A maintainer who reserves the issue for people, or warns that agent-written
#: pull requests may be refused. beetbox/beets#6984 was a `good first issue`
#: whose maintainer wrote that it was marked "for **human** contributors" and
#: that fully automated PRs from agents may be rejected; three such PRs had
#: already been closed. Emphasis marks are stripped before matching.
_AGENT_EXCLUSION = re.compile(
    r"\b(?:"
    r"for human contributors|human contributors only|humans? only"
    r"|(?:no|not accepting|do not accept|don't accept|won't accept|will not accept)"
    r" (?:ai|llm|agent|bot)[- ](?:generated |written |authored )?"
    r"(?:prs?|pull requests?|patch(?:es)?|contributions?|code)"
    r"|(?:fully |purely )?(?:automated|ai[- ]generated|llm[- ]generated|agent[- ]generated|agentic)"
    r" (?:prs?|pull requests?|patch(?:es)?|contributions?)"
    r"(?: from (?:agents?|bots?|ai|llms?))? (?:may|will|would) (?:be|get you)"
    r" (?:rejected|closed|declined|banned)"
    # stanfordnlp/stanza#1651, a collaborator: "Anyone else who does a
    # driveby LLM PR with zero interaction with the maintainers will be
    # banned." Mailman #245.
    r"|drive[- ]?by (?:ai|llm|agent|bot)[- ]?(?:generated |written )?"
    r"(?:prs?|pull requests?|patch(?:es)?|contributions?)"
    # An issue set aside for a mentoring programme is held for its people:
    # django-debug-toolbar#2481 "this may be a good djangonaut space
    # ticket". Mailman #194.
    r"|(?:djangonaut(?: space)?|outreachy|gsoc|google summer of code"
    r"|mentee|mentorship)(?: program(?:me)?)? (?:ticket|issue|task|candidate)"
    # pvlib#2864, a member: "if I review one more microslop hallucination I
    # drop my career in software". Mailman #198.
    r"|microslop|ai[- ]slop"
    r")",
    re.IGNORECASE,
)


def excludes_agents(
    comment: dict[str, Any], *, maintainers: Collection[str] = ()
) -> bool:
    """Say whether a project voice reserves the issue for human work.

    `CONTRIBUTOR` counts here, unlike for an invitation. The beets maintainer
    who wrote the refusal shows as CONTRIBUTOR because the org membership is
    private, and wrongly honouring a refusal costs one skipped issue while
    missing it costs a run and a closed pull request.
    """
    if not isinstance(comment, dict) or _is_bot(comment.get("user")):
        return False
    if not is_maintainer(
        comment, maintainers, associations=MAINTAINER_ASSOCIATIONS | {"CONTRIBUTOR"}
    ):
        return False
    text = _matchable(_flat(comment.get("body"))).replace("*", "").replace("_", " ")
    return bool(_AGENT_EXCLUSION.search(text))


#: A project voice saying the design is still open. zarr-python#2706 left an
#: open question about nested filesystems, responses#744 had a maintainer with
#: no design at all, and marimo#6250's only proposal was a new config option
#: nobody accepted. Each was turned down by hand after the prescreen passed it.
#: https://github.com/wolfgang-aura/Mailman/issues/124
_DESIGN_OPEN = re.compile(
    r"\b(?:"
    r"not (?:sure|certain) (?:what|how|whether|if) we(?:'d)? (?:should|want|would)"
    r"|open to (?:other )?(?:ideas|suggestions|proposals)"
    r"|which (?:approach|option|design|direction)\b"
    r"|we (?:need|have|'ll need) to (?:decide|figure out)"
    r"|needs? (?:more |further |some )?discussion"
    r"|one (?:option|approach|way|possibility) (?:is|would be|could be)\b"
    r".{0,400}?\banother\b"
    r"|alternatively,? we could"
    r"|i(?:'d| would) like to hear"
    r"|rfc\b(?![\s-]*\d)"
    r"|proposals?\b"
    r"|(?:haven't|have not|not yet) decided"
    # A project voice declining for now and polling for demand. plotly/dash#3968.
    r"|inclined (?:not )?to (?:leave|keep) (?:it|this|that|them|the [\w ]{1,30}?) as[- ]is"
    r"|see if any\s?one else (?:would like|wants|needs)"
    # robotframework#5783 "deciding how/where to register ... Alternatives:
    # 1." and #5747 "do you have opinions on this?". Mailman #205.
    r"|(?:is|are|be|means?) deciding (?:how|where|whether|what|which)"
    r"|alternatives?:\s*(?:1[.)]|-|\*|a[.)])"
    r"|(?:do|does) (?:you|any\s?one) have (?:any )?(?:opinions?|thoughts|views)"
    # sentence-transformers#3996 "perhaps it would be better if ... It's
    # something to think about". Mailman #252.
    r"|(?:it's|it is|that's|that is) something to (?:think|consider)"
    r"|(?:perhaps|maybe) it would be better if"
    # A maintainer floating a new knob has not accepted any fix yet.
    r"|(?:(?:we|you) (?:could|can|might|may) (?:just |also )?"
    r"(?:add|introduce|expose|provide)"
    r"|(?:maybe|perhaps) (?:we )?(?:add|introduce|adding|introducing))"
    r" (?:a |an )?(?:new )?(?:[\w-]+ )?"
    r"(?:config(?:uration)?|option|flag|setting|parameter|knob|toggle)s?\b"
    r")",
    re.IGNORECASE,
)

#: A later project voice closing the question: an invitation to the pull
#: request, or an explicit choice.
_DESIGN_SETTLED = re.compile(
    r"\b(?:"
    r"let(?:'s| us) go with|we(?:'ll| will) go with|decided to go with"
    r"|go ahead\b|i(?:'d| would) accept|sounds good|that works for (?:me|us)"
    # fonttools#4086 "yeah, I like this proposal. We'd use STAT ...". #240.
    r"|i (?:really |do )?like (?:this|that|the|your) "
    r"(?:proposal|idea|approach|plan|suggestion)"
    r"|\+1 (?:to|for|on) (?:this|that|the|your) (?:proposal|idea|approach|plan)"
    r")",
    re.IGNORECASE,
)

_PROJECT_VOICES = MAINTAINER_ASSOCIATIONS | {"CONTRIBUTOR"}


def _sentence_around(text: str, start: int, end: int, limit: int = 240) -> str:
    left = max(text.rfind(mark, 0, start) for mark in (". ", "? ", "! "))
    right_candidates = [
        position for position in (text.find(mark, end) for mark in ".?!")
        if position != -1
    ]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    sentence = text[left + 2 if left != -1 else 0 : right].strip()
    return sentence if len(sentence) <= limit else sentence[: limit - 3] + "..."


#: A project voice turning the report down: the behaviour is intended, or the
#: change is not wanted. hgrecco/pint#2060 ("I don't think we want to
#: implement this") and fsspec/filesystem_spec#1741 ("This is functioning
#: correctly") both passed prescreen. https://github.com/wolfgang-aura/Mailman/issues/174
_DECLINED = re.compile(
    r"\b(?:"
    r"(?:this|that|it)(?: is|'s| was) (?:functioning|working|behaving) "
    r"(?:correctly|as (?:intended|expected|designed))"
    r"|works? as (?:intended|designed)"
    r"|(?:this|that|it)(?: is|'s| was) (?:the |an? )?(?:intended|expected|correct|documented) behaviou?r"
    r"|(?:this|that|it)(?: is|'s) not a bug"
    r"|i (?:don't|do not) think we (?:want|should|need) to (?:implement|support|add|change|do)"
    r"|we (?:won't|will not|don't|do not) (?:want to )?(?:fix|implement|support|change)\b"
    r"|won't ?fix\b"
    r"|handle (?:the changes|this|it) internally"
    # conan#17492, plotnine#917 and jedi#2058, the same afternoon.
    r"|(?:this|that|it)(?: is|'s) not a problem"
    r"|(?:this|that|it)(?: is|'s) expected (?:because|since|as)\b"
    r"|unlikely to be fixed"
    # huggingface_hub#2742
    r"|not something (?:we|i) (?:want|plan|intend) to\b"
    r"|we (?:don't|do not) want to (?:raise|add|implement|support|change|expose)\b"
    # biopython#5101: "too many false positives ... a limitation of the
    # GenBank file format". Mailman #195.
    r"|(?:a |an )?limitation of the (?:\S+ ){0,3}(?:file )?format"
    r"|too many false positives"
    # pipenv#6715: "I am still not convinced its is a good idea". Mailman #205.
    r"|(?:i(?:'m| am)|we(?:'re| are)) (?:still |just |really )?not (?:yet )?convinced"
    # docling#3528: "the rationale of docling not choosing sides". Mailman #234.
    r"|the rationale (?:of|for|behind) (?:\S+ ){0,3}not \w+ing\b"
    # Arelle#2399: "this is the behavior I'd expect rather than a bug".
    # Mailman #319.
    r"|(?:the )?behaviou?r (?:i'd|i would|we'd|we would) expect\b"
    r"|rather than a bug\b"
    r")",
    re.IGNORECASE,
)


def design_open_questions(
    thread: Iterable[dict[str, Any]], *, maintainers: Collection[str] = ()
) -> list[dict[str, Any]]:
    """The project-voice comments that leave a design choice open, still unsettled.

    Walked in order. A comment from a maintainer (or CONTRIBUTOR, as
    `excludes_agents` counts them) whose last settling phrase ("PR welcome",
    "let's go with", "I'd accept") comes after its last open phrase settles
    everything before it; a later open phrase reopens the question. Outsiders
    neither open nor settle anything.
    """
    return _unsettled(thread, _DESIGN_OPEN, maintainers=maintainers)


def maintainer_declines(
    thread: Iterable[dict[str, Any]], *, maintainers: Collection[str] = ()
) -> list[dict[str, Any]]:
    """The project-voice comments turning the report down, not since reversed.

    The same walk as `design_open_questions`: a later invitation or explicit
    choice from the project undoes the refusal.
    """
    return _unsettled(thread, _DECLINED, maintainers=maintainers)


def _unsettled(
    thread: Iterable[dict[str, Any]],
    pattern: re.Pattern[str],
    *,
    maintainers: Collection[str] = (),
) -> list[dict[str, Any]]:
    unsettled: list[dict[str, Any]] = []
    for comment in thread:
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        if not is_maintainer(comment, maintainers, associations=_PROJECT_VOICES):
            continue
        text = _matchable(_flat(_unquoted(comment.get("body"))))
        settles = list(_DESIGN_SETTLED.finditer(text)) + list(
            _INVITATION.finditer(text)
        )
        # "I like this proposal" settles; its "proposal" does not reopen.
        opens = [
            match
            for match in pattern.finditer(text)
            if not any(
                settle.start() <= match.start() and match.end() <= settle.end()
                for settle in settles
            )
        ]
        last_settle = max((match.start() for match in settles), default=-1)
        if last_settle != -1 and (not opens or last_settle > opens[-1].start()):
            unsettled = []
            continue
        if not opens:
            continue
        match = opens[0]
        phrase = match.group(0)
        row = _row(comment)
        row["phrase"] = phrase if len(phrase) <= 80 else phrase[:77] + "..."
        row["quote"] = _sentence_around(text, match.start(), match.end())
        unsettled.append(row)
    return unsettled


#: Asking after a bug is not claiming it. These run first, because several of
#: them contain the words a claim is made of: "is anyone working on this" would
#: otherwise read as "working on this".
_NOT_A_CLAIM = re.compile(
    r"\b(?:"
    r"(?:is |are |has |have )?(?:any\s?one|any\s?body|some\s?one|some\s?body)\b"
    r"|any (?:update|progress|news|luck)"
    r"|has this been"
    r"|i(?:'m| am| was)? ?(?:no longer|not) working on"
    r"|" + _WELCOME + r")",
    re.IGNORECASE,
)

#: Somebody saying they are taking the work. An offer phrased as a question
#: ("can I work on this?") counts: it is still an announcement of intent, and
#: filing over it is the duplicate this gate exists to stop.
_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'m|m| am) (?:currently )?(?:working on|taking|fixing|looking into)"
    r"|i(?:'ll|ll| will| can| could| would like to|'d like to|d like to"
    r"| want to| plan to| intend to) "
    r"(?:take|work on|pick|fix|handle|submit|open|raise|send|look into"
    r"|tackle|contribute|have a go|give)"
    r"|i(?:'ve|ve| have) (?:a|an|the) (?:pr|patch|fix|branch|change)"
    r"|(?:please )?assign (?:this |it |the issue |me )?(?:to )?(?:me\b|myself)"
    r"|assign me"
    # quantumlib/Cirq#8317: "I am interested in working on this issue ...
    # could this issue be assigned to me?" read as no claim at all.
    r"|(?:could|can|may|would) (?:this|it|the issue) (?:please )?be assigned to me"
    r"|i(?:'m|m| am) (?:very |quite |really )?interested in (?:working on|taking|fixing|picking up)"
    r"|can i (?:take|work on|pick|try|have|give|attempt)"
    r"|may i (?:take|work on|pick|try|have|attempt)"
    r"|picking (?:this|it) up"
    r"|taking (?:this|it)(?: up| on)?\b"
    r"|let me (?:take|work on|handle|try|have|give)"
    r"|on it\b"
    r"|i(?:'m|m| am) on (?:this|it)\b"
    r"|working on (?:this|it) (?:now|already)"
    # A reporter who already wrote the fix. domokane/FinancePy#262 to #268
    # each carried a candidate patch and an offer to turn it into a pull
    # request, and the gate read them as unclaimed. See
    # https://github.com/wolfgang-aura/Mailman/issues/93.
    # robotframework#5774's report: "I'm happy to implement this via a pull
    # request". Mailman #198.
    # terryyin/lizard#487 dropped the "I'm": "Happy to put that together with
    # tests". Mailman #225.
    r"|(?:i(?:'m|m| am) |^)(?:glad|happy|pleased|willing) to "
    r"(?:implement|prepare|open|submit|send|raise|take|work|fix|make|contribute"
    r"|put (?:that|this|it|one|something) together)"
    r"|i(?:'d| would) be (?:glad|happy|pleased) to "
    r"(?:prepare|open|submit|send|raise|help|take|work|fix|make|turn|contribute)"
    r"|(?:attached|linked|local|my) candidate"
    r"|candidate (?:patch|fix|source|diff|change)"
    r"|proposed (?:correction|fix|patch|change|diff)"
    # A contributor whose pull request is on its way or already up, even
    # after a bot closed it. huggingface/peft#3804 said "Will have a PR up"
    # and "PR is up: #3832", and prescreen read neither as a claim.
    r"|will have (?:a|the|my) (?:pr|pull request|patch|fix) (?:up|ready|open)"
    r"|(?:my |the |a )?(?:pr|pull request) is (?:up|open|ready)\b"
    r"|(?:i(?:'ve|ve| have) )?(?:opened|submitted|raised|sent) "
    r"(?:a |the |my )?(?:pr|pull request|#\d+)"
    r"|still planning to"
    # A reporter settling the design before building it: biopython#5307
    # "I'd like to check one more point before I start ... I'll follow
    # whichever option you recommend". Mailman #204.
    r"|before i (?:start|begin|get started|dive in)\b"
    r"|i(?:'ll|ll| will) (?:follow|go with|implement) (?:whichever|whatever)"
    # biopython#4878's reporter: "yes I can try implementing a solution" and
    # "Ok, I'll try and implement (1)." Mailman #211.
    r"|\bi(?:'ll|ll| will| can| could)? try (?:and |to )?"
    r"(?:implement|fix|work on|submit|open|make|tackle|put together|write)"
    r")",
    re.IGNORECASE,
)

#: A maintainer handing the work to whoever asked. "PRs welcome" is not here on
#: purpose: it invites anybody, which is the opposite of a claim.
_ASSIGNMENT = re.compile(
    r"\b(?:"
    r"(?:i(?:'ve|ve| have) )?assigned (?:this |it |the issue )?to\b"
    r"|assigned to (?:you|@)"
    r"|(?:this |it )?(?:is |'s )?all yours\b"
    r"|it(?:'s| is) yours\b"
    r"|go ahead\b"
    r"|" + _FEEL_FREE + r"|you can (?:take|work on|pick|have) (?:this|it)"
    r"|(?:please )?go for it\b"
    r")",
    re.IGNORECASE,
)


#: GitHub's comment box turns a typed apostrophe into U+2019, so the real
#: comment on openai/openai-agents-python #4775 reads "I’d like to work on
#: this issue" and matched nothing. Normalise before matching, never before
#: quoting: the operator should see what was actually written.
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "＇": "'"})


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _unquoted(text: str | None) -> str:
    """The comment without its `>` quoted lines: those are somebody else's words.

    ansible/ansible-lint#4857 quoted "if someone else sees this comment" above
    "I'll work on this", and the quoted "someone" read the claim as a question.
    """
    return "\n".join(
        line for line in (text or "").splitlines() if not line.lstrip().startswith(">")
    )


_CODE_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.MULTILINE | re.DOTALL)


def _matchable(text: str) -> str:
    return text.translate(_APOSTROPHES)


#: The three ways a pull request is named in an issue thread. A full URL and
#: `owner/repo#N` can point anywhere; a bare `#N` means the repository the
#: issue lives in. deepset-ai/haystack#12777 ends with
#: "**Draft implementation:** [#12775](https://github.com/deepset-ai/haystack/pull/12775)"
#: and the duplicate search never found it, because #12775 does not cite the
#: issue back. See https://github.com/wolfgang-aura/Mailman/issues/98.
_PULL_REQUEST_URL = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_HASH_REFERENCE = re.compile(
    r"(?:\b([A-Za-z0-9_.-]+)/)?(?:\b([A-Za-z0-9_.-]+))?#(\d+)\b"
)

#: Words written straight before `#N` that name the kind of thing, not a
#: repository: `PR#12`, `issue#13`. Any other bare name, `docling-core#466`,
#: is a sibling repository under the same owner. Mailman #235.
_NOT_A_REPOSITORY = frozenset(
    {
        "pr", "prs", "pull", "issue", "issues", "bug", "gh",
        "fix", "fixes", "fixed", "close", "closes", "closed",
        "resolve", "resolves", "resolved", "see",
    }
)

#: How many references one thread may hand to the resolver. Each one is a `gh`
#: call, and a long thread quoting version numbers and colours would otherwise
#: buy a page of them. First mention first: the reference that decides a screen
#: is normally the one the reporter wrote into the body.
REFERENCE_LIMIT = 10


def pull_request_references(
    texts: Sequence[str | None],
    *,
    repository: str,
    exclude: Iterable[tuple[str, int]] = (),
    limit: int = REFERENCE_LIMIT,
    origins: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Every pull-request reference written in these texts, first mention first.

    `origins`, parallel to `texts`, says where each text sits in the thread
    (`body`, `comment`, `timeline`); the first mention's origin is kept under
    `in`, because a merged pull request the reporter names in the body is the
    cause or the context of the report, not its fix.

    Nothing here decides whether the number is a pull request at all: `#12`
    reads the same whether it names an issue, a pull request or a heading
    anchor, and only `gh pr view` can tell them apart. This finds candidates
    and records how each was written, so a rejection can quote the reference
    that caused it rather than assert one.
    """
    skip = {(slug.lower(), number) for slug, number in exclude}
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, text in enumerate(texts):
        flat = _flat(text)
        if not flat:
            continue
        origin = origins[index] if origins and index < len(origins) else None
        for pattern in (_PULL_REQUEST_URL, _HASH_REFERENCE):
            for match in pattern.finditer(flat):
                owner, name, digits = match.groups()
                if owner and name:
                    slug = f"{owner}/{name}"
                elif name and name.lower() not in _NOT_A_REPOSITORY:
                    slug = f"{repository.partition('/')[0]}/{name}"
                else:
                    slug = repository
                number = int(digits)
                key = (slug.lower(), number)
                if key in seen or key in skip or number <= 0:
                    continue
                seen.add(key)
                found.append(
                    {
                        "text": match.group(0),
                        "repository": slug,
                        "number": number,
                        "in": origin,
                    }
                )
    return found[:limit]


def _cross_referenced_urls(timeline: Any) -> list[str]:
    """The pull requests GitHub itself linked to this issue.

    python-jsonschema/jsonschema#1497 is the case text alone misses: its one
    comment says "the issue/PR in referencing" without a number, and the open
    cross-repository pull request, `python-jsonschema/referencing#367`, is on
    the timeline instead. One more read of a page already being paged.
    """
    if not isinstance(timeline, list):
        return []
    urls: list[str] = []
    for entry in timeline:
        if not isinstance(entry, dict) or entry.get("event") != "cross-referenced":
            continue
        source = entry.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        url = issue.get("html_url") if isinstance(issue, dict) else None
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def rival_pull_requests(timeline: Any) -> list[str]:
    """The open or merged pull requests GitHub cross-referenced to this issue.

    A closed, unmerged one is an abandoned attempt and leaves the issue
    workable. Of 55 maintainer-confirmed bugs read on 2026-09-28, 24 already
    had one of these; the screen now finds them instead of a hand script.
    """
    if not isinstance(timeline, list):
        return []
    rivals: list[str] = []
    for entry in timeline:
        if not isinstance(entry, dict) or entry.get("event") != "cross-referenced":
            continue
        source = entry.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(issue, dict):
            continue
        pull = issue.get("pull_request")
        if not isinstance(pull, dict):
            continue
        if issue.get("state") != "open" and not pull.get("merged_at"):
            continue
        url = str(issue.get("html_url") or "")
        match = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
        name = f"{match.group(1)}#{match.group(2)}" if match else url
        if name and name not in rivals:
            rivals.append(name)
    return rivals


#: A label that only routes the issue to triage says nobody has triaged it:
#: streamlit's `ai-review` summons a bot that labels and comments, and
#: `needs triage` asks for a person. Mailman #267.
_ROUTING_LABEL = re.compile(
    r"(?i)\bai[ _-]?(?:review|triage)\b"
    r"|\b(?:needs|pending|awaiting|to)[ _:-]*triage\b"
    r"|\buntriaged\b|^\s*triage\s*$"
)


def is_routing_label(name: str) -> bool:
    """Whether a label only asks for triage rather than recording it."""
    return bool(_ROUTING_LABEL.search(name))


def maintainer_labels(timeline: Any, *, reporter: str | None) -> list[dict[str, Any]]:
    """The labels somebody who can triage put on the issue, grouped per act.

    securo-finance/securo#972 was triaged with `prio:high` and `risk:medium`
    and no comment, and the untriaged-issue question fired anyway. GitHub lets
    only an account with triage access label another person's issue, so a
    `labeled` event from anybody but the reporter or a bot is triage. The
    reporter's own label is not, whatever template put it there.
    See https://github.com/wolfgang-aura/Mailman/issues/139.
    """
    if not isinstance(timeline, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in timeline:
        if not isinstance(entry, dict) or entry.get("event") != "labeled":
            continue
        actor = entry.get("actor")
        if _is_bot(actor):
            continue
        login = actor.get("login") if isinstance(actor, dict) else None
        label = entry.get("label")
        name = label.get("name") if isinstance(label, dict) else None
        if not login or not name or (reporter and login.lower() == reporter.lower()):
            continue
        if is_routing_label(name):
            continue
        at = entry.get("created_at")
        if rows and rows[-1]["actor"] == login and rows[-1]["at"] == at:
            rows[-1]["labels"].append(name)
        else:
            rows.append({"labels": [name], "actor": login, "at": at})
    return rows


def classify_comment(
    comment: dict[str, Any], *, maintainers: Collection[str] = ()
) -> str | None:
    """Say whether one comment claims the work, hands it over, or neither.

    Returns `"claim"`, `"assignment"`, or `None`. A bot never claims anything,
    and only a maintainer can hand work over: the same sentence from an
    outsider is an opinion.
    """
    if not isinstance(comment, dict) or _is_bot(comment.get("user")):
        return None
    # Fenced code is pasted output, not prose: pyright's "cannot be assigned
    # to parameter" read as a handover on py-shiny#2497. Mailman #207.
    prose = _CODE_FENCE.sub(" ", _unquoted(comment.get("body")))
    # A Markdown heading is a label: NetBox's issue form prints "### Proposed
    # Fix" on every report, filled or not. Mailman #254.
    prose = _HEADING.sub(" ", prose)
    body = _matchable(_flat(prose))
    if not body:
        return None
    maintainer = is_maintainer(comment, maintainers)
    if maintainer and _ASSIGNMENT.search(body):
        return "assignment"
    # Judge each sentence on its own: a question in one ("if someone reports
    # it later") must not cancel a claim in another. Mailman #204.
    for sentence in _SENTENCE_BREAK.split(body):
        if _CLAIM.search(sentence) and not _NOT_A_CLAIM.search(sentence):
            return "claim"
    return None


_SENTENCE_BREAK = re.compile(r"(?<=[.?!;])\s+")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t].*$", re.MULTILINE)


def classify_thread(
    comments: Iterable[dict[str, Any]], *, maintainers: Collection[str] = ()
) -> list[str | None]:
    """`classify_comment` over a thread, in order, with one contextual rule.

    "Feel free to open a PR" is a handover when it answers "can I take
    this?", and an invitation to anybody when nobody has asked. The sentence
    alone cannot tell the two apart; the thread can. A maintainer's
    invitation-phrased reply with no claim before it is read as
    `"invitation"` here, so the saturation gate does not count the issue
    claimed and `check-target` does not refuse it as handed over. Every other
    handover phrasing is left alone: "go ahead, all yours" names somebody
    even when the claim it answers was made elsewhere.
    """
    kinds: list[str | None] = []
    claimed_before = False
    for comment in comments:
        kind = classify_comment(comment, maintainers=maintainers)
        if (
            kind == "assignment"
            and not claimed_before
            and is_maintainer_invitation(comment, maintainers=maintainers)
        ):
            kind = "invitation"
        if kind == "claim":
            claimed_before = True
        kinds.append(kind)
    return kinds


def invites_pull_request(text: str | None) -> bool:
    """Say whether this text asks for a pull request, whoever wrote it."""
    return bool(_INVITATION.search(_matchable(_flat(text))))


def is_maintainer_invitation(
    comment: dict[str, Any], *, maintainers: Collection[str] = ()
) -> bool:
    """Say whether one comment is a maintainer asking for the pull request.

    The report itself counts when its author is a maintainer: a member who
    writes up a bug and says "PRs welcome" has invited the fix as plainly as
    one who answers a stranger's report the same way. From anybody else the
    same sentence is an opinion, which is the rule `classify_comment` already
    applies to handing the work over.
    """
    if not isinstance(comment, dict) or _is_bot(comment.get("user")):
        return False
    if not is_maintainer(comment, maintainers):
        return False
    return invites_pull_request(comment.get("body"))


def maintainer_touched_at(
    comments: Iterable[dict[str, Any]], *, maintainers: Collection[str] = ()
) -> str | None:
    """The newest timestamp at which somebody who speaks for the project wrote."""
    stamps = [
        str(comment.get("created_at"))
        for comment in comments
        if isinstance(comment, dict)
        and is_maintainer(comment, maintainers)
        and not _is_bot(comment.get("user"))
        and comment.get("created_at")
    ]
    return max(stamps) if stamps else None


def _moment(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        moment = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def offer_replies(
    comments: Iterable[dict[str, Any]],
    since: object,
    *,
    maintainers: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Maintainer comments written after the ask-first offer was handed over.

    One of these is the answer the offer asked for, and the run moves from ASK
    to the normal SEND path on it. `since` is the offer handoff's
    `prepared_at`; no offer, no replies. Bots do not answer.
    https://github.com/wolfgang-aura/Mailman/issues/138
    """
    start = _moment(since)
    if start is None:
        return []
    return [
        _row(comment, maintainers)
        for comment in comments
        if isinstance(comment, dict)
        and is_maintainer(comment, maintainers)
        and not _is_bot(comment.get("user"))
        and (_moment(comment.get("created_at")) or start) > start
    ]


def _row(
    comment: dict[str, Any], maintainers: Collection[str] = ()
) -> dict[str, Any]:
    user = comment.get("user") or {}
    row = {
        "author": user.get("login") if isinstance(user, dict) else None,
        "association": comment.get("author_association"),
        "created_at": comment.get("created_at"),
        "quote": _flat(comment.get("body"))[:_QUOTE_CHARACTER_LIMIT],
    }
    # The author is in the repository screen's maintainer set, whatever the
    # association says. Only written when true, so older rows read the same.
    if maintainers and is_maintainer(comment, maintainers, associations=()):
        row["listed_maintainer"] = True
    return row


def _write(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / CLAIMS_FILENAME
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_claims(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / CLAIMS_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def triage_warning(run_directory: Path) -> str | None:
    """Why the issue may not be a bug: nobody who speaks for the project said so.

    An outside reporter, no maintainer reply. pytest-dev/pytest#14992 looked
    exactly like this when #14993 was filed against it; the maintainer's
    first reply, eleven hours later, said the use case was unsupported, and
    the reporter withdrew the premise. Older claims records lack the fields
    and get no warning, which is a gap and not a pass.
    See https://github.com/wolfgang-aura/Mailman/issues/88 and /issues/90. skfolio#316 was
    filed on an issue in the same state, four days old and unanswered; the
    maintainer closed it in fifteen minutes: the behaviour was a choice.
    """
    claims = load_claims(run_directory) or {}
    reporter = claims.get("reporter_association")
    replied = claims.get("maintainer_replied")
    if reporter is None or replied is None:
        return None
    if reporter in MAINTAINER_ASSOCIATIONS or replied:
        return None
    if claims.get("reporter_is_maintainer") is True:
        return None
    # A label from somebody with triage access is triage in another form.
    # Mailman #139.
    if claims.get("maintainer_labelled"):
        return None
    return (
        f"the issue was reported from outside the project ({reporter}) and no "
        "owner, member or collaborator has replied on it. Nobody who can "
        "speak for the project has said this is a bug they want fixed. "
        "pytest-dev/pytest#14993 was filed on an issue in this state; the "
        "premise turned out to be wrong and the pull request closed unread."
    )


def read_claims(
    run_directory: Path,
    *,
    executable: str | None = None,
    timeout_seconds: float = 60,
    execute: Callable[..., CommandResult] = execute,
    pages: int = 4,
    maintainers: Collection[str] = (),
) -> dict[str, Any]:
    """Record who has claimed the run's target issue, from its own thread.

    `maintainers` is the login set the repository screen recorded; a comment
    by one of them counts as a maintainer's whatever GitHub's association
    says. Empty, every check reads the association alone. Mailman #203.
    """
    record: dict[str, Any] = {
        "schema_version": CLAIMS_SCHEMA_VERSION,
        "collected_at": datetime.now(UTC).isoformat(),
        "success": False,
        "claims": [],
        "assignments": [],
        "assignees": [],
        "comments_read": 0,
        "commands": [],
    }
    issue = load_issue_record(run_directory) or {}
    if issue.get("self_reported") is True:
        # A defect the operator wrote has no upstream thread, so there is
        # nothing to claim and nobody to have claimed it. That is a different
        # fact from "the thread was read and held no claim", and `check-target`
        # is entitled to see which one it got.
        record.update(
            {
                "success": True,
                "self_reported": True,
                "detail": (
                    "no upstream issue: this run started from a defect report, "
                    "so no thread exists to claim it in. The duplicate search "
                    "is the only prior-art evidence here."
                ),
            }
        )
        _write(run_directory, record)
        return record
    reference = issue.get("reference") or {}
    owner = reference.get("owner")
    name = reference.get("repository")
    number = reference.get("number")
    if not (owner and name and number):
        record["detail"] = (
            "no captured issue to read: run `mailman fetch-issue` first"
        )
        _write(run_directory, record)
        return record
    slug = f"{owner}/{name}"
    record["repository"] = slug
    record["issue_number"] = number

    command_executable = executable or resolve_tool(run_directory, "gh")

    def api(path: str, **query: int | str) -> Any | None:
        # Query parameters go in as fields rather than in the path. `gh` adds
        # them to the query string of a GET either way, and the recorded
        # command then carries no `&`, which is a command separator to every
        # Windows shell that ever re-runs it.
        command = [command_executable, "api", path]
        if query:
            command.append("-X")
            command.append("GET")
            for key, value in query.items():
                command += ["-f", f"{key}={value}"]
        result = execute(
            command,
            working_directory=run_directory,
            timeout_seconds=timeout_seconds,
        )
        record["commands"].append(result.to_dict())
        if result.timed_out or result.exit_code != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    payload = api(f"repos/{slug}/issues/{number}")
    if not isinstance(payload, dict):
        # An unreadable thread is not an empty one. Recording it as clean is
        # exactly the false clearance this gate exists to prevent.
        record["detail"] = f"{slug}#{number} could not be read"
        _write(run_directory, record)
        return record
    record["assignees"] = [
        entry.get("login")
        for entry in payload.get("assignees") or []
        if isinstance(entry, dict) and entry.get("login")
    ]
    record["issue_state"] = payload.get("state")
    record["issue_closed_at"] = payload.get("closed_at")
    # Who reported it, and whether anyone who can speak for the project has
    # answered. pytest-dev/pytest#14992 was thirteen hours old, reported from
    # outside, unanswered, and its premise was wrong; the fix filed against it
    # closed without a word. See wolfgang-aura/Mailman#88.
    record["reporter_association"] = payload.get("author_association")
    record["reporter_is_maintainer"] = is_maintainer(
        {
            "author_association": payload.get("author_association"),
            "user": payload.get("user"),
        },
        maintainers,
    )

    comments: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        got = api(
            f"repos/{slug}/issues/{number}/comments", per_page=100, page=page
        )
        # A failed later page is no end of the thread: a claim past comment
        # 100 read as none. Mailman #344.
        if not isinstance(got, list):
            record["detail"] = (
                f"the comments on {slug}#{number} could not be read (page {page})"
            )
            _write(run_directory, record)
            return record
        comments += got
        if len(got) < 100:
            break

    # The report itself is the reporter's first comment. A reporter who says
    # "proposed correction" and pastes the diff has claimed the work as surely
    # as one who comments "I'll open a PR" later.
    report = {
        "id": payload.get("id"),
        "user": payload.get("user"),
        "author_association": payload.get("author_association"),
        "body": payload.get("body"),
        "created_at": payload.get("created_at"),
        "html_url": payload.get("html_url"),
    }
    record["invitations"] = []
    record["agent_exclusions"] = []
    thread = [report, *comments]
    for comment, kind in zip(
        thread, classify_thread(thread, maintainers=maintainers)
    ):
        if kind == "claim":
            record["claims"].append(_row(comment, maintainers))
        elif kind == "assignment":
            record["assignments"].append(_row(comment, maintainers))
        # Read apart from the claim question. The reply that hands the work
        # to one person and the reply that asks anybody for it are both
        # invitations where the shortlist is concerned, and neither is a claim.
        if is_maintainer_invitation(comment, maintainers=maintainers):
            record["invitations"].append(_row(comment, maintainers))
        if excludes_agents(comment, maintainers=maintainers):
            record["agent_exclusions"].append(_row(comment, maintainers))
    record["design_undecided"] = design_open_questions(
        thread, maintainers=maintainers
    )
    record["declined"] = maintainer_declines(thread, maintainers=maintainers)
    # The screen asked this of its rows since #150; prescreen never did, and
    # passed threads waiting on logs or sent to another project. Mailman #193.
    record["disputed"] = maintainer_dispute(thread, maintainers=maintainers)
    record["reported_fixed"] = reported_fixed(thread)
    record["comments_read"] = len(comments)
    record["issue_created_at"] = payload.get("created_at")
    record["maintainer_touched_at"] = maintainer_touched_at(
        thread, maintainers=maintainers
    )
    # Every pull request the thread names, unresolved. Deciding what each one
    # is costs a `gh pr view` per reference, so the read stops at collecting
    # them and `prescreen` pays for the ones it wants.
    # https://github.com/wolfgang-aura/Mailman/issues/98
    # Every page, and a failed page is no timeline at all: a rival pull
    # request is a cross-reference here, and a missing one reads as no rival.
    # Mailman #339.
    timeline: list[Any] = []
    for page in range(1, pages + 1):
        got = api(
            f"repos/{slug}/issues/{number}/timeline", per_page=100, page=page
        )
        if not isinstance(got, list):
            record["detail"] = (
                f"the timeline of {slug}#{number} could not be read (page {page})"
            )
            _write(run_directory, record)
            return record
        timeline += got
        if len(got) < 100:
            break
    comment_bodies = [
        comment.get("body") for comment in comments if isinstance(comment, dict)
    ]
    cross_referenced = _cross_referenced_urls(timeline)
    record["remarks_elsewhere"] = remarks_elsewhere(
        timeline, api, repository=slug, number=int(number), maintainers=maintainers
    )
    record["references"] = pull_request_references(
        [payload.get("body"), *comment_bodies, *cross_referenced],
        repository=slug,
        exclude=[(slug, int(number))],
        origins=[
            "body",
            *(["comment"] * len(comment_bodies)),
            *(["timeline"] * len(cross_referenced)),
        ],
    )
    record["maintainer_replied"] = any(
        is_maintainer(comment, maintainers)
        for comment in comments
        if isinstance(comment, dict)
    )
    # Imported here: `handoff` reads this module at import time.
    from mailman.handoff import load_offer_handoff

    offer = load_offer_handoff(run_directory)
    if offer is not None:
        record["offer_prepared_at"] = offer.get("prepared_at")
        record["offer_replies"] = offer_replies(
            comments, offer.get("prepared_at"), maintainers=maintainers
        )
    reporter = payload.get("user")
    record["maintainer_labelled"] = maintainer_labels(
        timeline,
        reporter=reporter.get("login") if isinstance(reporter, dict) else None,
    )
    record["success"] = True
    _write(run_directory, record)
    return record


def render_claims(record: dict[str, Any]) -> str:
    """Render the thread's verdict, in the words that decide a run."""
    if record.get("self_reported") is True:
        return (
            "# Claims\n\n"
            "This run started from a defect report, not an upstream issue.\n"
            "There is no thread, so there is nothing to claim and nobody to\n"
            "have claimed it. Prior art here rests entirely on the duplicate\n"
            "search.\n"
        )
    slug = record.get("repository")
    number = record.get("issue_number")
    lines = [
        f"# Claims on {slug}#{number}",
        "",
        f"- Comments read: {record.get('comments_read', 0)}",
        f"- Assignees: {', '.join(record.get('assignees') or []) or 'none'}",
    ]
    if not record.get("success"):
        lines += ["", f"Unread: {record.get('detail', 'unknown failure')}"]
        return "\n".join(lines) + "\n"
    for heading, key in (
        ("## Claims", "claims"),
        ("## Maintainer replies handing the work over", "assignments"),
        ("## Maintainer replies asking for a pull request", "invitations"),
        ("## Maintainer replies reserving the issue for human work", "agent_exclusions"),
    ):
        rows = record.get(key) or []
        if not rows:
            continue
        lines += ["", heading, ""]
        for row in rows:
            lines.append(
                f"- **{row.get('author')}** ({row.get('association')}, "
                f"{row.get('created_at')}): {row.get('quote')}"
            )
    if not (record.get("claims") or record.get("assignments")):
        lines += ["", "No claim was made in this issue's comments."]
    return "\n".join(lines) + "\n"


#: A project voice saying the report does not reproduce for them.
_NOT_REPRODUCED = re.compile(
    r"\b(?:"
    r"(?:can(?:no|')?t|cannot|could(?:n't| not)|unable to|not able to|failed to)"
    # Arelle#2570 "I can't recreate this". Mailman #359.
    r" (?:reproduce|repro|replicate|recreate)\b"
    # jedi#2077: "I have tried to reproduce this, but couldn't." Mailman #210.
    r"|tried (?:to )?(?:reproduc|repro|replicat)\w*(?:[^.!?\n]|(?<=\d)\.(?=\d)){0,60}?\bbut"
    r" (?:couldn't|could not|can't|cannot|wasn't able|was not able|was unable|failed)\b"
    r"|works (?:fine )?for me\b"
    # huggingface_hub#3430 "It works on my side". Mailman #256.
    r"|works (?:fine )?on my (?:side|end|machine)\b"
    r"|no repro\b"
    r")",
    re.IGNORECASE,
)

#: A project voice asking for a retry or more detail has not triaged the
#: report yet. kombu#2291's only maintainer word was "can you please try
#: latest release ... and report back?", unanswered for 518 days, and it
#: ranked engaged. Mailman #190.
_NEEDS_INFO = re.compile(
    r"\b(?:"
    r"(?:try|test|check|retry)(?: it| this| again)?(?: (?:with|on|using|against))?"
    r" (?:the )?(?:latest|newest|most recent|current)"
    r" (?:release|version|main|master|dev|develop)"
    r"|report back\b"
    r"|(?:please |can you |could you )(?:upgrade|update)\b"
    r"|(?:can|could) you (?:please )?(?:provide|share|post|give us|add)"
    r" (?:a |an |some )?(?:minimal|reproduc|repro|example|traceback|more)"
    r"|need(?:s)? (?:a |some )?more (?:info|information|details|context)"
    # huggingface_hub#3974 "Can you attach the full crash report", #3871
    # "Could you share the logs", #3747 "have you tried upload_large_folder",
    # #3795 "Could you try disabling xet". Mailman #193.
    r"|(?:can|could) you (?:please )?(?:attach|share|post|send|provide)"
    r" (?:the |a |an |some |your |us )?[\"'“]?(?:full |complete |debug )?"
    # docling#2714 "can you please share a file which triggers the problem?"
    r"(?:logs?|crash|stack ?trace|output|details|files?|samples?|documents?"
    r"|pdfs?|examples?|reproduc\w*|repro)"
    r"|(?:can|could) you (?:please )?(?:re)?try\b"
    # marimo#9185 "not sure why your database is being locked. Are you
    # writing to the db?" and "maybe can try closing the connection
    # manually": a guess and a workaround, not a confirmed bug. Mailman #256.
    r"|not sure why\b"
    r"|maybe (?:you )?(?:can|could) try\b"
    # s3fs#999 "Could you please show the contents of fs.dircache". #195.
    r"|(?:can|could) you (?:please )?show (?:us |me )?(?:the |what|how|your)"
    r"|have you tried\b"
    # prefect#22314 "have you checked out the database maintenance docs?"
    r"|have you (?:checked|looked at|read|seen)\b"
    # prefect#22334 "I'll check to see if I can reproduce": still verifying.
    # Mailman #230.
    r"|(?:check|see|try|look)(?: to see)? (?:if|whether) (?:i|we) (?:can|could)"
    r" (?:reproduce|repro|replicate)\b"
    r")",
    re.IGNORECASE,
)

#: A project voice sending the report to another project's tracker: the fix
#: does not belong here. huggingface_hub#3795 was sent to xet-core, and
#: plotnine#975 is "a known quarto issue". Mailman #193.
_ELSEWHERE = re.compile(
    r"\b(?:"
    r"(?:open|opening|file|filing|report|reporting|raise|raising)"
    r" (?:an? |this |the )?(?:issue|bug|report|it|this)"
    r" (?:in|on|at|to|with|against) (?:the )?\S+ (?:repo|repository|project|tracker)"
    r"|(?:is|looks like|seems like) (?:a |an )?(?:known )?\S+ (?:issue|bug)"
    r" (?:in|with|upstream)\b"
    r"|this is (?:a |an )?known \S+ (?:issue|bug)"
    r"|upstream (?:issue|bug)\b"
    # Translations live on a platform, not in the repository's .po files:
    # django-debug-toolbar#2329 "adjust this in Transifex". Mailman #194.
    r"|(?:in|on|via|through|using) (?:transifex|weblate|crowdin|pontoon)\b"
    r")",
    re.IGNORECASE,
)

#: A project voice confirming the report: the answer that ends a dispute.
_CONFIRMED = re.compile(
    r"\b(?:"
    # A conditional is a promise to check, not a result. Mailman #230.
    r"(?<!if )(?<!whether )"
    r"(?:i|we) (?:can|could|was able to|am able to|were able to) "
    r"(?:reproduce|repro|replicate)\b"
    r"|(?:confirmed|reproduced)\b"
    r"|looks like a bug\b"
    r"|you(?:'re| are) right\b"
    r"|this is a bug\b"
    r")",
    re.IGNORECASE,
)


#: Anyone saying the bug is gone on the development head or the latest
#: release. pylint#10032: "This no longer reproduces on current `main`", and
#: prescreen passed it. Mailman #236.
_HEAD = (
    r"(?:the )?(?:current |latest |newest |most recent )?`?"
    r"(?:main|master|dev|develop|head|trunk|latest|release|version)\b"
)
_REPORTED_FIXED = re.compile(
    r"\b(?:"
    r"(?:no longer|doesn't|does not|can't|cannot|couldn't|could not|can no longer)"
    r" (?:reproduces?|repro|happens?|occurs?)"
    r"(?: (?:this|it))?(?: (?:anymore|any more))?"
    rf" (?:on|with|in|using|against) {_HEAD}"
    rf"|(?:was |is |been |got )?(?:already )?(?:fixed|resolved) (?:on|in|by) {_HEAD}"
    r")",
    re.IGNORECASE,
)
_STILL_BROKEN = re.compile(
    r"\b(?:still (?:happens|happening|reproduces|occurs|fails|broken|an issue|present)"
    r"|(?:is |remains )?still (?:reproducible|there)"
    r"|(?:can|could) still (?:reproduce|repro))\b",
    re.IGNORECASE,
)


def reported_fixed(thread: Iterable[dict[str, Any]]) -> str | None:
    """The sentence of the latest comment saying the bug no longer happens.

    Any author counts, because the claim is cheap to check and expensive to
    miss: the next stages are a clone, an environment and a reproduction. A
    later comment saying it still happens cancels it.
    """
    latest: tuple[bool, str] | None = None
    for comment in thread:
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        text = _matchable(_flat(_unquoted(comment.get("body"))))
        found = [
            (match.start(), True, match) for match in _REPORTED_FIXED.finditer(text)
        ] + [
            (match.start(), False, match) for match in _STILL_BROKEN.finditer(text)
        ]
        if not found:
            continue
        _, fixed, match = max(found, key=lambda item: item[0])
        latest = (fixed, _sentence_around(text, match.start(), match.end()))
    return latest[1] if latest and latest[0] else None


def maintainer_dispute(
    thread: Iterable[dict[str, Any]], *, maintainers: Collection[str] = ()
) -> str | None:
    """The sentence of the project's latest word when that word disputes the bug.

    `hunt targets --engaged-only` counted any maintainer reply as triage, so
    it offered threads whose maintainer said "could not reproduce", "works as
    designed" or left the design open: 21 of 28 hand rejections in hunt
    20260928T094000Z-3b91d9. The latest project voice wins, so a later "I can
    reproduce" or "PR welcome" ends the dispute. Mailman #150.
    """
    latest: tuple[bool, str] | None = None
    for comment in thread:
        if not isinstance(comment, dict) or _is_bot(comment.get("user")):
            continue
        if not is_maintainer(comment, maintainers, associations=_PROJECT_VOICES):
            continue
        text = _matchable(_flat(_unquoted(comment.get("body"))))
        settles = [
            match
            for pattern in (_CONFIRMED, _DESIGN_SETTLED, _INVITATION)
            for match in pattern.finditer(text)
        ]
        # The "proposal" in "I like this proposal" is not a later dispute. #240.
        found = [
            (match.start(), True, match)
            for pattern in (
                _NOT_REPRODUCED, _DECLINED, _DESIGN_OPEN, _NEEDS_INFO, _ELSEWHERE
            )
            for match in pattern.finditer(text)
            if not any(
                settle.start() <= match.start() and match.end() <= settle.end()
                for settle in settles
            )
        ] + [(match.start(), False, match) for match in settles]
        if not found:
            continue
        _, disputes, match = max(found, key=lambda item: item[0])
        latest = (
            disputes,
            _sentence_around(text, match.start(), match.end()),
        )
    return latest[1] if latest and latest[0] else None


#: Cross-referencing issues read for maintainer remarks, at one API call each.
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)\s]*\)")
_REMARK_SOURCES = 5


def remarks_elsewhere(
    timeline: Any,
    api: Any,
    *,
    repository: str,
    number: int,
    maintainers: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Maintainer comments about this issue written in another issue.

    pyinstaller#9224 is cross-referenced to #9121, and there two maintainers
    preferred always passing `--best`, "without having explicit fallbacks".
    The run for #9121 built the fallback and was recommended for filing,
    because nothing read that thread. Only issues in the same repository are
    read, and only comments that name this issue. Mailman #187.
    """
    if not isinstance(timeline, list):
        return []
    mention = re.compile(
        rf"(?:#{number}\b|github\.com/{re.escape(repository)}/issues/{number}\b)",
        re.IGNORECASE,
    )
    sources: list[str] = []
    found: list[dict[str, Any]] = []
    for entry in timeline:
        if not isinstance(entry, dict) or entry.get("event") != "cross-referenced":
            continue
        source = entry.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        url = str(issue.get("html_url") or "")
        match = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
        if not match or match.group(1).lower() != repository.lower():
            continue
        if url in sources:
            continue
        if len(sources) >= _REMARK_SOURCES:
            break
        sources.append(url)
        comments = api(
            f"repos/{repository}/issues/{match.group(2)}/comments", per_page=100
        )
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict) or _is_bot(comment.get("user")):
                continue
            if not is_maintainer(comment, maintainers):
                continue
            # A markdown link's target is not prose: rokm's link to
            # `utils.py#L281` spent the quote before its reason was reached.
            text = _MARKDOWN_LINK.sub(r"\1", _flat(_unquoted(comment.get("body"))))
            hit = mention.search(text)
            if hit is None:
                continue
            found.append(
                {
                    **_row(comment, maintainers),
                    "quote": _sentence_around(
                        text, hit.start(), hit.end(), limit=400
                    ),
                    "source": url,
                    "url": comment.get("html_url") or url,
                }
            )
    return found
