# 0008. Observable runs

Date: 2026-09-02. Status: accepted.

## Context

The owner's complaint, in their words: the whole process feels like a black
box. You ask Mailman to find an issue and solve it, and some time later you get
a verdict. Nothing in between.

That was literally true, for three separate reasons.

`execute` ran every command through `subprocess.run(capture_output=True)`. An
agent is given an hour. For that hour the harness produced no output at all, so
a slow run and a hung run looked exactly alike.

The Codex adapter passed `--json`, so its event stream was captured in full -
116 KB of it for run `20260902T110923Z-2eb44e` alone - written to
`agent-executions/0001-primary.json` and read by nothing. Every command the
agent ran, every file it changed, and every message it wrote was on disk and
invisible.

The Claude adapter passed `--output-format text`, which returns only the final
message. Claude's thread was never recorded at all. Run
`20260902T111426Z-3fc769` blocked with `claude produced no report`, and the
reason - `Error: Reached max turns (30)` - was one unrendered line in its
execution record. Three minutes, thirty turns, no explanation.

## Decision

Treat the agent's own event stream as primary evidence, not as a byproduct.

`mailman/transcript.py` normalizes both vendors' streams onto one small event
type: what the agent said, what it ran, what came back, what it changed. Codex
JSON lines and Claude stream-json map onto the same events, so nothing
downstream knows which agent produced them. Malformed and non-JSON lines are
kept as output rather than dropped, because the max-turns error arrived as a
bare line and losing it is exactly the failure this record is about.

`execute` grows an `on_stdout_line` callback. When it is passed, the command
runs under `Popen` and each line is handed over as it arrives. A watchdog timer
still kills the process at the deadline, stdin is written from its own thread so
a large prompt cannot deadlock against a full stdout pipe, and an exception from
the callback is swallowed: a broken console must never cost the run its
evidence.

The Claude adapter moves to `--output-format stream-json --verbose`. The report
now comes from the final `result` event, which also carries a `subtype`. That
subtype becomes `AgentResult.stop_reason`, so `produced no report` can say
whether the agent ran out of turns or failed part-way.

`mailman show` renders a run from evidence on disk: the orchestration timeline,
each agent's transcript, and the reports. It reads what is already stored, so
the fifteen runs recorded before any of this existed became readable without
re-running anything.

## Consequences

The Claude adapter's output contract changed. A report is no longer raw stdout,
so anything reading `agent-executions/*.json` for a Claude run sees JSON lines
where it used to see prose. The stored `report` field is unaffected.

Streaming makes the harness noisier by default. That is the point, and `show`
exists for the case where the noise has already scrolled past.

Turning the stream on immediately exposed a defect that had been invisible for
the project's whole life: under `--permission-mode acceptEdits` the primary
agent can edit files but has every Bash call denied, with no one to approve it,
so it spends its turns on refusals. Filed as
[#16](https://github.com/wolfgang-aura/Mailman/issues/16) and fixed in the same
session with an explicit `--allowedTools` allowlist. Three blocked runs were
attributed to the agents being wrong about the code. At least one of them was
the harness never letting the agent run a test.

The allowlist is per role. The reviewer gets read-only git, because a reviewer
that cannot run `git diff` is as blind as an engineer that cannot run a test.
The primary agent also gets the usual test runners and the run's own
verification command, which is normally an absolute path into the run's
interpreter that no prefix rule would otherwise cover. The upstream denylist is
unchanged and still wins, so nothing here can push or open a pull request.

Verified live against the installed CLI on 2026-09-02: `git status --short`,
`git diff --stat`, and `python -c` all executed in a workspace where, before
the change, every one of them came back `This command requires approval`.

## Addendum: the budget a stop is read against

Naming `error_max_turns` in the blocking message is only half of
[#14](https://github.com/wolfgang-aura/Mailman/issues/14). A turn limit means
nothing without the limit, so `turn_budget` is now recorded on every agent
execution and named in the message: "it ran out of turns, so the work was cut
off mid-task (budget: 120 turns)". Codex has no such bound and records `None`,
which is itself the honest answer.

The default moves from 30 to 120. Thirty was never chosen for external work; it
was the value the first fixture happened to use. Run
`20260902T111426Z-3fc769` spent all thirty turns on `pytest-dev/pytest` and
produced nothing, while `20260902T111843Z-01b5f6`, given 120 on the same issue,
reached a verdict and failed on the merits of its patch. Failing on the merits
is a result. Running out of turns is not.
