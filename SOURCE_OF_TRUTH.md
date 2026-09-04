# Source of truth

Last verified: 2026-09-04 in `Asia/Singapore`.

## Repository

- Canonical remote: `https://github.com/wolfgang-aura/Mailman.git`
- Default branch: `main`
- The public remote contains the foundation on `main`.
- License: Apache-2.0.
- Foundation deployment: commit `c1cc179` passed GitHub Actions on Python 3.12 and 3.14 in run `33546831897`.
- CLI adapter deployment: commit `29a5a53` passed GitHub Actions on Python 3.12 and 3.14 in run `33548539986`.
- Toolchain deployment: commit `318c9fe` passed GitHub Actions on Python 3.12 and 3.14 in run `33549213202`.
- Workspace deployment: commit `91e84ae` passed GitHub Actions on Python 3.12 and 3.14 in run `33549771368`.
- Orchestration deployment: commit `929caf5` passed GitHub Actions on Python 3.12 and 3.14 in run `33552625334`.
- Issue #9 deployment: commit `a371be1` passed GitHub Actions on Python 3.12 and 3.14 in run `33558633354`.
- Knowledge flywheel deployment: commit `d2be47a` passed GitHub Actions on Python 3.12 and 3.14 in run `33592165392`.
- External issue deployment: commit `78d048e` passed GitHub Actions on Python 3.12 and 3.14 in run `33596869316`. It carries issue ingestion, environment preparation, patch export, and the first external run records.
- Run record deployment: commit `46d0862` passed GitHub Actions on Python 3.12 and 3.14 in run `33596947293`.
- Observability deployment: commit `d72d645` passed GitHub Actions in run `33627419317`. It carries the
  agent transcript normalizer, live streaming from the executor, Claude on `stream-json`, and `mailman show`.
