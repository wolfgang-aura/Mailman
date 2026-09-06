"""The last step before something is posted under a human's name.

A pull request body is written by an agent and posted by a person. Between
those two acts the harness has one job: put the exact text that will be
published in front of the person who is about to publish it, in the same block
as the command that publishes it, and refuse to hand over the command when the
text has changed since they last saw it.

See https://github.com/wolfgang-aura/Mailman/issues/47.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from mailman.claims import load_claims
from mailman.submission import load_duplicate_search
from mailman.target_intel import repository_slug


HANDOFF_FILENAME = "handoff.json"

HANDOFF_SCHEMA_VERSION = 1

#: How old prior-art evidence may be when it is handed over for publishing.
#:
#: `duplicate-search` runs once, at run time; publishing happens whenever a
#: human gets to it, which for this project has been a day or more on every run
#: so far. Run 20260903T052426Z-ad8196 finished clean against
#: `encode/starlette` and was overtaken by a byte-identical pull request 94
#: minutes later, and by a second one the same evening. Nothing at run time
#: could have caught either; only a check at push time can.
#:
#: An hour sits under that observed 94 minutes, and re-running the search costs
#: one command. See https://github.com/wolfgang-aura/Mailman/issues/41.
EVIDENCE_MAX_AGE_MINUTES = 60

_KINDS = frozenset({"pull-request", "issue-comment"})

# Claims only the person posting can make true. The agent cannot read a diff
# on the human's behalf, cannot take responsibility, and cannot vouch for the
# change; a body that says otherwise in the first person is false the moment
# it is posted by someone who has not read it.
_FIRST_PERSON_CLAIMS = (
    re.compile(r"\bI\s+(?:have\s+)?read\b", re.IGNORECASE),
    re.compile(
        r"\bI\s+(?:have\s+)?(?:tested|verified|checked|reviewed|ran|run)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bI\s+take\s+responsibility\b", re.IGNORECASE),
    re.compile(r"\bI\s+(?:can\s+)?confirm\b", re.IGNORECASE),
    re.compile(r"\bI\s+stand\s+behind\b", re.IGNORECASE),
    re.compile(r"\bI\s+wrote\b", re.IGNORECASE),
    re.compile(
        r"\bmy\s+(?:own\s+)?(?:testing|machine|checkout|reading)\b", re.IGNORECASE
    ),
)


# A claim that something still works is a test result, not an opinion, and it
# is the kind a maintainer checks rather than takes on trust. A pull request
# said ndarray input was "unchanged" when it had never worked at all, and the
# maintainer found that in minutes.
#
# See https://github.com/wolfgang-aura/Mailman/issues/50.
_PRESERVATION_TRIGGERS = re.compile(
    r"\b(?:"
    r"unchanged|unaffected|preserved|untouched|"
    r"still\s+works?|still\s+passes|"
    r"not\s+(?:broken|affected|changed|altered)|"
    r"no\s+(?:behaviou?ral\s+)?change|"
    r"behaviou?r\s+is\s+(?:the\s+same|identical)|"
    r"backwards?[- ]compatible"
    r")\b",
    re.IGNORECASE,
)

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def preservation_claims(text: str) -> list[dict[str, Any]]:
    """Every line asserting that something kept working, and its list items.

    A lead-in such as "Existing behavior is unchanged for:" makes each bullet
    under it a separate claim about a separate input shape. Returning only the
    lead-in would hide exactly the item that turns out to be false, so the
    items come back too, attributed to the line that introduced them.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    found: list[dict[str, Any]] = []
    lead_in: int | None = None
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        is_bullet = bool(_BULLET.match(line))
        if lead_in is not None and is_bullet:
            found.append({"line": number, "text": stripped, "under": lead_in})
            continue
        if not is_bullet:
            lead_in = None
        if _PRESERVATION_TRIGGERS.search(stripped):
            found.append({"line": number, "text": stripped, "under": None})
            if not is_bullet:
                lead_in = number
    return found


def head_owner(head: str | None) -> str | None:
    """The account a pull request's head branch lives under, if it is named."""
    if not head or ":" not in head:
        return None
    owner = head.split(":", 1)[0].strip()
    return owner or None


