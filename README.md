# Mailman

Mailman is an open-source harness for testing coding agents on real software engineering issues. One agent owns the task. A second agent reviews the resulting repository state and evidence. Mailman runs important checks itself and stops before anything reaches an upstream project.

Mailman can create a private local run record, capture a GitHub issue into that record, clone a target repository at an exact base commit, install the target's dependencies outside its working tree, pin the executables a run may use, invoke one configured Codex or Claude CLI adapter, run the bounded primary and reviewer loop, capture process evidence with a timeout and redaction, enforce the workflow state machine, and write a reviewable patch package for a human. It does not publish anything to a target repository.

Two live models have completed the loop end to end. See [the two-model run record](docs/runs/0003-two-model-fixture.md).

## Why this exists

Agent prose is weak evidence. A useful engineering run needs the issue, exact base commit, diff, tests, command results, independent review, unresolved risks, and a human decision. Mailman treats those artifacts as the product.

The long-term outputs are:

- an interchangeable multi-model engineering harness;
- a public record of attempted open-source work, including failures;
- an evidence-driven engineering skill refined from those runs.

Read [the project direction](docs/project-direction.md) for the full intent and [the first architecture decision](docs/decisions/0001-foundation.md) for the initial boundaries.

## Local setup

Mailman requires Python 3.12 or newer. It currently has no runtime dependencies.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
mailman doctor
python -m unittest discover -s tests -v
```

Create a local run record:

```powershell
mailman init-run `
  --repository https://github.com/owner/project `
  --issue https://github.com/owner/project/issues/123 `
  --base-commit 0123456789abcdef0123456789abcdef01234567 `
  --primary codex `
  --primary-model MODEL_ID `
  --reviewer claude `
  --reviewer-model MODEL_ID
```

Both model ids are required. A run that accepted a vendor default recorded
`null` and could not say afterwards which model wrote the patch, which makes two
runs incomparable and the record wrong about its own inputs.

Clone the recorded repository into the ignored run directory and detach it at the exact base commit:

```powershell
mailman prepare-workspace RUN_ID --timeout 600
```

Mailman records the clone and checkout commands in `workspace.json`. A second call reuses the workspace only when its origin, commit, and clean state still match. It preserves the original command evidence and increments a reuse counter.

Capture the issue text into the run record:

```powershell
mailman fetch-issue RUN_ID
```

This reads the run's own issue URL with the GitHub CLI and writes `issue.md`.
Comments, linked pull requests, and any accepted upstream fix are left out, so a
replayed historical issue cannot leak its own answer. On a host without `gh`,
pass `--from-file issue.txt --title "..."` to record text a human transcribed;
the record keeps the source path and its SHA-256 digest.

Turn the captured issue into prompts:

```powershell
mailman build-prompts RUN_ID --verification "python -m unittest discover -s tests"
```

This writes `primary-task.md` and `reviewer-task.md` into the run directory, and
`mailman orchestrate` uses them when no prompt is passed. Both prompts state the
base commit, the verification command Mailman runs itself, and the rule that an
agent's own exit code proves nothing.

Install the target repository's dependencies:

```powershell
mailman prepare-environment RUN_ID --plan .\plans\python.json
```

A plan is a JSON file with `schema_version` 1 and a list of named steps. Each
step is a command list that runs without a shell, with `{environment}`,
`{workspace}`, and `{run}` expanded. The environment lives in the run directory
rather than the checkout, because the primary agent has to start from a clean
workspace at the base commit. Preparation that changes the workspace fails with
that reason instead of surfacing later as a refused orchestration. What it
compares is the workspace before and after, not whether the workspace is clean,
so a finished run whose workspace holds its own candidate can still rebuild its
environment. The primary role keeps its own cleanliness requirement.

```json
{
  "schema_version": 1,
  "steps": [
    {
      "name": "create-virtualenv",
      "command": ["python", "-m", "venv", "{environment}"],
      "working_directory": "run"
    },
    {
      "name": "install-target",
      "command": ["{environment}/Scripts/pip.exe", "install", "-e", "."]
    }
  ],
  "register": [
    {"name": "python", "executable": "{environment}/Scripts/python.exe"}
  ]
}
```

Each `register` entry is probed and digest-pinned like `probe-tool`, so the
prepared interpreter is the one the agents are told to use. `{environment}` also
expands inside a `verify` or `orchestrate` command, which lets verification run
against the prepared interpreter instead of the host's.

Mailman writes live run data under `.mailman/`, which Git ignores. Capture a verification command with:

```powershell
mailman verify RUN_ID -- python -m unittest discover -s tests -v
```

The command runs without a shell, has a timeout, and records its exit code, duration, and redacted output. `mailman export-patch` is what turns a finished run into a shareable artifact. Do not commit `.mailman/` by force.

Register a runtime before an agent run:

```powershell
$pythonPath = (Get-Command python).Source
mailman probe-tool RUN_ID --name python --executable $pythonPath
```

The probe records the resolved path, version result, and SHA-256 digest in the private run directory. Mailman adds verified tool paths to the saved agent prompt and refuses to use a binary whose digest changed after probing. Host verification is still authoritative because a host-accessible executable may be unavailable inside an agent sandbox.

Run one configured agent against an already prepared Git workspace:

```powershell
mailman run-agent RUN_ID `
  --role primary `
  --prompt .mailman\runs\RUN_ID\primary-prompt.md `
  --timeout 3600
```

