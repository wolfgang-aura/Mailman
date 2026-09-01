# Run 0001. Codex arithmetic fixture

Date: 2026-09-02.

Status: development fixture, not a formal Mailman run.

## Task

A disposable Git repository contained this defect:

```python
def add(left: int, right: int) -> int:
    return left - right
```

The existing unittest expected `add(2, 3)` to return `5`. The baseline test failed with `-1 != 5`.

## Attempt 1

Codex CLI 0.152.0 ran through the first adapter command. The process exited `0` after 29.136833 seconds and wrote a report, but every repository command was rejected by the Windows workspace policy. It made no diff. Independent verification still failed.

This attempt proved that a zero process exit and a report do not mean the engineering task succeeded.

## Diagnosis

The adapter used `--ignore-user-config`, which also removed the host's native Windows sandbox implementation setting. The installed host was configured for the elevated Windows sandbox. A diagnostic run restored only `windows.sandbox='elevated'` while retaining `workspace-write` and non-interactive approvals.

## Verified adapter attempt

The corrected adapter finished after 77.040939 seconds. It changed one operator:

```diff
-    return left - right
+    return left + right
```

Mailman reran `python -m unittest -v` outside the agent process. One test passed.

Codex reported that neither `python` nor `py` was available inside its sandbox account, so it could not run the test itself. The host result verified the patch but exposed a runtime mismatch.

## Toolchain-backed CLI attempt

Mailman initialized private run `20260901T192007Z-60b445` at exact fixture commit `4458f3dd5e85f1dae44f3251f293d62300cffb5d`. It probed a bundled Python executable and saved the resolved path, version result, and digest in `toolchain.json`. `run-agent` added that path to the saved prompt before invoking Codex.

The agent process finished after 44.715588 seconds. Codex made the same one-line fix and used the registered executable to run one unittest successfully. It also reported a clean `git diff --check`. Mailman then ran the same unittest independently with the same executable, and it passed.

The run remained at `INITIALIZED`. Agent completion did not advance workflow status.

## Gaps

- The run did not pin a model, so the exact model is unknown. Run creation now accepts explicit primary and reviewer model IDs.
- Raw JSONL process evidence is private and has not been exported into this public record.
- Claude did not review the patch because Claude CLI is absent.
- The simple fixture proves process integration, not performance on open-source work.

Issue [#3](https://github.com/wolfgang-aura/Mailman/issues/3) stays open until a real Codex and Claude fixture run completes.
