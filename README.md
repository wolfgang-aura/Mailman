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
Every other ending, including a missing or contradictory verdict, is `BLOCKED`. The command exits
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

It exits non-zero when no duplicate search is recorded, when a pull request is
open against the issue, or when closed attempts have not been acknowledged.
`mailman orchestrate` runs the same check first and blocks the run rather than
spending two agents on a target that was never worth having. An open pull
request refuses outright and no flag overrides it. Closed attempts usually mean
maintainers rejected the approach rather than the code, so read them, then pass
`--acknowledge-prior-attempts`.

Before running anything, find out what has already been tried:

```powershell
mailman duplicate-search RUN_ID --query "RaisesGroup check"
mailman prior-art RUN_ID
```

`duplicate-search` records a GitHub CLI search of the target's pull requests and
issues. `prior-art` then reads each earlier pull request and writes
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

## Human boundary

Mailman may eventually prepare branches, patches, and pull request text. It must not push, open a pull request, comment on an issue, or otherwise change an upstream project without a separate human approval step.

## Project status

The loop has run end to end against a repository this project does not own: [the first external issue](docs/runs/0004-first-external-issue.md), from an issue URL to an exported patch with no hand-written prompt. The revision cycle and a `BLOCKED` ending have live evidence too, in [the revision and blocking record](docs/runs/0005-revision-and-blocking-paths.md). Every run record keeps what went wrong, including the runs that failed. See [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md) for verified environment facts and [ROADMAP.md](ROADMAP.md) for the next thin slice.

## License

Apache-2.0. See [LICENSE](LICENSE).