`--workspace` defaults to the workspace `prepare-workspace` wrote for the run,
`.mailman\runs\RUN_ID\workspace`. Pass it only for a workspace somewhere else.

Mailman checks that a primary workspace is clean and exactly at the run's base commit. A reviewer workspace may contain uncommitted changes or candidate commits descended from that base. Mailman saves the exact prompt, including registered tool paths, then sends it through stdin. It records the process result and leaves workflow status unchanged. A zero process exit does not prove that the patch is correct. Review and independent verification remain separate gates.

Run the bounded primary and reviewer loop:

```powershell
mailman orchestrate RUN_ID `
  --primary-prompt .\prompts\primary.md `
  --reviewer-prompt .\prompts\reviewer.md `
  --agent-timeout 3600 `
  -- python -m unittest discover -s tests
```

The loop runs the primary agent, verifies the result itself, runs the reviewer,
and reads one `MAILMAN-VERDICT: APPROVE` or `MAILMAN-VERDICT: REVISE` line from
the review report. It allows at most one revision, verifies again after
approval, and only then reaches `READY_FOR_HUMAN_REVIEW`. A verification that
fails after a primary stage spends that same single revision: the agent is
handed the failing command and its output and gets one attempt to fix it.
An `APPROVE` also requires the reviewer to have written
`MAILMAN-VERIFICATION: RAN`, meaning it ran the run's verification command
itself and saw it finish. `BLOCKED` is the honest answer when a sandbox refused
to start the interpreter, and it carries no penalty, but it cannot accompany an
approval: that reviewer read the candidate rather than checking it, and the loop
must not report a two-agent check it did not get. A missing claim blocks the
same way a missing verdict does, and a claim of `RAN` from a reviewer whose
transcript shows no command at all is refused as a contradiction. A `REVISE`
from a blocked reviewer still counts, since a finding from a careful read is
worth acting on.

Counting commands alone does not answer this, which is why the reviewer is asked
outright. In the run that started this, the Codex reviewer ran three commands:
two file reads that succeeded and one `pytest` that exited 1. It then said in
prose that it made no test-pass claim, and approved. Every other ending,
including a missing or contradictory verdict, is `BLOCKED`. The command exits
`0` only for a run that is ready for a human, and writes each step to
`orchestration.json`. See [the orchestration decision](docs/decisions/0004-bounded-orchestration.md)
for the reasoning.

Claude's turn budget defaults to 120 (`--max-turns`) and is recorded with every
execution. Thirty, the earlier default, never once finished an external target:
one run spent all thirty on an unfamiliar codebase and wrote no report. When an
agent stops at its limit, the blocking message says so and names the budget it
hit, instead of reporting only a missing report.

A Codex run can also name how hard the model should think:

```powershell
mailman run-agent RUN_ID --role reviewer --model gpt-5.6-luna --reasoning-effort max
```

