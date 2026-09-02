# 0006. Issue ingestion, environment preparation, and patch export

Date: 2026-09-02. Status: accepted.

## Context

Two live runs reached `READY_FOR_HUMAN_REVIEW`, but neither proved the harness
could take a real external issue. Three gaps stood between the loop and a first
upstream attempt:

- `init-run --issue URL` stored the URL and nothing else. Both live prompts were
  written by hand, so "run an issue" meant "transcribe an issue by hand".
- Mailman has no runtime dependencies, so no run had ever installed anything. On
  a target repository, a verification command means nothing until its
  dependencies exist.
- After the loop stopped there was no command to emit a diff or a pull request
  draft. A human read the workspace by hand.

## Decision

### Issue ingestion

`mailman fetch-issue` reads the run's own issue URL with the GitHub CLI and
writes `issue.md` plus an `issue.json` record. The URL is parsed strictly: a
pull request URL, a non-GitHub host, or an issue number of zero is refused
rather than fetched.

Only the issue's own fields are captured. Comments, linked pull requests, and
the accepted upstream fix are deliberately absent, because a historical issue
replay whose prompt contains its own answer measures nothing. The rendered file
says so, so an agent reading it cannot mistake the omission for an oversight.

A `--from-file` path records text a human transcribed, with the source path and
its SHA-256 digest, for hosts without `gh`. That is how the issue #9 rehearsal
was done before this command existed.

`mailman build-prompts` turns the captured issue into `primary-task.md` and
`reviewer-task.md`, quoting the base commit and the verification command Mailman
will run itself. `orchestrate` uses those files when no prompt is passed.

### Environment preparation

`mailman prepare-environment` runs a plan of named commands without a shell and
records every one. The prepared environment lives in the run directory, not in
the checkout, and preparation fails if it leaves the workspace dirty.

That rule is the whole point. The primary agent must start from a clean
workspace at the exact base commit, so an in-place install would only surface
later as a refused orchestration with an unhelpful message. Failing at
preparation names the step that dirtied the tree.

A plan may register the interpreter it built. Registration reuses `probe-tool`,
so the prepared runtime is digest-pinned and named in the agent prompts.
`{environment}` expands in a `verify` or `orchestrate` command as well, which is
how verification runs against the prepared interpreter rather than the host's.

### Patch export

`mailman export-patch` writes `changes.diff`, `summary.md`, `pull-request.md`,
and `export.json`. New files are included: `git add --intent-to-add --all` runs
first, because a diff that silently dropped an added test would understate the
change.

The diff is read straight from Git rather than through `execute`, which redacts
credential shapes from evidence records. A redacted patch would not apply and
the human would never learn which hunk was altered. So a diff matching a
credential pattern stops the export with an explanation instead.

Export refuses a run that is not `READY_FOR_HUMAN_REVIEW` unless
`--allow-unfinished` is passed, which is how a `BLOCKED` run's partial work gets
read without the package implying an approval that never happened.

## Consequences

The harness can now take a GitHub issue URL to a reviewable patch without a
human writing a prompt or reading a workspace by hand. Nothing here contacts an
upstream repository: the pull request text is a draft, and the human boundary is
unchanged.

Unit tests cover the modules. The commands were exercised end to end against a
local fixture, and `fetch-issue` was run against a real GitHub issue. What they
have not yet done is carry a live two-model run on an external repository with
real dependencies.