- Bounded-loop deployment: commit `9fc4805` passed GitHub Actions in run `33641545669`. It carries the
  claimed-issue refusal (#15), the named turn budget and its new default of 120 (#14), and one revision
  granted when the post-primary verification fails (#13).
- Issue-sweep deployment: commit `299b1c8` passed GitHub Actions on Python 3.12 and 3.14 in run
  `33670366489`. It carries the fixes for #18, #21, #22, #23, #24, #25, #27 and #28, plus the
  recording halves of #20 and #26.
- Duplicate-search deployment: commit `d1d4a98` passed GitHub Actions on Python 3.12 and
  3.14 in run `33677777743`. It carries the pull request standard and the fix for #30, a
  duplicate search whose failed methods were recorded as a successful empty search.
- Duplicate-precision deployment: commit `ed933ea` passed GitHub Actions in run `33680128212`.
  It carries the fix for #31: the run's own issue is no longer a duplicate of itself, matches
  are split into index-backed hits that block and listing hits a human must read, and
  `mailman acknowledge-duplicates` records that reading against the exact rows it covers.
- Duplicate-state deployment: commit `eb85694` passed GitHub Actions in run `33681805068`.
  It carries the fix for #32, so a closed prior attempt is prior art rather than an
  unclearable duplicate, and a merged one blocks as `already-fixed-upstream`. It also
  records the `langchain-ai/langchain` target policy.
- Prior-art scoping deployment: commit `54206df` passed GitHub Actions in run `33682244519`.
  It carries the fix for #33 and its follow-up: `prior-art` reads the rows that are about
  the issue whatever their state, and only open or merged rows stop a run.
- Environment plan deployment: commit `d777218` passed GitHub Actions in run `33682705844`.
  It adds the `langchain-core` environment plan.
- Verification-resolution deployment: commit `41d9eb7` passed GitHub Actions on Python 3.12
  and 3.14 in run `33718702205`. It carries the fix for #39, so a verification command typed
  as a bare `python` resolves through the run toolchain instead of running whatever is first
  on PATH, and adds the `pytest` and `starlette` environment plans.
- No-test acknowledgement deployment: commit `87bbd93` passed GitHub Actions in run
  `33719939852`. It carries the fix for #40: `mailman acknowledge-no-test` lets a run answer
  the `no-test-change` gate with recorded evidence, pinned to the paths the diff touches.
- Run record deployment: commit `9e1174f` passed GitHub Actions in run `33720398351`. It adds
  `docs/runs/0008-starlette-3497-submission-ready.md`.
- Target intel deployment: commit `77f094c` passed GitHub Actions in run `33735205372`. It
  carries `mailman target-intel`, the `check-target` precondition that refuses a run against a
  repository nobody has read, and the first six lessons in `knowledge/lessons.json`.
- Reproduction gate deployment: commit `5009949` passed GitHub Actions in run `33766732724`.
- Merged-attempt deployment: commit `133d7dc` passed GitHub Actions in run `33771151452`.
  It carries the fix for #38: `check-target` splits prior attempts three ways, and a merged
  one blocks under `already-fixed-upstream` rather than being reported as a rejection that
  `--acknowledge-prior-attempts` clears.
- Comment-claim deployment: commit `156b8cb` passed GitHub Actions in run `33772783849`.
  It carries the fix for #36: `mailman claims` reads the target issue's own thread, and
  `check-target` refuses without that record, on an assignee, on a maintainer handing the
  work over, and on an unanswered claim until `--acknowledge-claims` is passed.
- Reviewer-execution deployment: commit `b68f886` passed GitHub Actions in run `33786866250`.
  It carries the fix for #20: an APPROVE from a reviewer whose transcript shows no command
  blocks the run instead of clearing it. REVISE is unaffected.
- Verification-claim deployment: commit `3bd94b8` passed GitHub Actions in run `33791836068`.
  It carries the real fix for #20: an APPROVE requires `MAILMAN-VERIFICATION: RAN`. Exercised
  live against a Codex reviewer in both states, which wrote BLOCKED under a read-only sandbox
  and RAN under workspace-write, and declined to approve in the blocked case.
- Repository screen deployment: commit `cf2dd1f` passed GitHub Actions in run `33790613015`.
  It carries #35: `mailman screen-target OWNER/REPO`, six gates, verdict cached under
  `screens/`. Exercised live against six repositories from the hand-screened table.
  It carries `mailman reproduce` and the two `check-target` refusals behind it.
- Defect-report deployment: commit `65a7b4d` passed GitHub Actions in run `33797195813`.
  It carries #45: `init-run --defect-report PATH` as an alternative to `--issue`, exactly one
  of the two required. `fetch-issue` renders the file through the same capture boundary and
  `claims` records that no thread exists. The duplicate search and the reproduction stay
  mandatory and carry the whole evidence burden. 420 unit tests, up from 412.
- Superseded-merge deployment: commit `bd7c5bd` passed GitHub Actions in run `33802398911`.
  It carries #46: prior art records `mergeCommit`, and a merged duplicate is cleared when its
  merge commit is an ancestor of the base commit and the reproduction failed at that same
  commit. `check-target` reports `merged-fix-already-in-base` and `prepare-submission` makes
  it a non-blocking finding. Also `examples/target-policies/ffn.json`, stance `unknown`, the
  first target with no written policy at all. 431 unit tests, up from 420.
- Handoff-gate deployment: commit `4023709` passed GitHub Actions in run `33836739688`.
  It carries #47: `mailman handoff` prints the whole body in one block with the single `gh`
  command that posts it, records the body's SHA-256, and emits `--body-file` so the previewed
  bytes are the posted bytes. `mailman handoff-check` re-hashes the file and exits non-zero
  once it changed after the preview. A first-person read-or-tested claim exits non-zero and
  names the line. 447 unit tests, up from 431.
- Provenance-gate deployment: commit `23107ba` passed GitHub Actions in run `33855131252`.
  It carries #48. `screen-target` now runs seven gates, with provenance reported first: a
  repository passes at a year old with 500 stars, or at ten outside authors in ninety days,
  and a fork is refused outright. It reuses the freshness gate's author count, so it costs no
  extra API call. Re-screened live on 2026-09-04: `pydantic/pydantic-ai` 44 authors,
  `langchain-ai/langchain` 34, `pmorissette/ffn` 11, all passing. An environment plan step may
  start only a Python interpreter or `git`; `check_executable` runs in `load_plan` and again on
  the substituted command in `prepare_environment`. This is not isolation. A hostile `setup.py`
  in a repository that clears the gate still executes as the invoking user, and no sandbox
  exists. 466 unit tests, up from 447.
- Screening-gate deployment: commit `fc0ccae` passed GitHub Actions in run `33837443444`.
  It carries #42, #43 and #44. Freshness fails when every merge in the window is by one author
  who wrote 35% or more of the pattern-window merges over a sample of at least eight, and the
  gate line names the authors counted and the bots excluded. The policy gate reads three rules
  rather than one, and `TargetPolicy.requires_own_words` blocks a generated body in
  `prepare-submission`. The pure-python gate reads `[build-system].requires`, failing a
  compiling back end and passing a wheel-only hook with the new `source-tree` environment plan.
  457 unit tests, up from 447.

### Reproduce gate, live-verified 2026-09-04

Run `20260903T190542Z-65ac37` against `pmorissette/bt` #461 refused with `bug-not-reproduced`.
0 failures in 1200 randomized trials at base `db6163e`; the same reproducer gives 3 failures
in 256 trials at `2a607df^`, the commit before merged PR #530. That control is what makes the
non-reproduction evidence. No agent ran. Issue #37 closed on it.

### Warning: the history was rewritten on 2026-09-02

Between 20:38 and 20:45 UTC every commit on `main` changed its SHA, on both the local clone
and `origin`, without this session asking for it. Nothing was lost: each new commit's tree
matches the old one byte for byte (`330d6b7` and `ed933ea` both point at tree `7d79d6ea`),
and author, committer, and dates are identical. Only the commit objects differ, which means
the rewrite began at an ancestor and propagated. `origin/hoplite/mytilene-39fb53cc` moved
from `21f47d8` to `d6f0da4` in the same period. The old commits are still reachable through
the GitHub API but not from any local ref. The cause is unidentified. Treat any SHA recorded
before 2026-09-02 20:45 UTC as unresolvable locally, and check `git log` against this file
before trusting either.

## Development environment

- Host operating system: Windows.
- Verified Python: CPython 3.14.3. The project supports Python 3.12 and newer.
- Codex CLI: installed at `%APPDATA%\npm\codex.cmd` and authenticated. It executed a real fixture on 2026-09-02.
- Claude CLI: installed at `%APPDATA%\npm\claude.cmd` and authenticated. It executed a real review on 2026-09-02. Its adapter flags are verified against the installed build: `--print`, `--input-format`, `--output-format`, `--permission-mode` (`acceptEdits`, `plan`), `--disallowedTools`, `--model`, and the undocumented but accepted `--max-turns`.
- Both CLIs were absent from the host earlier on 2026-09-02 and were installed with `npm install -g @anthropic-ai/claude-code @openai/codex` from an independent terminal. Before that install, a Claude Code agent session reported both as present while the host did not have them. Confirm agent CLI presence from an independent terminal, never from inside an agent session.
- GitHub CLI: installed and authenticated. The user authorized the first public push on 2026-09-02.

- Codex's Windows sandbox will not execute any binary under the user profile. Verified 2026-09-02:
  the same `python --version` fails from `AppData\Local\Python` and succeeds from
  `C:\ProgramData\mailman-python`, while `git` in Program Files runs either way. A virtual
  environment inside a run directory works as long as its **base** interpreter sits outside the
  profile, because only the base process creation is refused.
- `C:\ProgramData\mailman-python` holds a copy of CPython 3.14.3 staged for exactly that reason.
  It needs no administrator rights to create. `mailman doctor` reports "agent-runnable python" and
  names the problem when the current base interpreter is inside the profile.

## Target selection

Set by the repository owner on 2026-09-02, after the first external run used
`ayukhno/autosound-tcc`, a repository with almost no stars or usage.

- External targets must be projects a reader would recognize as real and
  maintained, not whatever repository happened to have an open issue.
- Small issues are fine. Obscure projects are not. The size of the issue and the
  standing of the project are separate choices.
- A candidate is only viable here if a fresh clone is clean, its test suite runs
  on Windows without native, GPU, or Qt dependencies, and a specific test can
  serve as a verification gate.
- Vet a candidate against those checks before any agent runs. Two of the first
  three candidates failed them.

Upstream contribution policies observed on 2026-09-02, which constrain any future
submission:

- Pallets, covering Click, Flask, and Jinja, states that a contribution appearing
  to be LLM-generated will be closed and the author likely blocked.
  See `https://palletsprojects.com/contributing/llm-ai`.
- `python-attrs/attrs` forbids unsupervised agentic tools and refuses any pull
  request carrying an LLM co-author trailer. See its `.github/AI_POLICY.md`.
- `Textualize/rich` has effectively closed external pull requests, citing poor
  quality AI submissions.
- `pypa/packaging` carries no such policy in its repository.

Vetting a target now includes reading its contribution and AI policy before an
agent runs, not before a pull request is opened.

Three further screens, established by hand on 2026-09-03 across ninety candidate
repositories and recorded on
[#35](https://github.com/wolfgang-aura/Mailman/issues/35):

- **Freshness must exclude bots.** `author_association` treats dependabot as a
  `CONTRIBUTOR`. `PyCQA/bandit` scored one outside merge in fourteen days on that
  basis and has merged no human outside pull request since May 2026. Filtering on
  account type also takes `psf/black`, `psf/requests`, `encode/httpx`,
  `fastapi/typer` and `Textualize/textual` to zero. Repositories that pass, with
  human outside merges in the fortnight to 2026-09-03: `scrapy/scrapy` 48,
  `celery/celery` 40, `sqlfluff/sqlfluff` 24, `pydantic/pydantic` 11,
  `redis/redis-py` 11, `pypa/pipx` 11, `pdm-project/pdm` 9, `pypa/virtualenv` 9,
  `pytest-dev/pytest` 7, `encode/starlette` 4.
- **Saturation decides.** Every fresh bug in every recognizable Python repository
  already has a pull request, usually within a day or two. `sqlfluff/sqlfluff`
  #8354 had four attempts, all closed; `python-jsonschema/jsonschema` #1511 has
  seven. Subtracting every issue number mentioned by the last 200 to 400 pull
  requests is the cheap way to compute this: two REST calls per repository rather
  than one search call per issue.
- **Targets screen for automated accounts.** `sqlfluff/sqlfluff` runs an
  `agentscan` workflow that closes a pull request when the account looks
  automated, whatever the content says. `pytest-dev/pytest` uses an "ai rejected"
  label. `PyCQA/bandit` appears to refuse pull request creation from
  non-collaborators outright.

The freshness, saturation and enforcement screens are now mechanical: `mailman
target-intel` computes all three for one named repository and `check-target`
refuses to start without the record. Choosing which repositories to feed it is
still a hand pass, which is what
[#35](https://github.com/wolfgang-aura/Mailman/issues/35) asks for.

An issue's age says nothing about whether its bug still exists. pytest #14964 was
a precise same-day report with no pull request against it and was already fixed on
`main`; only a hand-built reproduction at the base commit caught it, after the
environment had been built. That reading is now mechanical and blocking:
`mailman reproduce` records it and `check-target` refuses without it.
[#37](https://github.com/wolfgang-aura/Mailman/issues/37) is fixed and exercised
through the CLI against a scratch run, including the shape where a fixed tree
still fails and differs only in its counts. It has not yet run against a live
target.

Vetting also includes searching the target's existing pull requests for the same
change before a run starts. On 2026-09-02, three runs were spent on
`pytest-dev/pytest` issue #14324 before anyone searched; the issue already had
four pull requests against it, three closed unmerged and one open. An open issue
is not an unclaimed issue. See `docs/runs/0006-pytest-14324-three-blocked-runs.md`.

Projects whose policies permit AI-assisted contributions, read on 2026-09-02:
`pytest-dev/pytest` welcomes them with human accountability and appreciates a
`Co-authored-by` trailer; `encode/starlette` permits them and makes a duplicate
search mandatory; `pydantic/pydantic` welcomes them. `modelcontextprotocol/python-sdk`
permits disclosed assistance but closes any outside pull request whose issue a
maintainer has not assigned.

## Authority and artifact boundaries

- The target repository checkout and machine-observed command results outrank agent self-reports.
- Live runs, agent transcripts, cloned target workspaces, and raw logs stay under `.mailman/` and are not public artifacts.
- Tracked examples must contain synthetic or human-reviewed, sanitized data.
- Upstream pushes, pull requests, comments, issue changes, and branch changes require explicit human approval.

## Current capability

The code can initialize a run record, capture a GitHub issue into it, generate the primary and reviewer prompts from that issue, prepare an isolated repository at the exact base commit, install the target's dependencies outside its working tree, register digest-pinned toolchain executables, enforce allowed state transitions, run one configured Codex or Claude CLI adapter, run the bounded primary and reviewer loop, execute a verification command without a shell, export a reviewable patch package, redact common token formats, and report missing local tools. `run-agent` requires a clean primary workspace at the exact base commit. Reviewer workspaces may contain changes descended from that base. The command stores private execution evidence and never changes workflow status by itself.

`mailman orchestrate` runs the bounded loop: primary work, independent verification, review, at most one revision, a second review, final independent verification, then `READY_FOR_HUMAN_REVIEW`. Approval requires a parsed `MAILMAN-VERDICT: APPROVE` line and a passing verification that Mailman runs itself. A missing, unparseable, or contradictory verdict, a second revision request, a failed verification, an agent that exits zero without a report, or an unexpected error all end the run at `BLOCKED`. See `docs/decisions/0004-bounded-orchestration.md`.

Agent executables are resolved before launch and may be pinned per run through `probe-tool` under the agent's name. A missing executable now names the tool and the command that would register it.

The Codex adapter completed a disposable fixture on 2026-09-02. It produced the expected one-line patch under the elevated native Windows sandbox. A later private run registered a bundled Python executable in the run toolchain, and Codex used it to pass the unittest. Mailman then passed the same test independently with the same executable.

The bounded loop is covered by 75 unit tests with scripted agents, and it completed a live two-model run on 2026-09-02. Private run `20260901T201921Z-0b85ed` used Codex as primary and Claude as reviewer on a disposable `slugify` fixture that started with 2 of 3 tests failing. Codex changed one line in 43.4s, Mailman's own verification passed, Claude reviewed in 29.0s and returned a parsed `APPROVE`, the final verification passed, and the run stopped at `READY_FOR_HUMAN_REVIEW` with no revision. Independent confirmation afterwards reproduced three passing tests against the same diff. See `docs/runs/0003-two-model-fixture.md`.

Private run `20260901T202957Z-ec044a` rehearsed public Mailman issue #9 against exact commit `0ba4387fd4187faff88d9a3d900412d4ad2fc367`. The issue text was captured manually. Codex changed `mailman/artifacts.py` and `tests/test_artifacts.py` in the isolated workspace. Mailman's verification passed all 47 tests after the primary stage and again after Claude returned `APPROVE`. The run reached `READY_FOR_HUMAN_REVIEW` with no revision. The reviewed change shipped on `main` in `a371be1`, passed CI on Python 3.12 and 3.14, and issue #9 was closed.

Codex reported that it could not run the pinned Python executable inside its own sandbox and exited `0` anyway. Mailman's independent verification is what carried that run, which is the case the harness exists for.

Three earlier attempts on the same day, runs `20260901T194823Z-56b438`, `20260901T200938Z-3994b1`, and `20260901T201050Z-cd908c`, blocked because the agent executable could not be resolved. They are evidence that the harness refuses to start an unverifiable agent.

Private run `20260902T051904Z-f0cd07` was the first against a repository this project does not own: `ayukhno/autosound-tcc` issue #4 at commit `284d79918991fd29c15902f32dc879487ebf31fa`, from issue URL to exported patch with no hand-written prompt. `fetch-issue` captured the issue with the GitHub CLI, `prepare-environment` initialized submodules and installed the target with its development extra into a virtual environment in the run directory, and the workspace stayed clean. Codex changed one line plus a regression test in 139.8s, Claude approved in 95.3s after running the tests itself and confirming the new test fails without the fix, and both independent verifications passed. Independent confirmation afterwards applied the exported diff to a fresh upstream clone: 36 tests passed, and reverting only the source fix reproduced the issue's exact error. The target's full suite is unusable as a gate on this host, so verification was scoped to `tests/test_dsp_state.py`. The exported patch also carries an unrelated trailing-newline change that neither agent flagged. See `docs/runs/0004-first-external-issue.md`.

The revision cycle, the second review, and a `BLOCKED` ending have live evidence as of 2026-09-02, recorded in `docs/runs/0005-revision-and-blocking-paths.md`. Run `20260902T054112Z-ae9e1b` completed `REVISE`, revision, second review, `APPROVE`, and `READY_FOR_HUMAN_REVIEW` across two review cycles. Run `20260902T054015Z-0e51db` ended `BLOCKED` when verification failed after the revision, on an issue whose acceptance criteria contradicted an existing test. The remaining blocking causes, a missing or contradictory verdict, an agent timeout, and an agent that produces no report, still have only unit coverage.

A verification pass after the primary stage does not prove work happened: in the `clamp` run it passed because the agent changed nothing. The loop now records the changed paths after each primary stage. It does not block on an empty candidate; the reviewer caught that case.

An external target can be dirty on clone. `wandb/rai-toolkit` commits `rai_toolkit/redteam/attacks.py` with CRLF while its own `.gitattributes` declares `*.py text eol=lf`, so every fresh clone is modified before any agent runs and Mailman refuses to start. That is a property of the target, not of the run.

`mailman prepare-submission` checks a finished run against a target's recorded
contribution policy and writes a draft pull request, a human accountability
brief, and a machine-readable verdict. It blocks on diff noise, a missing
changelog entry, a missing or failed duplicate search, an unread or prohibitive
policy, a run that is not `READY_FOR_HUMAN_REVIEW`, and a run with no passing
verification. `mailman duplicate-search` records a GitHub CLI search of the
target's pull requests and issues. Both were exercised against live run data on
2026-09-02. Neither contacts an upstream repository for anything but a read.

`mailman acknowledge-no-test` records why a change ships without a test, pinned to
the exact paths its diff touches. With that record present the `no-test-change`
finding is still reported and still visible in `submission.json`, but no longer
blocks. It exists because run `20260903T052426Z-ad8196`'s reviewer proved, against
an export of the base commit, that a test would be dead coverage, and
`prepare-submission` had no way to accept that argument.

The verification command's executable is resolved through the run toolchain before
anything uses it, so the recorded command, the prompt text and the agent's allow
rules all name the same file. Before that fix a bare `python` ran whatever was
first on PATH, which failed run `20260903T050831Z-bed67e` on a missing dependency
of the host interpreter rather than on the candidate.

Run `20260903T052426Z-ad8196` is the first to reach `"ready": true` from
`prepare-submission`: `encode/starlette` #3497, Claude Opus 5 as both primary and
reviewer, one revision spent on the reviewer requiring the added test be removed.
A branch, a written pull request body and an accountability brief are staged under
the private run directory. Nothing has been sent. See
`docs/runs/0008-starlette-3497-submission-ready.md`.

`mailman target-intel RUN_ID` records how a target actually hands out and merges
outside work: human-only outside merges in a window, open issues no open or merged
pull request references, the automated rules a bot enforces quoted from its own
comments, and the thread that preceded each recent outside merge with the winning
author's comments marked. `check-target` refuses to clear a run without a
successful record, and `--acknowledge-prior-attempts` does not clear that refusal.
It reports counts against their denominators rather than verdicts, and it does not
choose the target. Exercised against `langchain-ai/langchain` on 2026-09-03, where
it found the `require-issue-link` and `block-fork-main` bot rules without being
told to look. See `docs/decisions/0009-target-intel.md`.

`mailman reproduce RUN_ID -- <command>` runs the reporter's own steps in the
prepared workspace at the base commit and records `reproduction.json`.
`check-target` refuses a run with no such record, and refuses again when the
record says the reported behaviour did not happen; neither refusal is clearable
by `--acknowledge-prior-attempts`. The default expectation is a command that
fails; `--expect-output`, `--forbid-output` and `--expect-exit-code` cover a bug
whose fixed and unfixed trees both fail and differ only in what they print, which
is the pytest #14964 shape. A timeout is recorded as a timeout, never as a
reproduction. `--not-machine-reproducible --note` records a human reading and
warns instead of blocking. The command's executable resolves through the run
toolchain. Covered by 19 unit tests, and exercised end to end through the CLI on
2026-09-03 against a scratch run: a bug that still fails passes the gate, a
command that now succeeds is refused, the counts-only pytest #14964 shape is
refused through `--expect-output`/`--forbid-output`, and
`--acknowledge-prior-attempts` clears neither refusal. It has not yet run
against a live target.
See `docs/decisions/0010-reproduction-gate.md`.

Sanitized public run export is not implemented. Nothing in this project has ever
pushed, commented, or opened anything on a repository it does not own.

Three runs against `pytest-dev/pytest` issue #14324 all ended `BLOCKED` on
2026-09-02: two on a failed verification after the primary stage, one when the
agent hit its turn limit and wrote no report. That last one is the first live
evidence for the missing-report blocking cause. See
`docs/runs/0006-pytest-14324-three-blocked-runs.md`.

## Knowledge flywheel

`mailman retrospective RUN_ID` drafts `retrospective.json` and
`retrospective.md` in the private run directory from evidence Mailman already
holds. It seeds observations only for machine-observed facts and refuses to
overwrite an existing retrospective without `--force`. The taxonomy, the
weighted learning channels, the retrospective schema, and the lesson registry
with its promotion gates are implemented and unit-covered. See
`docs/decisions/0005-knowledge-flywheel.md`.

`knowledge/lessons.json` holds six lessons as of 2026-09-03, written through the
registry's own dataclasses so the state machine and promotion gates were enforced
rather than asserted. All six sit at `CANDIDATE_LESSON`: reproduction at the base
commit, executable resolution before a command is recorded, dead regression
coverage, human-only merge counting, closed-unmerged rows on a repository that
auto-closes, and how assignment is actually won. Only the executable-resolution
lesson has the two distinct supporting runs `VALIDATED` requires, and it was not
promoted on that basis alone.

Nothing yet writes a lesson automatically at the end of a run, so the registry
does not gain evidence without a hand pass. `mailman retrospective` has still not
been run against a live run. Skill versioning and the regression suite are not
implemented, so every retrospective records `skill_version` as `unversioned`.