def github_owner_type(owner: str) -> str | None:
    """"User", "Organization", or None when GitHub could not be asked.

    None is not a pass. The caller turns it into a warning that says the
    question went unanswered, because an unanswered question here is what the
    org-owned fork looked like right up until the push was denied.
    """
    if shutil.which("gh") is None:
        return None
    try:
        completed = subprocess.run(
            ["gh", "api", f"users/{owner}", "--jq", ".type"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def maintainer_edit_warning(owner: str | None, owner_type: str | None) -> str | None:
    """Why a maintainer will not be able to push to this branch.

    GitHub grants "allow edits by maintainers" only on forks owned by a user
    account. On an organisation-owned fork the permission cannot be granted,
    `maintainerCanModify` still reports true, and the maintainer finds out only
    when the push is denied. That is how pmorissette/ffn#328 was closed and
    reopened as somebody else's pull request.

    See https://github.com/wolfgang-aura/Mailman/issues/49.
    """
    if owner is None:
        return None
    if owner_type == "Organization":
        return (
            f"{owner} is a GitHub organisation. Maintainer edits cannot be "
            "granted on an organisation-owned fork, so a maintainer who wants "
            "to amend this branch will be denied the push, and may close the "
            "pull request instead. Push from a fork owned by your user account."
        )
    if owner_type is None:
        return (
            f"could not determine whether {owner} is a user or an organisation. "
            "If it is an organisation, maintainers will not be able to push to "
            "this branch."
        )
    return None


def body_digest(text: str) -> str:
    """Hash the body as it will be posted, newlines normalised."""
    normalised = text.replace("\r\n", "\n").strip() + "\n"
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def first_person_claims(text: str) -> list[dict[str, Any]]:
    """Every line making a claim only the human posting it can make true."""
    found: list[dict[str, Any]] = []
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in _FIRST_PERSON_CLAIMS:
            if pattern.search(stripped):
                found.append({"line": number, "text": stripped})
                break
    return found


def publish_command(
    *,
    kind: str,
    body_path: Path,
    repository: str,
    title: str | None = None,
    head: str | None = None,
    base: str | None = None,
    issue_number: int | None = None,
) -> str:
    """The one command that sends the body, reading it from the file on disk.

    `--body-file` and not `--body` on purpose: the bytes that were previewed
    are the bytes that get posted, and the digest below covers them.
    """
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {sorted(_KINDS)}, not {kind!r}")
    quoted = str(body_path)
    if kind == "issue-comment":
        if issue_number is None:
            raise ValueError("an issue comment needs --issue")
        return (
            f"gh issue comment {issue_number} --repo {repository} "
            f'--body-file "{quoted}"'
        )
    if not head:
        raise ValueError("a pull request needs --head, for example Mailman-Fork:branch")
    if not base:
        raise ValueError("a pull request needs --base")
    if not title:
        raise ValueError("a pull request needs --title")
    return (
        f"gh pr create --repo {repository} "
        f'--title "{title}" --body-file "{quoted}" '
        f"--head {head} --base {base}"
    )


def _preamble(record: dict[str, Any], claims: list[dict[str, Any]]) -> list[str]:
    lines = [
        "=" * 72,
        f"HANDOFF  {record['kind']}  ->  {record['repository']}",
        "=" * 72,
        "",
        "This is the whole text that will be posted under your name. Read it",
        "before running the command underneath it. Nothing else in this",
        "session has shown it to you.",
        "",
        f"Body file: {record['body_path']}",
        f"Digest:    {record['digest']}",
        "",
    ]
    warning = record.get("maintainer_edit_warning")
    if warning:
        lines.extend(
            [
                "-" * 72,
                "FORK OWNERSHIP -- a maintainer may not be able to push here",
                "-" * 72,
                "",
                "  " + warning,
                "",
            ]
        )
    preservation = record.get("preservation_claims") or []
    if preservation:
        lines.extend(
            [
                "-" * 72,
                "PRESERVATION CLAIMS -- each one is a test result",
                "-" * 72,
                "",
            ]
        )
        for claim in preservation:
            indent = "    " if claim.get("under") else "  "
            lines.append(f"{indent}line {claim['line']}: {claim['text']}")
        lines.extend(
            [
                "",
                "Each of these says something kept working. Name the command that",
                "showed it, or rewrite it as what you actually established.",
                '"Not exercised by this change" is honest and costs nothing.',
                "",
            ]
        )
    if claims:
        lines.extend(
            [
                "-" * 72,
                "FIRST-PERSON CLAIMS -- only you can make these true",
                "-" * 72,
                "",
            ]
        )
        lines.extend(f"  line {claim['line']}: {claim['text']}" for claim in claims)
        lines.extend(
            [
                "",
                "Posting this without having done what it says makes it false at",
                "the moment it is posted. Either do it, or cut the line.",
                "",
            ]
        )
    return lines


def render_handoff(record: dict[str, Any], body: str) -> str:
    """The body and the command that posts it, as one block that cannot split."""
    claims = record.get("first_person_claims") or []
    lines = _preamble(record, claims)
    lines.extend(
        ["-" * 72, "BODY", "-" * 72, "", body.replace("\r\n", "\n").rstrip(), ""]
    )
    lines.extend(
        [
            "-" * 72,
            "COMMAND",
            "-" * 72,
            "",
            "The check re-hashes the body file and refuses if it changed since",
            "this block was printed, so an edit made after the preview cannot",
            "post unread text.",
            "",
            record["verify_command"],
            record["command"],
            "",
        ]
    )
    return "\n".join(lines)


def build_handoff(
    *,
    run_id: str,
    run_directory: Path,
    body_path: Path,
    kind: str,
    repository: str,
    title: str | None = None,
    head: str | None = None,
    base: str | None = None,
    issue_number: int | None = None,
    data_root: Path | None = None,
    owner_type_lookup: Callable[[str], str | None] = github_owner_type,
) -> tuple[dict[str, Any], str]:
    """Record the body's digest and render the block that hands it over."""
    resolved = body_path.resolve()
    if not resolved.is_file():
        raise ValueError(f"no body at {resolved}")
    # Strictly, and never with `errors="replace"`: a body GitHub would render
    # as mojibake must stop here, not be quietly repaired into a preview that
    # differs from what gets posted.
    try:
        body = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"the body at {resolved} is not valid UTF-8 ({error}). Re-save it as "
            "UTF-8; PowerShell's default encoding mangles dashes and quotes."
        ) from error
    if not body.strip():
        raise ValueError(f"the body at {resolved} is empty")
    command = publish_command(
        kind=kind,
        body_path=resolved,
        repository=repository,
        title=title,
        head=head,
        base=base,
        issue_number=issue_number,
    )
    owner = head_owner(head) if kind == "pull-request" else None
    owner_type = owner_type_lookup(owner) if owner else None
    verify = f"mailman handoff-check {run_id}"
    if data_root is not None:
        verify += f' --data-root "{data_root}"'
    record = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "run_id": run_id,
        "prepared_at": datetime.now(UTC).isoformat(),
        "kind": kind,
        "repository": repository,
        "title": title,
        "head": head,
        "base": base,
        "issue_number": issue_number,
        "body_path": str(resolved),
        "digest": body_digest(body),
        "first_person_claims": first_person_claims(body),
        "preservation_claims": preservation_claims(body),
        "head_owner": owner,
        "head_owner_type": owner_type,
        "maintainer_edit_warning": maintainer_edit_warning(owner, owner_type),
        "command": command,
        "verify_command": verify,
    }
    path = run_directory / HANDOFF_FILENAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return record, render_handoff(record, body)


