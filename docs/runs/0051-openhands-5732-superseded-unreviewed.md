# 0051. OpenHands software-agent-sdk #5732, superseded before review

Date: filed 2026-10-13 04:54 UTC. The competing fix merged 2026-10-27 11:54
UTC; we found it on 2026-11-10. Private run `20261013T013750Z-128e9d`.
Sanitized from local evidence and the public threads.

Nobody reviewed our pull request. A member wrote the same fix twelve days
later, and two maintainers approved it.

## Target and defect

`OpenHands/software-agent-sdk`, issue [5723](https://github.com/OpenHands/software-agent-sdk/issues/5723): `Skill.to_skill_info()` fills
`SkillInfo.triggers` from `get_triggers()`, which returns `[]` for a
`PathTrigger`, so path-triggered skills reach API clients without their globs.
Our [#5732](https://github.com/OpenHands/software-agent-sdk/pull/5732) and the winning [#6213](https://github.com/OpenHands/software-agent-sdk/pull/6213) change the same two source files,
`openhands-sdk/openhands/sdk/skills/skill.py` and
`clients/typescript/src/models/api.ts`. Ours also added a test file.

## What happened

| UTC | Event |
| --- | --- |
| 2026-10-12 12:56 | Issue labelled `ready-for-dev`, `priority:low` |
| 2026-10-13 04:54 | #5732 filed. `Validate PR description` fails: the template's `HUMAN:` section is still the placeholder |
| 2026-10-25 04:18 | hieptl opens #6213 with the `HUMAN:` section filled |
| 2026-10-27 11:54 | #6213 merges after all-hands-bot and tofarr approve; issue closed |
| 2026-11-10 | `mailman contributions --refresh` flags the merged competitor; `hunt watch` never did |

No comment or review ever landed on #5732.

## What the harness did

| Stage | Result |
| --- | --- |
| Policy screen | `requires_own_words` false. The template tells AI agents not to edit the `HUMAN` section and asks the human author to write it; no pattern matched |
| Filing | Filed with the placeholder, so the required check stayed red |
| Follow-up | Every pr-followup from 2026-11-01 to 2026-11-09 listed the `HUMAN:` note as the operator's item. None noticed the merged competitor |

## Lessons

- **A required human section is an own-words target.** The template reserves
  a section for the person and a CI check enforces it. The screen now records
  `requires_own_words` for that wording, and the standing rule drops such
  targets (Mailman #515).
- **The watch must read competitors.** `contributions --refresh` stored the
  merged #6213 in provenance, but `hunt watch` read only our own pull request,
  so the row looked like routine attention for two weeks. The watch now raises
  attention for a merged competitor (Mailman #515).
