"""File every ready candidate in a hunt with one command.

Filing a ready hunt took the operator six to eight hand-run commands per run:
fork, push, open the pull request, then `hunt file` with the URL gh printed.
Hunt 20261001T102926Z-6e027b needed fifteen of them for two runs, in three
blocks, each a round trip to the machine. `hunt ship` runs the same steps for
every run at filing approval and stops at the first failure.

The approval boundary does not move: the operator running the command is the
approval for that batch, and only runs the readiness gate calls ready are
shipped. The destination is the one the handoff recorded and the packet
showed, never a new one. Every step reads before it writes, so a rerun after a
failure reuses the fork, the pushed branch and the open pull request instead of
making a second one.
https://github.com/wolfgang-aura/Mailman/issues/362
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mailman.artifacts import load_run
from mailman.completion import read_object
from mailman.handoff import body_digest, head_owner, load_handoff
from mailman.target_intel import repository_slug

#: Seconds between reads while GitHub creates a fork; forking is asynchronous.
FORK_WAITS = (2, 4, 8, 16, 30)
COMMAND_TIMEOUT_SECONDS = 180
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


@dataclass
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str], Path | None], Completed]


def run_command(command: list[str], cwd: Path | None = None) -> Completed:
    """Run git or gh without a shell, never prompting."""
    environment = {**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=COMMAND_TIMEOUT_SECONDS, check=False,
            shell=False, env=environment,
        )
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"timed out after {COMMAND_TIMEOUT_SECONDS}s")
    except OSError as error:
        return Completed(127, "", str(error))
    return Completed(done.returncode, done.stdout or "", done.stderr or "")


class ShipFailure(Exception):
    """A step that could not complete; the run stops here."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def _said(result: Completed) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")[:500]


def _not_found(result: Completed) -> bool:
    text = f"{result.stderr}\n{result.stdout}"
    return "404" in text or "Not Found" in text


def signed_in_login(run: Runner) -> str:
    """The account gh writes as. A fork lands there, whatever the head says."""
    result = run(["gh", "api", "user", "--jq", ".login"], None)
    login = result.stdout.strip()
    if result.returncode or not login:
        raise ShipFailure("gh-auth", f"`gh api user` failed: {_said(result)}. Run `gh auth status`.")
    return login


def _row_key(root: Path, run_id: str) -> tuple[str, Any] | None:
    try:
        run, _ = load_run(run_id, root)
    except (OSError, ValueError):
        return None
    return repository_slug(run.repository), run.issue or run.defect_report


def plan(root: Path, record: dict, *, login: str | None,
         readiness: Callable[[Path], dict], only: str | None = None,
         answer_review: bool = False) -> list[dict]:
    """Every live run, with what `ship` would do to it and why.

    Readiness is the hunt's own gate (`next_action`): decision, finalize,
    handoff and handoff-check. Nothing here loosens it; a run it does not call
    ready at filing approval is skipped with the gate's reason, and a run that
    still needs the operator (own-words rewrite, CLA) is never filed.
    """
    from mailman.hunt import PERSONAL_REVIEW_ACTION, READY_TO_ASK

    rows: list[dict] = []
    filed = sum(1 for row in record["runs"] if row.get("filed"))
    # A rolling hunt has no count, so every ready candidate is in the batch.
    slots = None if record.get("requested") is None else max(record["requested"] - filed, 0)
    counted: set = set()
    for entry in record["runs"]:
        run_id = entry["run_id"]
        if entry.get("dropped") or (only and run_id != only):
            continue
        row: dict[str, Any] = {"run_id": run_id, "steps": []}
        rows.append(row)
        if entry.get("filed"):
            row.update(outcome="already-filed", pr_url=entry["filed"]["pr_url"])
            continue
        try:
            checked = readiness(root / run_id)
        except (OSError, ValueError) as error:
            checked = {"ready": False, "stage": "evidence", "action": "", "detail": str(error)}
        if checked.get("disposition") == READY_TO_ASK:
            row.update(outcome="skipped", reason="ask-first candidate: its offer comment is "
                       "posted by hand and the pull request waits for a maintainer's answer")
            continue
        if not checked.get("ready"):
            reason = "; ".join(part for part in (
                f"not at filing approval (stage {checked.get('stage')})",
                checked.get("detail") or "", checked.get("action") or "") if part)
            row.update(outcome="skipped", reason=reason)
            continue
        # Passing --answer-review is the commitment the personal-review gate
        # asks for; own-words and CLA still need work done first (#402).
        personal = checked.get("action") == PERSONAL_REVIEW_ACTION
        if checked.get("human_required") and not (personal and answer_review):
            reason = "needs you before filing: " + checked["action"]
            if personal:
                reason += " Pass --answer-review to commit to that and file it."
            row.update(outcome="skipped", reason=reason)
            continue
        key = _row_key(root, run_id)
        if key is not None and key in counted:
            row.update(outcome="skipped", reason="same target as a run already in this batch")
            continue
        if slots is not None and slots <= 0:
            row.update(outcome="skipped", reason=f"the hunt's {record['requested']} requested "
                       "pull request(s) are already covered")
            continue
        handoff = load_handoff(root / run_id) or {}
        head = str(handoff.get("head") or "")
        owner = head_owner(head)
        if login is not None and owner and owner.lower() != login.lower():
            row.update(outcome="skipped", reason=(
                f"the approved head is {head}, but gh is signed in as {login}; "
                f"a fork would land under {login}. Run `gh auth switch --user {owner}`."))
            continue
        if key is not None:
            counted.add(key)
        if slots is not None:
            slots -= 1
        row.update(outcome="pending", repository=repository_slug(str(handoff.get("repository"))),
                   head=head, base=handoff.get("base"), title=handoff.get("title"))
    return rows


