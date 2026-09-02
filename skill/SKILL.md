---
name: token-optimization
description: Keep the context small so long sessions stay cheap and correct. Use when a tool result or command output is large, when a session has run long, when choosing tools or models, before delegating to a sub-agent, when work is data-heavy enough to script instead, or when the user mentions token cost, context, or optimisation.
---

# Token Optimization

The always-on rules live in `CLAUDE.md`: locate before retrieving, bound command output, never re-read, and never trade correctness for tokens. This skill is the procedures for specific situations.

## A result or command output is too big

Archive first, reduce second, and leave one line saying what you dropped and how to recover it. Reduced data must never look complete.

For a concrete reduction, pipe it: `<command> | python tools/reduce.py --budget 4000 --query "<what you need>"`. It removes empty fields, drops irrelevant ones, deduplicates with counts, ranks records, preserves totals, and prints a manifest. It never summarises, so it cannot fabricate.

→ `references/payload.md` for the ladder and what each rung costs you.

## The session has run long

Offer a handover — objective, decisions, files in play, next step — rather than accumulating. Know when *not* to: mid-debug or mid-refactor, the accumulated reasoning is the thing of value.
→ `references/session.md`

## Context is under pressure now

Externalise the newest large items first: they sit outside the cache, so removing them costs nothing. Summarising old context invalidates the cached prefix and usually costs more than it saves.
→ `references/context.md`

## The work is data-heavy

Write code that queries, joins and aggregates outside the context and returns the answer, not the data. Beats any filtering pipeline when the payload would cross the boundary more than once.
→ `references/bypass.md`

## Choosing tools or how much schema to load

Smallest schema that works, filters pushed upstream, and one batched call over N per-record calls.
→ `references/tools.md`

## Considering a sub-agent or parallel work

Only when cheaper end to end. A sub-agent gets a role, the tools its subtask needs, named inputs and an output schema — never your context, never this skill.
→ `references/delegation.md`

## Something failed

Roll back. One line: what failed, why, what to try next. Never repeat a failed call unchanged — fix the input, then raise reasoning effort, then the model.
→ `references/failure.md`

## Deciding what to persist or retrieve

Honour `as_of`; a superseded fact is not current. Write at turn boundaries, never mid-loop.
→ `references/memory.md`
