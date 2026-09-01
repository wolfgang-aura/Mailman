# 0002. Wrap installed agent CLIs without bypassing their sandboxes

Status: accepted on 2026-09-02.

## Decision

Use installed Codex and Claude CLIs as the first `EngineeringAgent` implementations. Pass prompts through stdin. Capture stdout, stderr, exit code, duration, timeout state, and a separate final report under the private run directory.

Codex runs through stable non-interactive `codex exec` with an ephemeral session. Primary runs use `workspace-write`; reviewer runs use `read-only`. Native Windows runs explicitly select the preferred elevated Windows sandbox. Mailman ignores ordinary user configuration so a run does not silently inherit a different model, plugin, or tool setting.

Claude runs through non-interactive print mode. Primary runs use `acceptEdits`; reviewer runs use `plan`. The adapter denies common upstream write commands and never enables `--dangerously-skip-permissions`.

Mailman does not advance workflow status when an agent process exits. Process success, a report, a candidate diff, and successful verification are different facts.

## Evidence

The first Codex attempt could not start any repository command because ignoring user configuration also removed the native Windows sandbox implementation choice. Adding only `windows.sandbox='elevated'` fixed command and edit access while retaining workspace boundaries. The verified adapter then produced the expected one-line fixture patch. Host-side unittest verification passed.

The agent could not access the user-installed Python runtime inside its sandbox account. Mailman now records explicitly probed executables in a private per-run toolchain manifest. Each record includes the exact path, version probe, and SHA-256 digest. Prompt preparation rejects a changed binary. A later fixture run used a bundled Python executable successfully without inheriting the host's full environment. See [fixture run 0001](../runs/0001-codex-fixture.md).

The Codex flags were checked against installed CLI 0.152.0 and the current [OpenAI developer command reference](https://learn.chatgpt.com/docs/developer-commands). Claude flags follow Anthropic's [CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage), but the adapter remains locally unverified until Claude CLI is installed and authenticated.
