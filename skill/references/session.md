# Session length

Load when a session has run long, when a task finishes, or when the user asks about context or cost.

## Why this is the biggest lever

Context never shrinks on its own. Measured across a real multi-project corpus:

```
turn    1     prompt    41,500     the floor: instructions, tools, memory, environment
turn   40     prompt    96,900     ~1,000 tokens added per turn
turn  150     prompt   207,800
turn  400+    prompt   297,700     6.4x a turn-10 prompt, for identical work
```

**Turns past #100 were 79% of turns but 92% of all prompt tokens.** Nothing else available at runtime comes close: capping sessions near 100 turns is worth roughly half the cost of the long ones.

## When to offer a fresh start

Offer, do not insist. The user decides. Good moments:

- A task just completed and the next one is unrelated
- The session has run long and is now mostly history the current work does not need
- You are repeatedly re-reading things because earlier context has been compacted away
- The user says things are slow or expensive

Bad moments: mid-debug, mid-refactor, or anywhere the accumulated reasoning is the thing of value. Losing context you actually need costs more than the tokens save.

## The handover

The point is to make restarting cheap. A few hundred tokens that replace tens of thousands:

```
Objective     what we are trying to achieve, one line
Decisions     what has been settled, and why - the reasoning you would otherwise redo
State         what is done, what is in progress, what is untouched
Files         the paths in play, with a word on each - not their contents
Next step     the single next action
Open          unresolved questions, blockers, things deliberately deferred
```

Write it to a file so the next session reads it instead of you re-typing it. Keep it to what cannot be re-derived from the repository — the code is already on disk; the *reasoning* is not.

## What not to put in it

- File contents. Paths only. The next session can read what it needs.
- Anything recoverable from git, the issue tracker, or the code itself.
- A transcript summary. This is state, not narrative.
- Tool output. If it mattered, it is a decision or a fact; write that instead.

## Within a session

Between full restarts, the same principle applies at smaller scale:

- At a task boundary, state what is now settled. It makes later compaction lossless where it matters.
- Do not re-read files to "refresh" — if it is in context it is current; if it was compacted away, read only the part you need now.
- Prefer one targeted read over three exploratory ones. Exploration is what fills a context.
