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

`requires_own_words` is separate from `stance`, because a project can permit the
code and refuse the prose. freqtrade's contributing guide says "Never let an LLM
speak for you" and asks that comments, issues and pull request descriptions be in
the author's own words. Mailman's draft body is model-written, so under that rule
the body is the violation however good the patch is. Preparation blocks with
`policy-requires-own-words` until a person has rewritten the body and set
`own_words_confirmed`. The screen's policy gate reports the sentence that decided
it, so the constraint reaches the run instead of being summarised into a verdict
word.

The files here are the shapes seen so far: `pytest.json` permits with disclosure
and wants a changelog fragment, `starlette.json` permits and makes a duplicate
search mandatory, `freqtrade.json` permits the code and requires the author's own
words, and `attrs.json` is `restricted` and exists as the case Mailman has to
refuse rather than as a target.
