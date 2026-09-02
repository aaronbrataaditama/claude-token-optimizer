# Deliverable 1 — the runtime skill

**Recalibrated against measurement.** What the primary agent actually loads.

## The budget

`SKILL.md` is paid on **every request of every session**. It sits inside a measured **~41,500-token
session floor** — the instructions, tool schemas, skill listings, agent listings and memory that load
before any work happens — and every token of it is multiplied by every turn. The specification's
ceiling is 500–800 tokens, enforced at deploy time by invariant V15.

That guard fired during the recalibration: the rewritten `SKILL.md` came in at 818 tokens and had to
be trimmed to 777 before it could ship. The ceiling is not a formality.

Measured against a generic estimator (the deploy check re-measures with the target model's tokenizer):

| File | Chars | ~Tokens | Ceiling | Loaded |
|---|---:|---:|---:|---|
| `SKILL.md` | 2,951 | **777** | 800 | Always |
| `references/session.md` | 2,634 | 693 | 900 | Session has run long, or a task just finished |
| `references/payload.md` | 2,663 | 701 | 900 | A result is over budget, or choosing a representation |
| `references/tools.md` | 2,368 | 623 | 900 | Choosing tools or schema depth |
| `references/bypass.md` | 2,391 | 629 | 900 | Data-heavy task detected |
| `references/context.md` | 2,751 | 724 | 900 | Under budget pressure, or before summarising |
| `references/delegation.md` | 2,791 | 734 | 900 | Considering a sub-agent or parallel branches |
| `references/failure.md` | 2,671 | 703 | 900 | Tool error, exhausted retry, suspected loop |
| `references/memory.md` | 3,006 | 791 | 900 | Retrieving, persisting, or resuming |

**Why the ceilings differ.** The always-loaded file is billed on every turn of every session; a
reference is billed once, on the turn that needs it. A 900-token reference firing on 10% of turns
costs 90 tokens per turn amortised — an eighth of what the same content costs living in `SKILL.md`.
That ratio is the entire argument for progressive disclosure.

## What the measurement changed

The first version of this skill was calibrated for the modelled reference deployment. Measured
against real transcripts it was **inert**: its fast-path gate said "do nothing when context is under
35% full", and on a 1M window that is 350,000 tokens — above the p90 prompt of 316,000. The skill
concluded "small turn, do nothing" on roughly 90% of turns.

Three changes followed:

* **Dropped the utilisation gate entirely from the runtime instructions.** A fraction of a large
  window is not a cost signal. What matters is what accumulates.
* **Reordered around what actually drives cost.** Context volume is 93% of spend, and 92% of prompt
  tokens sit in turns past #100. So the skill now leads with *what you leave in context*, not with
  *whether to optimise this turn*.
* **Added `references/session.md`.** Session length is the largest lever available at runtime and the
  original skill said nothing about it.

Grounding: [`../docs/phase0-findings.md`](../docs/phase0-findings.md) §4b–4c.

## Structure

```
skill/
  SKILL.md              always loaded · the decision spine
  references/
    session.md          session length and the handover  ← the largest runtime lever
    payload.md          reducing a tool result
    tools.md            selecting tools and loading schemas
    bypass.md           code-execution bypass
    context.md          managing context under pressure
    delegation.md       delegating to a sub-agent
    failure.md          when something fails
    memory.md           memory
```

## Design rules the file obeys

* **Cache-stable.** Content does not vary by turn, task or tenant. Anything that would vary belongs
  in configuration — a skill that changes between turns invalidates the prefix it sits in.
* **Decisions, not explanations.** Rationale lives in `docs/` for humans and in the references for
  the situations that need it.
* **Correctness rules are unconditional and stated in the always-loaded file.** Trust propagation,
  never summarising code or identifiers, and resolving ties toward correctness cannot wait for a
  reference to load: by the time the situation is recognisable, the mistake is usually made.
* **No examples in the always-loaded file.** Examples are the most token-expensive form of
  instruction and the first thing to push into a reference.

## A known gap

The design assumes this file is **always loaded**. Claude Code loads skills **on demand**, when the
description matches. So the three unconditional correctness rules are absent on turns where the skill
does not trigger — which is exactly when they matter.

The mechanism for genuinely always-on instructions in Claude Code is `~/.claude/CLAUDE.md`. Moving
the non-negotiables there, and leaving the situational procedures here, would close the gap. That is
a per-user decision and has not been made.

## Changing it

Editing `SKILL.md` invalidates every cached prefix in every live session, so it is a **restart-only**
change (`docs/configuration.md` §7). Batch edits into a deploy; three separate one-line clarifications
cost three full cache rebuilds for one benefit.

Installed copy lives at `~/.claude/skills/token-optimization/`. Remove with
`rm -rf ~/.claude/skills/token-optimization`.