def _step(row: dict, stage: str, result: str, progress: Callable[[str], None]) -> None:
    row["steps"].append({"stage": stage, "result": result})
    progress(f"ship {row['run_id']}: {stage}: {result}")


def _fork_state(run: Runner, fork: str, upstream: str) -> bool:
    """True when `fork` is a fork of `upstream`, False when it does not exist."""
    result = run(["gh", "api", f"repos/{fork}"], None)
    if result.returncode:
        if _not_found(result):
            return False
        raise ShipFailure("fork", f"could not read {fork}: {_said(result)}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ShipFailure("fork", f"unreadable answer for {fork}: {error}") from error
    parent = str(((data.get("parent") or {}).get("full_name")) or "")
    if not data.get("fork") or parent.lower() != upstream.lower():
        raise ShipFailure("fork", (
            f"{fork} exists and is not a fork of {upstream} (parent {parent or 'none'}). "
            "Rename or delete it; provenance reads the fork under the upstream's name."))
    return True


def ship_run(root: Path, record: dict, row: dict, *, run: Runner, dry_run: bool,
             progress: Callable[[str], None], provenance_recorder=None,
             sleep: Callable[[float], None] = time.sleep) -> None:
    """Fork, push, open the pull request and record it, for one ready run."""
    from mailman.hunt import record_filing

    directory = root / row["run_id"]
    handoff = load_handoff(directory) or {}
    upstream = row["repository"]
    owner, _, branch = row["head"].partition(":")
    fork = f"{owner}/{upstream.split('/', 1)[1]}"
    workspace = Path(read_object(directory / "export" / "export.json").get("workspace")
                     or directory / "workspace")

    local = run(["git", "-C", str(workspace), "rev-parse", "--verify", f"refs/heads/{branch}"], None)
    sha = local.stdout.strip()
    if local.returncode or not sha:
        raise ShipFailure("package", f"branch {branch} is missing from {workspace}: {_said(local)}")
    row["commit"] = sha
    # Readiness already ran handoff-check, which needs the committed branch
    # and the handoff `package` writes. Packaging again would re-hash the body
    # and approve bytes the operator has not read, so it is never redone here.
    _step(row, "package", f"packaged; handoff-check passed on {sha[:12]}", progress)

    exists = _fork_state(run, fork, upstream)
    if exists:
        _step(row, "fork", f"reused {fork}", progress)
    elif dry_run:
        _step(row, "fork", f"would fork {upstream} to {fork}", progress)
    else:
        forked = run(["gh", "repo", "fork", upstream, "--clone=false"], None)
        if forked.returncode:
            raise ShipFailure("fork", f"`gh repo fork {upstream}` failed: {_said(forked)}")
        for wait in (0, *FORK_WAITS):
            sleep(wait)
            if _fork_state(run, fork, upstream):
                break
        else:
            raise ShipFailure("fork", f"{fork} did not appear after forking {upstream}")
        _step(row, "fork", f"forked {upstream} to {fork}", progress)

    remote = f"https://github.com/{fork}.git"
    if not exists and dry_run:
        _step(row, "push", f"would push {branch} at {sha[:12]} to {fork}", progress)
    else:
        listed = run(["git", "ls-remote", remote, f"refs/heads/{branch}"], None)
        if listed.returncode:
            raise ShipFailure("push", f"could not read {remote}: {_said(listed)}")
        there = listed.stdout.split()[0] if listed.stdout.split() else ""
        if there == sha:
            _step(row, "push", f"reused {fork}:{branch} at {sha[:12]}", progress)
        elif there:
            raise ShipFailure("push", (
                f"{fork}:{branch} is at {there[:12]}, the approved commit is {sha[:12]}. "
                "Refusing to overwrite it; find out who pushed it first."))
        elif dry_run:
            _step(row, "push", f"would push {branch} at {sha[:12]} to {fork}", progress)
        else:
            pushed = run(["git", "-C", str(workspace), "push", remote,
                          f"refs/heads/{branch}:refs/heads/{branch}"], None)
            if pushed.returncode:
                raise ShipFailure("push", f"git push to {remote} failed: {_said(pushed)}")
            _step(row, "push", f"pushed {branch} at {sha[:12]} to {fork}", progress)

    listed = run(["gh", "pr", "list", "--repo", upstream, "--head", branch, "--state", "all",
                  "--json", "url,state,headRepositoryOwner", "--limit", "50"], None)
    if listed.returncode:
        raise ShipFailure("pull-request", f"could not list pull requests on {upstream}: {_said(listed)}")
    try:
        existing = [pr for pr in json.loads(listed.stdout or "[]")
                    if str((pr.get("headRepositoryOwner") or {}).get("login") or "").lower()
                    == owner.lower()]
    except json.JSONDecodeError as error:
        raise ShipFailure("pull-request", f"unreadable pull request list: {error}") from error
    open_prs = [pr for pr in existing if str(pr.get("state")).upper() == "OPEN"]
    if open_prs:
        url = open_prs[0]["url"]
        _step(row, "pull-request", f"reused {url}", progress)
    elif existing:
        raise ShipFailure("pull-request", (
            f"{owner}:{branch} already had pull request {existing[0]['url']} "
            f"({existing[0].get('state')}). A second one is the operator's call, not this command's."))
    elif dry_run:
        url = None
        _step(row, "pull-request", f"would open on {upstream} from {row['head']} into "
              f"{row['base']}: {row['title']}", progress)
    else:
        body = Path(str(handoff.get("body_path") or ""))
        # The bytes posted are the bytes approved: the same digest check
        # handoff-check made, repeated at the moment of posting.
        if not body.is_file() or body_digest(body.read_text(encoding="utf-8")) != handoff.get("digest"):
            raise ShipFailure("pull-request", f"the body at {body} changed after the handoff; "
                              "run `mailman handoff` again and read it")
        created = run(["gh", "pr", "create", "--repo", upstream, "--title", str(row["title"]),
                       "--body-file", str(body), "--head", row["head"], "--base", str(row["base"])],
                      None)
        found = _PR_URL.findall(created.stdout)
        if created.returncode or not found:
            raise ShipFailure("pull-request", f"`gh pr create` failed: {_said(created)}")
        url = found[-1]
        _step(row, "pull-request", f"opened {url}", progress)
    row["pr_url"] = url

    if dry_run:
        _step(row, "hunt-file", "would record the pull request and commit", progress)
        row["outcome"] = "would-file"
        return
    try:
        record_filing(root, record, row["run_id"], pr_url=url, commit=sha,
                      provenance_recorder=provenance_recorder)
    except Exception as error:  # every cause stops the batch the same way
        raise ShipFailure("hunt-file", f"{error}. The pull request is open at {url}.") from error
    _step(row, "hunt-file", f"recorded {url}", progress)
    row["outcome"] = "filed"


def resume_command(record: dict, *, lease_owner: str | None, data_root: Path | None,
                   answer_review: bool = False) -> str:
    command = f"mailman hunt ship {record['hunt_id']}"
    if lease_owner:
        command += f" --owner {lease_owner}"
    if answer_review:
        command += " --answer-review"
    if data_root is not None:
        command += f' --data-root "{data_root}"'
    return command


def ship(root: Path, record: dict, *, lease_owner: str | None = None, dry_run: bool = False,
         refresh_evidence: bool = True, only: str | None = None,
         data_root: Path | None = None, run: Runner | None = None,
         readiness: Callable[[Path], dict] | None = None,
         refresher: Callable[..., Any] | None = None,
         provenance_recorder=None, progress: Callable[[str], None] | None = None,
         sleep: Callable[[float], None] = time.sleep,
         answer_review: bool = False) -> dict:
    from mailman import hunt

    if hunt.is_terminal(record):
        raise ValueError(f"hunt {record['hunt_id']} is {record['status']}; nothing is left to ship")
    progress = progress or (lambda line: print(line, file=sys.stderr, flush=True))
    run = run or run_command
    readiness = readiness or hunt.next_action
    result: dict[str, Any] = {"hunt_id": record["hunt_id"], "dry_run": dry_run, "runs": [],
                              "failure": None, "resume": None}
    if dry_run:
        result["note"] = ("dry run: evidence was not refreshed and nothing was written; "
                          "the real run refreshes duplicate searches and claims first")
    elif refresh_evidence:
        # The candidates about to be pushed are the ones a rival pull request
        # overtakes, so the filing checks run on fresh evidence, as in finish.
        progress("ship: refreshing duplicate searches and claims")
        (refresher or hunt.refresh)(root, record, include_ready=True, only=only,
                                    progress=progress)
    try:
        login = signed_in_login(run)
    except ShipFailure as failure:
        result["failure"] = {"run_id": None, "stage": failure.stage, "detail": failure.detail}
        result["resume"] = resume_command(record, lease_owner=lease_owner, data_root=data_root,
                                          answer_review=answer_review)
        return result
    result["login"] = login
    rows = plan(root, record, login=login, readiness=readiness, only=only,
                answer_review=answer_review)
    result["runs"] = rows
    if only and not rows:
        raise ValueError(f"run {only} is not a live run in hunt {record['hunt_id']}")
    for row in rows:
        if row["outcome"] != "pending":
            continue
        if result["failure"]:
            row["outcome"] = "not-attempted"
            continue
        try:
            ship_run(root, record, row, run=run, dry_run=dry_run, progress=progress,
                     provenance_recorder=provenance_recorder, sleep=sleep)
        except ShipFailure as failure:
            row.update(outcome="failed", stage=failure.stage, detail=failure.detail)
            result["failure"] = {"run_id": row["run_id"], "stage": failure.stage,
                                 "detail": failure.detail}
    if result["failure"]:
        result["resume"] = resume_command(record, lease_owner=lease_owner, data_root=data_root,
                                          answer_review=answer_review)
    result["filed"] = sum(row["outcome"] == "filed" for row in rows)
    return result


def render(result: dict) -> str:
    """One summary: per run, its pull request or why it has none."""
    rows = result["runs"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    tally = ", ".join(f"{count} {name}" for name, count in sorted(counts.items())) or "no runs"
    heading = "hunt ship" + (" --dry-run" if result["dry_run"] else "")
    lines = [f"{heading} {result['hunt_id']}: {tally}"]
    if result.get("note"):
        lines.append(result["note"])
    for row in rows:
        target = row.get("repository") or ""
        if row["outcome"] in ("filed", "already-filed", "would-file"):
            what = row.get("pr_url") or "new pull request"
        elif row["outcome"] == "failed":
            what = f"FAILED at {row['stage']}: {row['detail']}"
        elif row["outcome"] == "skipped":
            what = row["reason"]
        else:
            what = "not attempted: an earlier run failed"
        lines.append(f"  {row['run_id']}  {row['outcome']}  {target}  {what}".rstrip())
        for step in row.get("steps") or []:
            lines.append(f"      {step['stage']}: {step['result']}")
    failure = result.get("failure")
    if failure:
        if failure["run_id"] is None:
            lines.append(f"stopped before any run: {failure['stage']}: {failure['detail']}")
        lines.append(f"resume with: {result['resume']}")
    return "\n".join(lines)