The effort is validated against the CLI's own catalog before launch and stored
on the execution record, because a review at `max` and one at the default are
not the same evidence.

Each execution record stores the model the CLI itself reported using, next to
the one the run requested. Claude names it in its init event; Codex names none,
which the record says as `not reported` rather than as a null that could mean
either unset or unknown.

Every execution record also lists the instruction sources that were in effect:
the operator's own `CLAUDE.md`, `AGENTS.md`, settings, and skill files, each with
a SHA-256 digest and whether Mailman suppressed it. Neither CLI can be told to
ignore all of them, so a run that is affected by machine-local text at least says
which text, and the review page reports how many were read.

Agent executables are resolved at launch. Register one for a run with
`mailman probe-tool RUN_ID --name claude --executable PATH` when it is not on
`PATH`, or when a run must be pinned to an exact binary.

Review a finished run as one page:

```powershell
mailman review RUN_ID
```

This writes `review.html` into the run directory and opens it. The page leads
with the decision a human has to make: run status, reviewer verdict, and whether
Mailman's own gate passed, then the diff, then the evidence behind it. It is a
single file with no network dependency, so it opens from disk anywhere. Pass
`--output` to write it elsewhere and `--no-open` to write it without a browser.
`DESIGN.md` records the visual language it uses.

While the loop runs it prints every command, edit, and message the agent
produces, and appends the same lines to `agent-executions/ROLE-live.log` in the
run directory. Read what a run did, during or after it:

```powershell
mailman show                 # every recorded run, with its status and agents
mailman show RUN_ID          # timeline, agent transcript, and reports
mailman show RUN_ID --full   # with the message bodies and command output
```

The transcript is rebuilt from evidence already on disk, so runs recorded
before this existed are readable too. See
[the observability decision](docs/decisions/0008-observable-runs.md).

Draft a retrospective for a finished run:

```powershell
mailman retrospective RUN_ID
```

The command reads the run directory and writes `retrospective.json` and
`retrospective.md` beside the rest of the run evidence. It seeds only what
Mailman observed, a failed verification, an agent that exited zero without a
report, a reviewer verdict of `REVISE`, and leaves twelve questions empty for a
person to answer. It refuses to overwrite an existing retrospective without
`--force`. Lessons reach the reusable skill only through the registry in
[`knowledge/`](knowledge/README.md). See
[the knowledge flywheel decision](docs/decisions/0005-knowledge-flywheel.md).
Write the human review package for a finished run:

```powershell
mailman export-patch RUN_ID
```

This produces `changes.diff`, `summary.md`, `pull-request.md`, and `export.json`
in the run's `export` directory. The diff covers new files as well as edited
ones, and it is never rewritten: a diff matching a credential pattern stops the
export instead of being silently redacted. The command refuses a run that is not
`READY_FOR_HUMAN_REVIEW` unless `--allow-unfinished` is passed, which is how a
`BLOCKED` run's partial work gets read. Nothing is pushed, and the pull request
text is a draft for a human to accept, edit, or discard.

A run refuses to start against an issue somebody else has already claimed:

```powershell
mailman check-target RUN_ID
```

It exits non-zero when no duplicate search is recorded, when nothing is recorded
about how the target hands out work, when nobody has read the issue's own
comments, when no reproduction is recorded, when the reported bug did not
reproduce at the base commit, when a pull request is open against the issue,
when a pull request against it was merged, when somebody has claimed the issue
in a comment, or when closed attempts have not been acknowledged.
`mailman orchestrate` runs the same check first and blocks the run rather than
spending two agents on a target that was never worth having.

Prior attempts are read three ways, because a row's state changes what it means.
An open pull request refuses outright and no flag overrides it. A merged one
refuses under the same `already-fixed-upstream` code `prepare-submission` uses,
and no flag overrides that either: nobody rejected it, it is what the repository
ships. Only genuinely closed attempts are acknowledgeable, and those usually
mean maintainers rejected the approach rather than the code, so read them, then
pass `--acknowledge-prior-attempts`.

Judge a repository before any run is spent on it:

```powershell
mailman screen-target OWNER/REPO
```

