# Run 0002. Public repository workspace preparation

Date: 2026-09-02.

Status: development fixture.

## Input

- Repository: `https://github.com/wolfgang-aura/Mailman.git`
- Base commit: `318c9fe6b701064d4409bf5121d33ac032efd833`
- Tracking issue: [#7](https://github.com/wolfgang-aura/Mailman/issues/7)
- Private run ID: `20260901T192742Z-1f3acb`

## Result

`prepare-workspace` cloned the repository without checking out the default branch, then checked out the recorded commit in detached HEAD state. Independent Git checks confirmed:

- `HEAD` exactly matched the recorded commit;
- the workspace had no branch attached;
- `git status --porcelain` returned no changes;
- `origin` matched the recorded repository URL.

A second call reused the same workspace. `workspace.json` retained the original clone and checkout command results and recorded `reuse_count: 1`.

No project install, hook, test, or build command ran during preparation.
