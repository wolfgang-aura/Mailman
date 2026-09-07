---
name: prhunt
description: Run or resume Mailman's PRHunt workflow when the user says /PRHunt N, $prhunt N, or asks to prepare N pull requests. Ask for model choices once, complete the shared procedure, and stop for final filing approval. Use only in the Mailman repository.
---

Read `mailman/procedure.md` from the repository root, or run
`python -m mailman procedure`. That file is the single procedure for every
model. Follow it through `hunt finish`; do not stop after the agents finish.

Read existing hunt state with `python -m mailman hunt list` before starting.
If model choices are missing, ask which primary and reviewer adapter/model IDs
the user wants. Do not assume defaults. N counts complete PR candidates, not
attempts. Repair routine failures or replace candidates without asking the user.

Run Mailman from this repository with `python -m mailman` if the installed
entry point resolves to another checkout. Never publish or file an issue until
the user approves the exact final filings. Do not hand-write review HTML.