Six gates, in the order a candidate actually dies in. Freshness runs first
because it kills most of them for two API calls. Stars run last because they
have never changed a decision: `OpenBB-finance/OpenBB` has 72.6k of them and has
merged nothing from outside in six weeks.

1. **Freshness.** Outside human merges in the window, bots excluded by account
   type and by name. Fails when nobody outside has merged, when one person wrote
   every outside merge in ninety days, or when one person wrote 80% of them.
2. **CI.** A workflow that runs a test suite, not only publish and lint. The
   pattern is deliberately wide, since a false negative here rejects a good
   candidate: `ccxt/ccxt` runs its Python suite as `npm run test-base-rest-py`.
3. **Pure Python.** Fails on a Cython or Rust build, which this host has no
   compiler for, and on a repository where Python is a minority of the source
   and may be generated from the majority language.
4. **Policy.** Reads the contributing guide for a rule that closes an
   AI-assisted pull request unread. Matched narrowly on purpose, because "AI"
   appears in every model library's guide.
5. **Saturation.** How many unassigned open issues have no open pull request
   naming them, and how old they are.
6. **Stars.** Reported, never decisive.

Every gate prints its numbers next to the threshold that judged them, and the
verdict is written to `screens/OWNER__REPO.json` under the data root so a
rejected candidate is not screened twice. Pass `--refresh` to re-read one. The
command exits `0` for a repository worth a run and `1` for a rejected one.

The screen does not judge assignment protocol; that is `target-intel`, one level
down, once a run exists.

Read who has already claimed the issue, in the issue's own comments:

```powershell
mailman claims RUN_ID
```

Every other prior-art gate reads pull requests, which misses the earlier and
more common form: somebody saying "I'd like to work on this" and not having
opened one yet. On `openai/openai-agents-python` #4775 the duplicate search was
empty and `check-target` called the target unclaimed while the second comment
was a claim with a three-point scope plan under it.

The command records `claims.json` and tells three states apart. A GitHub
assignee refuses outright. A maintainer answering a claim by handing the work
over refuses outright too, because that is the assignee field written in prose.
An offer nobody answered is worth a human reading rather than a hard stop, so it
is the one state `--acknowledge-claims` clears; `--acknowledge-prior-attempts`
does not, because they answer different questions. A comment that only asks
after the bug is not a claim, and neither is "PRs welcome". The comments are
read for this judgement alone and never written into `issue.md`, which is the
same rule that keeps a merged pull request's diff out of an agent's prompt.

Read how the target actually hands out and merges outside work, before a run is
spent on it:

```powershell
mailman target-intel RUN_ID
```

It counts outside merges by human accounts only, subtracts the open issues that
an open or merged pull request already references, quotes the rules a bot
enforces from the bot's own comments, and traces the most recent outside merges
back to the threads that won them, with the winning author's comments marked. It
reports counts next to their denominators rather than verdicts, and it does not
choose the target. `check-target` refuses without the record, and
`--acknowledge-prior-attempts` does not clear that refusal. See
[the target intel decision](docs/decisions/0009-target-intel.md).

Then prove the bug is still there, after `prepare-environment` and before any
agent runs:

```powershell
mailman reproduce RUN_ID -- {environment}\Scripts\python.exe -m pytest tests/test_x.py
```

The command runs in the prepared workspace at the base commit, records
`reproduction.json` beside the run's other evidence, and exits non-zero when the
reported behaviour did not happen. By default the bug is a command that fails.
Where a fixed and an unfixed tree both fail and differ only in what they print,
say so with `--expect-output`, repeatable, and `--forbid-output`; where the bug
has a specific exit code, `--expect-exit-code`. A timeout is recorded as a
timeout, never as a reproduction.

Where the issue has no runnable reproducer, record the reading instead:

```powershell
mailman reproduce RUN_ID --not-machine-reproducible --note "the report is a screenshot"
```

That warns on every later `check-target` rather than blocking, which is what a
human reading is worth. A bug that no longer reproduces blocks outright and no
flag overrides it: the right response is to abandon the run.

