# Environment plans

A plan tells `mailman prepare-environment` how to make a target repository's
test command mean something. Every step runs without a shell and is recorded.

`python-venv.json` is the plan used for the first external run: initialize the
target's submodules, build a virtual environment in the run directory, install
the target with its development extra, and register the resulting interpreter
for the run.

Three tokens expand in a command: `{environment}`, `{workspace}`, and `{run}`.
`{environment}` also expands in a `mailman verify` or `mailman orchestrate`
command, which is how verification runs against the interpreter the plan built.

Two rules are worth knowing before writing one.

The environment goes in the run directory, not the checkout. The primary agent
has to start from a clean workspace at the exact base commit, so preparation
that leaves a file behind in the working tree fails, and names the step that did
it. On Linux and macOS, `Scripts` becomes `bin`.

Registration reuses `probe-tool`, so a registered interpreter is digest-pinned
and named in both agent prompts. An agent that ignores it and reaches for the
host's Python is visible in the evidence rather than silently trusted.
