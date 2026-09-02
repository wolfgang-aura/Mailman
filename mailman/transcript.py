"""Normalize agent CLI output into events a person can read.

Both agent CLIs emit a machine-readable stream while they work: Codex writes
JSON lines under ``--json``, Claude writes them under ``--output-format
stream-json``. The shapes differ, so everything downstream - the live console,
the stored log, ``mailman show`` - works on the normalized events here instead
of on either vendor's schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Iterator

from mailman.redaction import redact

CODEX = "codex"
CLAUDE = "claude"

_SUMMARY_LIMIT = 160


@dataclass(frozen=True)
class TranscriptEvent:
    """One thing the agent did, in a form both CLIs can be mapped onto."""

    kind: str
    summary: str
    detail: str = ""

    def line(self, *, width: int = _SUMMARY_LIMIT) -> str:
        return f"{self.kind:<9} {_clip(self.summary, width)}"


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _tail(text: str, lines: int = 6) -> str:
    kept = [line for line in str(text).splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def _workspace_relative(path: str) -> str:
    """Show a run workspace path the way the target repository sees it."""
    text = str(path).replace("\\", "/")
    marker = "/workspace/"
    position = text.find(marker)
    if position != -1:
        return text[position + len(marker) :]
    return text


def unwrap_shell(command: str) -> str:
    """Drop the interpreter wrapper so the command itself stays visible.

    Codex runs everything through ``powershell.exe -Command "..."`` on Windows
    and ``bash -lc '...'`` elsewhere. Left intact, the wrapper fills the whole
    line and the actual command scrolls off.
    """
    text = str(command).strip()
    for flag in ("-Command", "-c", "-lc"):
        marker = f" {flag} "
        position = text.find(marker)
        if position == -1:
            continue
        head = text[:position].lower()
        if not any(
            shell in head
            for shell in ("powershell", "pwsh", "bash", "sh", "cmd", "zsh")
        ):
            continue
        text = text[position + len(marker) :].strip()
        break
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.replace("\\\\", "\\").strip()


# Codex ------------------------------------------------------------------


def _codex_item(event_type: str, item: dict) -> TranscriptEvent | None:
    item_type = item.get("type")
    if item_type == "command_execution":
        command = unwrap_shell(item.get("command", ""))
        if event_type == "item.started":
            return TranscriptEvent("command", command)
        exit_code = item.get("exit_code")
        status = "ok" if exit_code == 0 else f"exit {exit_code}"
        output = _tail(item.get("aggregated_output", ""))
        last_line = output.splitlines()[-1] if output else ""
        return TranscriptEvent(
            "result",
            f"{status} <- {last_line or command}",
            f"$ {command}\n{output}".rstrip(),
        )
    if item_type == "agent_message":
        if event_type != "item.completed":
            return None
        text = item.get("text", "")
        return TranscriptEvent("says", text, text)
    if item_type == "reasoning":
        if event_type != "item.completed":
            return None
        return TranscriptEvent("thinks", item.get("text", ""))
    if item_type == "file_change":
        if event_type != "item.completed":
            return None
        changes = item.get("changes") or []
        described = ", ".join(
            f"{change.get('kind', '?')} {_workspace_relative(change.get('path', '?'))}"
            for change in changes
            if isinstance(change, dict)
        )
        return TranscriptEvent("edits", described or "no paths reported", described)
    if item_type == "todo_list":
        if event_type != "item.completed":
            return None
        items = item.get("items") or []
        return TranscriptEvent("plan", f"{len(items)} step(s)", json.dumps(items))
    if event_type != "item.completed":
        return None
    return TranscriptEvent(str(item_type or "item"), json.dumps(item))


def parse_codex_event(payload: dict) -> TranscriptEvent | None:
    event_type = str(payload.get("type", ""))
    if event_type == "thread.started":
        return TranscriptEvent("thread", f"thread {payload.get('thread_id', '?')}")
    if event_type == "turn.started":
        return TranscriptEvent("turn", "turn started")
    if event_type == "turn.completed":
        usage = payload.get("usage") or {}
        return TranscriptEvent(
            "turn",
            "turn completed, "
            f"{usage.get('input_tokens', 0)} in / "
            f"{usage.get('output_tokens', 0)} out tokens",
        )
    if event_type in {"turn.failed", "error"}:
        return TranscriptEvent("error", json.dumps(payload))
    item = payload.get("item")
    if isinstance(item, dict):
        return _codex_item(event_type, item)
    return None


# Claude -----------------------------------------------------------------


def _claude_blocks(message: dict) -> Iterator[TranscriptEvent]:
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            yield TranscriptEvent("says", content, content)
        return
    for block in content or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and str(block.get("text", "")).strip():
            yield TranscriptEvent("says", block["text"], block["text"])
        elif block_type == "thinking":
            yield TranscriptEvent("thinks", block.get("thinking", ""))
        elif block_type == "tool_use":
            name = block.get("name", "tool")
            arguments = block.get("input") or {}
            described = (
                arguments.get("command")
                or arguments.get("file_path")
                or arguments.get("pattern")
                or arguments.get("path")
                or json.dumps(arguments)
            )
            kind = "edits" if name in {"Edit", "Write", "NotebookEdit"} else "command"
            yield TranscriptEvent(
                kind, f"{name}: {described}", json.dumps(arguments)
            )
        elif block_type == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(
                    part.get("text", "") for part in body if isinstance(part, dict)
                )
            status = "error" if block.get("is_error") else "ok"
            yield TranscriptEvent(
                "result", f"{status}: {_clip(body or '', 120)}", _tail(body or "")
            )


def iter_claude_events(payload: dict) -> Iterator[TranscriptEvent]:
    event_type = str(payload.get("type", ""))
    if event_type == "system":
        if payload.get("subtype") == "init":
            yield TranscriptEvent(
                "thread",
                f"session {payload.get('session_id', '?')} "
                f"model {payload.get('model', '?')}",
            )
        return
    if event_type in {"assistant", "user"}:
        message = payload.get("message")
        if isinstance(message, dict):
            yield from _claude_blocks(message)
        return
    if event_type == "result":
        subtype = payload.get("subtype", "?")
        turns = payload.get("num_turns", "?")
        cost = payload.get("total_cost_usd")
        cost_text = f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        yield TranscriptEvent(
            "turn",
            f"{subtype} after {turns} turn(s){cost_text}",
            str(payload.get("result", "")),
        )
        return
    if event_type == "error":
        yield TranscriptEvent("error", json.dumps(payload))


# Stream -----------------------------------------------------------------


def parse_line(line: str, agent: str) -> list[TranscriptEvent]:
    """Parse one line of agent output. Never raises on malformed input."""
    stripped = line.strip()
    if not stripped:
        return []
    if not stripped.startswith("{"):
        return [TranscriptEvent("output", stripped)]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [TranscriptEvent("output", stripped)]
    if not isinstance(payload, dict):
        return [TranscriptEvent("output", stripped)]
    if agent == CLAUDE:
        return list(iter_claude_events(payload))
    event = parse_codex_event(payload)
    return [event] if event else []


def parse_stream(lines: Iterable[str], agent: str) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    for line in lines:
        events.extend(parse_line(line, agent))
    return events


def render(events: Iterable[TranscriptEvent], *, width: int = _SUMMARY_LIMIT) -> str:
    return "\n".join(redact(event.line(width=width)) for event in events)


def final_message(events: Iterable[TranscriptEvent]) -> str | None:
    """Return the last thing the agent said, which is its report."""
    said = [event.detail or event.summary for event in events if event.kind == "says"]
    return said[-1] if said else None
