# 0003. Prepare target repositories inside private run directories

Status: accepted on 2026-09-02.

## Decision

Clone each target repository into `<run>/workspace` and checkout the full recorded base commit in detached HEAD state. Capture clone and checkout results in `<run>/workspace.json`.

Disable Git system and global configuration during clone and checkout. Use an empty template and hooks directory. This prevents the target checkout from inheriting host hooks, content filters, or other Git behavior that could execute during preparation. The first version therefore supports public repositories that do not need a credential helper or custom proxy configuration.

Refuse an existing workspace unless its origin URL, exact HEAD, clean status, and prior preparation record all match. Reuse preserves the original Git command evidence and increments a counter. Mailman never deletes or repairs a mismatched workspace automatically.

Reject repository URLs containing embedded HTTPS credentials or SSH passwords before writing a run record.

## Evidence

Private run `20260901T192742Z-1f3acb` cloned the public Mailman repository and detached at `318c9fe6b701064d4409bf5121d33ac032efd833`. The resulting tree was clean and its origin matched the run record. A second preparation call reused the workspace, retained both Git command records, and set `reuse_count` to `1`.

Unit tests also prepare a local repository at an older commit and reject dirty workspace reuse.
