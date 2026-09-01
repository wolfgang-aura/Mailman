# Security policy

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose credentials, private source code, or a host machine. Use GitHub's private vulnerability reporting for this repository when it is available.

## Sensitive data rules

Mailman executes tools and stores their output. That output may contain access tokens, local paths, private source code, issue text, or data printed by a failing test.

- Live run artifacts belong under `.mailman/` and are ignored by Git.
- Mailman records only a small allowlist of environment metadata.
- Captured output passes through best-effort token redaction. Redaction is not proof that an artifact is safe to publish.
- Public run exports require human review before commit or upload.
- Credentials must come from the host's credential store or ignored environment files. Never put a real secret in a tracked configuration file.

If a secret enters Git history, revoke it first. Removing the text from the latest commit is not enough.
