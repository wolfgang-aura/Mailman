---
description: Prepare PRs with Mailman's fixed procedure, a set number or rolling until told to stop
argument-hint: "[number of PRs, or nothing for a rolling hunt]"
---

Run Mailman's PRHunt procedure. With a number ($ARGUMENTS), prepare that many
PR candidates. With no number, run a rolling hunt: one candidate at a time to
filing approval, then the next, until the operator says stop.
Read AGENTS.md and mailman/procedure.md in this repository, including its
"Rolling hunt" section. Read existing hunt state before starting. Ask once for
primary and reviewer adapter/model IDs if they are missing. Follow the shared
procedure through `hunt finish`, replacing rejected candidates. Only genuine
user dependencies or final filing approval reach the user. Do not publish
anything without that approval, and do not file from the coordinator session.