This gate exists because run `20260903T045152Z-369e77` passed every other one.
pytest-dev/pytest #14964 was a precise, same-day, unclaimed regression report,
the environment built in four steps, and the bug had already been fixed by a
commit that landed after the 9.1.1 tag. Only a hand-built reproduction caught
it, after the cost had been paid. See
[issue #37](https://github.com/wolfgang-aura/Mailman/issues/37).

Before running anything, find out what has already been tried:

```powershell
mailman duplicate-search RUN_ID --query "RaisesGroup check"
mailman prior-art RUN_ID
```

`duplicate-search` tries three methods against the target's pull requests and
issues, runs all of them, and unions what they return. Two use GitHub's search
index; the third lists every open item and matches locally on the query terms
and the issue number, in the title, body, and branch name. That third method is
the one that counts. GitHub's index refuses some repositories outright,
`encode/starlette` among them, and `gh pr list --search` there returns nothing
even for a single token four open titles contain. The record carries `complete`,
which is false when the listing failed, and `prepare-submission` refuses to
clear a run on a search that did not complete.

Not every match is a duplicate. A row blocks outright when it names the run's
issue or when GitHub's index returned it, because the index ANDs every query
term. A row the local matcher found on one shared word is recorded as a
candidate instead, and `prepare-submission` holds the run until a human has read
those and said so:

```powershell
mailman acknowledge-duplicates RUN_ID --note "read all four, none touch the broker"
```

The acknowledgement pins the exact rows it covers. A later search that turns up
anything new blocks again, so it cannot become a standing waiver. Nothing here
can tell a topic overlap from a duplicate, and the split says which judgement
the harness is making and which one it is handing to you.

This second gate was added after the first one over-corrected. Against
`kernc/backtesting.py` #939 the repaired search recorded twenty-two matches,
including the run's own issue and eighteen open issues that shared the word
"price". None was a duplicate. See
[issue #31](https://github.com/wolfgang-aura/Mailman/issues/31).

This gate was added after it failed. On 2026-09-03 a finished run against
`encode/starlette` #3458 was cleared for filing while four open pull requests
already fixed that issue, three touching the same two files. Every search method
had either errored or matched nothing, and the record still said `success`. See
[issue #30](https://github.com/wolfgang-aura/Mailman/issues/30).

`prior-art` then reads each earlier pull request and writes
`prior-art.md`, which `build-prompts` folds into both agent prompts: what each
attempt claimed, which files it touched, and what a maintainer said when closing
it. A merged pull request's body and files are withheld, because handing an
agent the accepted fix measures nothing. A closed one is the opposite case: on
`pytest-dev/pytest` #14324, three attempts were closed before Mailman ever ran,
one of them with the comment "Closing as unattended undisclosed ai".

Check a finished run against the target project's own contribution policy:

```powershell
mailman prepare-submission RUN_ID --policy examples/target-policies/pytest.json
```

This writes `pull-request.md`, `accountability.md`, and `submission.json` in the
run's `submission` directory, and exits non-zero when anything blocks. It reads
the diff for noise a maintainer would see first, whitespace-only hunks and
trailing-newline changes among them, and it checks the target's own rules:
disclosure, the trailer form that project wants or forbids, a required linked
issue, a mandatory duplicate search, and its changelog convention. A policy
whose stance is `unknown` blocks, because an unread policy must not behave like
a permissive one. See
[the submission preparation decision](docs/decisions/0007-submission-preparation.md)
and [the shipped policies](examples/target-policies/README.md).

The draft it writes follows [the pull request standard](docs/pull-request-standard.md),
which covers the step Mailman does not automate: how a person turns the patch
package into a pull request a maintainer wants to read.

## Human boundary

Mailman may eventually prepare branches, patches, and pull request text. It must not push, open a pull request, comment on an issue, or otherwise change an upstream project without a separate human approval step.

## Project status

The loop has run end to end against a repository this project does not own: [the first external issue](docs/runs/0004-first-external-issue.md), from an issue URL to an exported patch with no hand-written prompt. The revision cycle and a `BLOCKED` ending have live evidence too, in [the revision and blocking record](docs/runs/0005-revision-and-blocking-paths.md). Every run record keeps what went wrong, including the runs that failed. See [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md) for verified environment facts and [ROADMAP.md](ROADMAP.md) for the next thin slice.

## License

Apache-2.0. See [LICENSE](LICENSE).
