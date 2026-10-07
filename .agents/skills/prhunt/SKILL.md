---
name: prhunt
description: Run or resume Mailman's PRHunt workflow when the user says /PRHunt N, $prhunt N, asks to prepare N pull requests, or asks for a rolling hunt that keeps going until told to stop (/PRHunt with no number). Ask for model choices once, complete the shared procedure, and stop for final filing approval. Use only in the Mailman repository.
---

Read `mailman/procedure.md` from the repository root, or run
`python -m mailman procedure`. That file is the single procedure for every
model. Follow it through `hunt finish`; do not stop after the agents finish.

Read existing hunt state with `python -m mailman hunt list` before starting.
One hunt has one coordinator. If `hunt status` shows a live lease you do not
hold, another task owns it: take a separate hunt, or take that one over with
`hunt lease --takeover --reason`. Never sit in a loop polling someone else's
hunt. Show the checkpoint page as soon as any candidate is ready rather than
holding finished work until the quota is met.
If model choices are missing, ask which primary and reviewer adapter/model IDs
the user wants. Do not assume defaults. N counts complete PR candidates, not
attempts. With no N, run the procedure's "Rolling hunt": ask for models once,
then loop one candidate at a time until the user says stop, and leave filing
to a separate session. Repair routine failures or replace candidates without asking the user.
When either Codex role uses `gpt-5.6-luna`, run orchestration at `medium`
reasoning effort unless the user explicitly asks for a slower effort after being
told that higher effort increases end-to-end latency on these bounded tasks.

Run Mailman from this repository with `python -m mailman` if the installed
entry point resolves to another checkout. Never publish or file an issue until
the user approves the exact final filings. Do not hand-write review HTML.
