# Target policies

A target policy records what one upstream project asks of a contributor, taken
from that project's own written policy and nothing else. `mailman
prepare-submission` reads one and refuses to call a submission ready when the
change or the run's evidence does not meet it.

Every file here carries `policy_read_on`. A policy is a claim about another
project on a particular day, and these change. Re-read before relying on one.

`stance` is the field that decides whether preparation can proceed at all:

- `permitted` — AI-assisted contributions are accepted.
- `permitted_with_disclosure` — accepted, and the pull request has to say so.
- `restricted` — accepted only under conditions a human has to satisfy first.
  Preparation is blocked and names the condition.
- `forbidden` — not accepted. Preparation is blocked.
- `unknown` — nobody has read the policy. Preparation is blocked. This is the
  default, on purpose: an unread policy must never behave like a permissive one.

`ai_trailer` differs sharply between projects and is not cosmetic. pytest
appreciates a `Co-authored-by:` trailer crediting the tool; attrs closes any pull
request that carries one; the Linux kernel and Kubernetes want an `Assisted-by:`
line instead. Set it from the project's text, not from habit.

The three files here are the three shapes seen so far: `pytest.json` permits with
disclosure and wants a changelog fragment, `starlette.json` permits and makes a
duplicate search mandatory, and `attrs.json` is `restricted` and exists as the
case Mailman has to refuse rather than as a target.
