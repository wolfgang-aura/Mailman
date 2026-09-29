"""Who speaks for a repository, beyond what `author_association` admits.

GitHub reports a maintainer whose organisation membership is private as
`CONTRIBUTOR`. marimo's lead maintainer, mscolnick, is one: his own parked fix,
marimo-team/marimo#9862 ("Fixes #9808"), read as an outsider's stale attempt,
and the issue passed prescreen with a warning. Mailman #203.

Merging a pull request needs write access whatever the membership says, so the
repository screen records who merged its recent pull requests. Every check
that asks "is this a maintainer?" asks `is_maintainer`, which accepts either
GitHub's association or a login in that recorded set. With no recorded set it
answers exactly as the association alone did.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any

from mailman.target_intel import _is_bot

#: GitHub's author associations for somebody who can merge.
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: Merged pull requests read for their merger, in one GraphQL call. The REST
#: list of closed pull requests carries no `merged_by`, and reading it per pull
#: request would cost a hundred calls.
MERGED_SAMPLE = 100

MERGERS_QUERY = (
    'query { repository(owner: "%s", name: "%s") { '
    f"pullRequests(states: MERGED, last: {MERGED_SAMPLE}) "
    "{ nodes { mergedBy { login __typename } } } } }"
)


def _login(item: dict[str, Any]) -> str | None:
    """The author login of a REST comment, a `gh` row or a recorded row."""
    for key in ("user", "author"):
        value = item.get(key)
        if isinstance(value, dict) and value.get("login"):
            return str(value["login"])
        if key == "author" and isinstance(value, str) and value:
            return value
    login = item.get("login")
    return str(login) if login else None


def _association(item: dict[str, Any]) -> str:
    for key in ("author_association", "authorAssociation", "association"):
        value = item.get(key)
        if value:
            return str(value).upper()
    return ""


def is_maintainer(
    item: Any,
    maintainers: Collection[str] = (),
    *,
    associations: Collection[str] = MAINTAINER_ASSOCIATIONS,
) -> bool:
    """Whether this comment's, pull request's or row's author speaks for the project.

    True when GitHub's association is one of `associations`, or when the
    author's login is in `maintainers`, the set the repository screen
    recorded. Logins compare without case, as GitHub's do.
    """
    if not isinstance(item, dict):
        return False
    if _association(item) in associations:
        return True
    if not maintainers:
        return False
    login = _login(item)
    return bool(login) and login.lower() in {name.lower() for name in maintainers}


def mergers(payload: Any) -> list[str]:
    """The human logins that merged pull requests, from the GraphQL answer."""
    try:
        nodes = payload["data"]["repository"]["pullRequests"]["nodes"]
    except (KeyError, TypeError):
        return []
    found: set[str] = set()
    for node in nodes if isinstance(nodes, list) else []:
        merged_by = node.get("mergedBy") if isinstance(node, dict) else None
        if not isinstance(merged_by, dict) or not merged_by.get("login"):
            continue
        user = {"login": merged_by["login"], "type": merged_by.get("__typename")}
        if _is_bot(user):
            continue
        found.add(str(merged_by["login"]))
    return sorted(found, key=str.lower)


def maintainer_logins(screen: Any) -> frozenset[str]:
    """The maintainer logins a stored repository screen recorded, or none."""
    if not isinstance(screen, dict):
        return frozenset()
    logins = screen.get("maintainer_logins")
    if not isinstance(logins, Iterable) or isinstance(logins, (str, bytes)):
        return frozenset()
    return frozenset(str(login) for login in logins if login)


def load_maintainer_logins(data_root: Path | None, slug: str | None) -> frozenset[str]:
    """`maintainer_logins` of the screen stored under `data_root`, or none."""
    if data_root is None or not slug:
        return frozenset()
    # Imported here: `screen` imports `claims`, which imports this module.
    from mailman.screen import load_screen

    return maintainer_logins(load_screen(data_root, slug))
