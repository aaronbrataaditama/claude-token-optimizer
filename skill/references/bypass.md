# Code-execution bypass

Load when a task is data-heavy: large result sets, joins across sources, aggregation, filtering over many records, or the same call repeated per record.

## Why it wins

Filtering a payload still makes it cross the context boundary. Running code means it never crosses at all.

```
Through context:  tool A (5,000 tok) → you → tool B (8,000 tok) → you → join → answer
Bypass:           40-line script → calls A and B, joins, filters, aggregates → 200 tokens → answer
```

On data-heavy work this beats any filtering pipeline by an order of magnitude. Check it **before** deciding which tools to load, not after.

## Triggers

- More than ~5,000 tokens of payload expected in total.
- More than three homogeneous calls, or one call per record in a loop.
- Any join, group-by, aggregate, sort-then-top-N, or cross-source reconciliation.
- A question about the data as a whole, rather than about specific records.

## Do not bypass when

- The answer needs the records themselves, not a derivation of them.
- The work is a single small call.
- The operation mutates something and the program cannot be made idempotent.
- You cannot state the return contract in advance.

## Writing the program

1. Import the tools you need — schemas are discovered in the environment, so they never enter context.
2. Fetch, join, filter and aggregate **inside** the environment. Intermediate data stays there.
3. Return only the answer: an aggregate, a selection, a verdict, a small table. Not the working set.
4. State the return size contract up front. Returning 40,000 tokens means the bypass failed at its one job.
5. Print what you dropped, in the same manifest form a filtered payload would carry.

## Boundaries

The generated code runs under exactly the same authorization, tenancy and network limits as a direct tool call. Sandbox limits — CPU, memory, wall clock, egress bytes, network allowlist — apply and are enforced by the runtime, not by the program's good behaviour.

## If it fails

Do not retry the same program. Classify first: a deterministic error (bad column, wrong argument) is fixed by editing the program; a resource breach means the program is trying to move too much data and should aggregate earlier; anything else falls back to native tool calls with normal filtering. That fallback is always available and always correct — it is just the expensive path.
