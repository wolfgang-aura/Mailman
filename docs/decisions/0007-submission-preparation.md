# 0007. Policy-aware submission preparation

Date: 2026-09-02. Status: accepted.

## Context

The goal changed on 2026-09-02: Mailman should produce pull requests that can
actually be merged upstream, not only patches a human reads privately. Surveying
what recognizable projects now ask of contributors made the shape of that
problem clear, and it is not mainly a code-quality problem.

Read on that day, from each project's own text:

- `pytest-dev/pytest` welcomes AI-assisted contributions, appreciates a
  `Co-authored-by:` trailer crediting the tool, and closes purely agentic pull
  requests whose author cannot explain the change.
- `encode/starlette` permits AI tools and treats a duplicate pull request as a
  ban-level offence, so a search of open and closed pull requests, issues, and
  discussions is mandatory before opening one.
- `python-attrs/attrs` forbids unsupervised agentic tools and closes any pull
  request that lists an LLM as co-author.
- Pallets, covering Click, Flask, and Jinja, closes contributions that appear
  LLM-generated and is likely to block the author.
- `modelcontextprotocol/python-sdk` permits disclosed AI assistance but closes
  any outside pull request whose linked issue a maintainer has not assigned.

Two projects therefore want opposite things from the same commit trailer, and
one wants a search that leaves no trace in the diff at all. A single hard-coded
submission format cannot satisfy them, and a permissive default is dangerous:
preparing an attrs submission the pytest way would get the author banned.

The export step also had a known defect. The first external run's exported patch
carried an unrelated trailing-newline change that neither agent mentioned, and
`export-patch` did not look at the diff's contents at all.

## Decision

`mailman prepare-submission RUN_ID --policy FILE` checks a finished run against
one target's recorded policy and writes a submission package. It never contacts
the upstream repository.

### The policy is data, read from the project

A `TargetPolicy` records what one project asks: its stance, whether disclosure
is required, the trailer form it wants or forbids, whether it requires a linked
issue, a maintainer-assigned issue, or a duplicate search, and its changelog
convention. Every policy carries `policy_read_on`, because a policy is a claim
about another project on a particular day.

`stance` defaults to `unknown`, and `unknown` blocks preparation. An unread
policy must never behave like a permissive one. `forbidden` and `restricted`
block too, and name the condition rather than hinting at a workaround.

### Diff hygiene is a gate, not a note

The diff is parsed per file. A whitespace-only change, a trailing-newline-only
change, a binary file, an empty diff, or a change with no test file at all is a
blocking finding. These are the things a maintainer sees before they see the
fix.

### The human is the part that cannot be automated

Every permissive policy read here says the same thing in different words: a
person has to understand the change and answer for it. So the package includes
`accountability.md`, five questions the submitting human answers in their own
words before anything is opened, and the pull request body is a draft with the
argument left blank on purpose. Mailman supplies evidence, not the author's
voice.

### Refusal is the useful output

`prepare-submission` exits non-zero when anything blocks, and lists the blocking
codes. A run that is not `READY_FOR_HUMAN_REVIEW`, or that has no verification
Mailman ran itself and saw exit zero, cannot produce a ready submission.

## Consequences

Adding a target now means reading its policy and writing it down, which is
deliberate friction. Three policies ship as examples, including one, attrs, that
exists only as the case Mailman must refuse.

Opening the pull request stays manual and needs explicit human approval, as
[the human boundary](../../README.md#human-boundary) requires. Nothing here
pushes, comments, or opens anything.