def load_handoff(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / HANDOFF_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _age_minutes(timestamp: object, now: datetime) -> float | None:
    """Minutes between an ISO 8601 record timestamp and now."""
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    try:
        recorded = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=UTC)
    return max(0.0, (now - recorded).total_seconds() / 60)


def check_prior_art_freshness(
    run_directory: Path,
    *,
    repository: str | None = None,
    now: datetime | None = None,
    max_age_minutes: float = EVIDENCE_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Say whether the run's prior-art evidence still describes the target.

    A staged submission ages. The duplicate search that cleared it ran once, at
    run time, and nothing re-reads the target's pull request list before the
    body is posted. This does not re-run the search itself, because it is a
    local check that must be able to decide on its own; it refuses stale
    evidence and names the command that refreshes it.
    """
    moment = now or datetime.now(UTC)
    search = load_duplicate_search(run_directory) or {}
    claims = load_claims(run_directory) or {}
    search_age = _age_minutes(search.get("searched_at"), moment)
    claims_age = _age_minutes(claims.get("collected_at"), moment)
    evidence = {
        "max_age_minutes": max_age_minutes,
        "duplicate_search_age_minutes": (
            round(search_age, 1) if search_age is not None else None
        ),
        "duplicate_search_repository": search.get("repository"),
        "claims_age_minutes": (
            round(claims_age, 1) if claims_age is not None else None
        ),
        "self_reported": bool(claims.get("self_reported")),
    }
    refresh = (
        "Re-run `mailman duplicate-search` and `mailman claims` for this run, "
        "then `mailman handoff` again."
    )
    if search_age is None:
        return {
            "ok": False,
            "reason": "no-duplicate-search",
            "detail": (
                "this run has no readable duplicate search, so nothing has "
                "checked the target for a pull request that does the same "
                f"thing. {refresh}"
            ),
            "evidence": evidence,
        }
    if not search.get("success"):
        return {
            "ok": False,
            "reason": "duplicate-search-failed",
            "detail": (
                "the recorded duplicate search did not complete, so it cleared "
                f"nothing. {refresh}"
            ),
            "evidence": evidence,
        }
    if repository and search.get("repository"):
        wanted = repository_slug(repository).lower()
        searched = repository_slug(str(search["repository"])).lower()
        if wanted != searched:
            return {
                "ok": False,
                "reason": "duplicate-search-elsewhere",
                "detail": (
                    f"the duplicate search covered {searched}, and this handoff "
                    f"publishes to {wanted}. {refresh}"
                ),
                "evidence": evidence,
            }
    if search_age > max_age_minutes:
        return {
            "ok": False,
            "reason": "duplicate-search-stale",
            "detail": (
                f"the duplicate search is {search_age / 60:.1f} hours old and "
                f"the limit is {max_age_minutes:g} minutes. An upstream "
                "duplicate has appeared 94 minutes after a run finished, so "
                f"evidence this old clears nothing. {refresh}"
            ),
            "evidence": evidence,
        }
    if not evidence["self_reported"]:
        if claims_age is None:
            return {
                "ok": False,
                "reason": "no-claims-check",
                "detail": (
                    "this run has no readable claims record, so nobody has "
                    "checked whether someone else took the issue. " + refresh
                ),
                "evidence": evidence,
            }
        if claims_age > max_age_minutes:
            return {
                "ok": False,
                "reason": "claims-stale",
                "detail": (
                    f"the claims check is {claims_age / 60:.1f} hours old and "
                    f"the limit is {max_age_minutes:g} minutes. Someone may "
                    f"have claimed the issue since. {refresh}"
                ),
                "evidence": evidence,
            }
    return {
        "ok": True,
        "reason": "fresh",
        "detail": (
            f"the duplicate search is {search_age:.0f} minute(s) old and the "
            "claims check is "
            + (
                "not required for a self-reported defect."
                if evidence["self_reported"]
                else f"{claims_age:.0f} minute(s) old."
            )
        ),
        "evidence": evidence,
    }


def check_handoff(
    run_directory: Path,
    *,
    now: datetime | None = None,
    max_age_minutes: float = EVIDENCE_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Say whether the body still matches the text the last handoff showed."""
    record = load_handoff(run_directory)
    if record is None:
        return {
            "ok": False,
            "reason": "no-handoff",
            "detail": (
                "no handoff was generated for this run. Run `mailman handoff` "
                "and read the body it prints before publishing anything."
            ),
        }
    body_path = Path(record.get("body_path", ""))
    if not body_path.is_file():
        return {
            "ok": False,
            "reason": "body-missing",
            "detail": f"the body recorded at {body_path} is gone.",
        }
    try:
        current = body_digest(body_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        return {
            "ok": False,
            "reason": "body-undecodable",
            "detail": f"the body at {body_path} is not valid UTF-8 ({error}).",
        }
    if current != record.get("digest"):
        return {
            "ok": False,
            "reason": "body-changed",
            "detail": (
                "the body changed after the last handoff was printed. Run "
                "`mailman handoff` again and read it before publishing."
            ),
            "expected_digest": record.get("digest"),
            "actual_digest": current,
        }
    unchanged = {
        "ok": True,
        "reason": "unchanged",
        "detail": "the body matches the text the last handoff printed.",
        "digest": current,
        "first_person_claims": record.get("first_person_claims") or [],
        "preservation_claims": record.get("preservation_claims") or [],
        "maintainer_edit_warning": record.get("maintainer_edit_warning"),
    }
    if record.get("kind") != "pull-request":
        return unchanged
    # The body being the text that was read is one question; whether the target
    # still wants it is another, and it is the one that ages. See
    # https://github.com/wolfgang-aura/Mailman/issues/41.
    freshness = check_prior_art_freshness(
        run_directory,
        repository=record.get("repository"),
        now=now,
        max_age_minutes=max_age_minutes,
    )
    unchanged["prior_art"] = freshness
    if freshness["ok"]:
        return unchanged
    return {
        "ok": False,
        "reason": freshness["reason"],
        "detail": freshness["detail"],
        "digest": current,
        "prior_art": freshness,
    }
