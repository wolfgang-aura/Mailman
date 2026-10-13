# 0035. pyinfra #1992, the twenty-third merge upstream

Date: filed 2026-10-07 07:16 UTC, merged 2026-10-13 11:44 UTC. Private run
`20261007T045414Z-34cc41`. Sanitized from local evidence and the public pull
request thread.

Merged as pull request [#1992](https://github.com/pyinfra-dev/pyinfra/pull/1992),
merge commit `e0652b9`, six days after filing, by wowi42.

## Target and defect

`connectors/sshuserclient/client.py` imports a paramiko API that exists only
from 3.2, but `pyproject.toml` allowed `paramiko>=2.11`. The fix raises the
floor to `>=3.2` in `pyproject.toml` and the matching `uv.lock` requires-dist
line. +19 -2 across 3 files.

## What the harness did

| Stage | Result |
| --- | --- |
| Filing | 2026-10-07, one commit of ours |
| Review | wowi42 approved on 2026-10-13; no change requested, no conversation comments |
| Merge | wowi42 merged the PR as filed, 33 minutes after the approval |

## What the maintainer changed

Nothing. The merged tree is our commit.

## Lessons

- A dependency floor fix needs the import that proves it. The PR body named the
  import, and the approval summary repeated it.
- The `uv.lock` edit was part of the same change; a floor raised in only one
  file would have left the lock out of step.