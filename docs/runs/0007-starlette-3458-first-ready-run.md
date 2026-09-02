# 0007. starlette #3458, the first external run to reach a human

Date: 2026-09-02. Private run `20260902T144544Z-5dbf69`. Sanitized from local
evidence.

The first run against a recognizable external project that finished the whole
loop and stopped at `READY_FOR_HUMAN_REVIEW` with no revision. Claude was the
primary engineer at the new 120-turn budget, Codex the reviewer.

## Target

`encode/starlette`, BSD-3-Clause, 12,585 stars, an explicit `AI_POLICY.md`.
Issue [#3458](https://github.com/encode/starlette/issues/3458): a
`BackgroundTask` starts before the last byte of the response reaches the client
whenever a `BaseHTTPMiddleware` sits in the stack, which contradicts the
documented contract that a background task runs only once the response has been
sent. Base commit `39fd0ffac25593fce39466320c9a666957ce8b8c`.

The duplicate search found no pull requests. Two people in the issue thread have
offered to write one and no maintainer has answered either, so the issue is
unclaimed by Mailman's rule and soft-claimed by any human reading.

Nothing was sent upstream. No comment, no branch, no pull request.

## What the loop did

| Step | Result |
| --- | --- |
| target | no prior attempt |
| primary (claude, 120 turns) | 35 turns, 358 s, $1.00, exit 0 |
| workspace change | `starlette/middleware/base.py`, `tests/middleware/test_base.py` |
| verification after primary | exit 0 |
| reviewer (codex) | 64 s, exit 0, verdict APPROVE |
| final verification | exit 0 |

`revisions_used: 0`, `review_cycles: 1`. The gate was scoped to
`tests/middleware/test_base.py` and `tests/test_background.py`, which pass 67
tests and 2 xfail at the base commit on this host, because the full suite has 14
pre-existing failures here.

## The patch

Eleven lines in `send_no_error`. After the inner app hands over its terminal
message, `http.response.body` without `more_body` or `http.response.pathsend`,
the inner send waits on the existing `response_sent` event before returning. The
inner `Response.__call__` runs its background task the moment that send returns,
so holding it there defers the task until the outer `_StreamingResponse` has
finished writing to the real `send`. Non-final chunks return immediately, so a
streaming body is not serialized.

This is the backpressure approach, not the message-passing one. The alternative
is to move the background task off the inner response and onto the constructed
`_StreamingResponse`, which needs a new channel through the memory stream to
carry the reference.

## Independent confirmation

Afterwards, against a fresh clone of the upstream repository at the same base
commit, outside any run directory, with the run's own interpreter:

- `git apply` accepted `changes.diff`.
- Scoped gate: 69 passed, 2 xfailed, against 67 and 2 at baseline. The two added
  cases are the new test on the asyncio and trio backends.
- Reverting only the source hunk and keeping the test: both new cases fail, with
  `background-done` landing at index 2, before the final body frame. The test
  fails for the reason the issue describes.
- Full suite with the patch: 14 failed, 1189 passed. Full suite with the source
  hunk reverted: 16 failed, 1187 passed. The delta is exactly the two new cases,
  and all 14 remaining failures are Windows environment problems in
  `test_staticfiles.py` and one config regex, none in middleware.
- `ruff check` and `ruff format --check` pass. `mypy` reports one pre-existing
  error, `os.pathconf` missing on Windows in `test_staticfiles.py`, untouched by
  the patch.

## What this run exposed

The reviewer could not execute anything. Codex reported that the run's
interpreter returned "Access is denied" inside its sandbox and correctly refused
to claim any test passed, then approved on a code read alone. Mailman's own
verification carried the run, as it has every time. Filed as
[#20](https://github.com/wolfgang-aura/Mailman/issues/20).

Streaming output was invisible for the whole run because `_emit` printed without
flushing and stdout was redirected to a file. The per-role live log held the
evidence the console did not. Filed and fixed as
[#19](https://github.com/wolfgang-aura/Mailman/issues/19).

`mailman prior-art` failed on a target with no prior pull requests, which is the
best possible target. Filed as
[#18](https://github.com/wolfgang-aura/Mailman/issues/18).

## What this does not show

One issue, one repository, one approval on the first cycle. The revision path
the previous session added has still never fired on a live run. The turn budget
was raised to 120 in the same session and this run used 35, so it says nothing
about whether 120 is right. And an approved patch is not a merged pull request:
the change alters timing for every response through `BaseHTTPMiddleware`, not
only responses carrying a background task, and a maintainer may want that cost
argued before the fix.

## Addendum: the review, run again on 2026-09-03

The reviewer in the table above executed nothing. Its interpreter was inside the
user profile and Codex's Windows sandbox refuses to create a process from one
([#20](https://github.com/wolfgang-aura/Mailman/issues/20)). After staging an
interpreter at `C:\ProgramData\mailman-python` and rebuilding the run's
environment on it, the review stage was run again against the same tree, this
time on `gpt-5.6-luna` at `max` reasoning effort.

| | first review | second review |
| --- | --- | --- |
| model | Codex default | `gpt-5.6-luna`, `max` effort |
| duration | 64 s | 682 s |
| commands run | 3, one denied | 46 |
| tests executed | none | the gate, plus targeted runs and its own harnesses |
| verdict | APPROVE | APPROVE |

The second reviewer reproduced the ordering itself with hand-written ASGI
harnesses, checked the `http.response.pathsend` path the patch also touches,
read the upstream history around the file, and only then approved. Both reviews
reached the same verdict; only one of them earned it.

Two limits showed up in the second review. The read-only sandbox has no writable
temp directory, so the gate errored twice on `tempfile` and once on
`.pytest_cache` before the agent worked around it with `-p no:cacheprovider`;
Mailman's own verification of the same tree, outside any sandbox, is 69 passed
and 2 xfailed. Filed as
[#29](https://github.com/wolfgang-aura/Mailman/issues/29). And re-preparing the
environment of a finished run reports failure because the workspace holds the
candidate, which is
[#28](https://github.com/wolfgang-aura/Mailman/issues/28).

