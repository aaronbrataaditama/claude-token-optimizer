# Selecting tools and loading schemas

Load when choosing tools for a task, or deciding how much of a schema to bring in.

## Before calling anything

Ask, in order:

1. Is the answer already in context, memory, or a result you already have?
2. Can several operations be combined into one call, or run in parallel?
3. Can generated code replace the round-trips entirely? → `bypass.md`
4. Is the expected information worth the cost?

That last one is a real comparison, not a vibe: the cost includes tokens **and** API charges, database compute, search fees, execution time, rate-limit consumption and latency. The cheapest operation is not the one with the fewest tokens. A database scan returning 200 tokens can cost more than a call returning 8,000.

Call the tool only when the answer plausibly changes what you do next.

## Disclosure levels — escalate only as needed

```
0  name + one-line description        picking a candidate
1  capability + required parameters   confirming it fits
2  full schema                        actually calling it
3  documentation and examples         it failed, or the call is unusual
```

Most selection happens at level 0. Load level 2 for the tools you are about to call, not for the shortlist.

## Compact signatures, not raw JSON Schema

When a schema appears as context text, write it as a typed signature:

```
search(query: string, limit?: number, fields?: string[]): SearchResult[]
```

not as `{"$schema":..., "type":"object", "properties":{...}, "additionalProperties":false}`. Typically 40–60% fewer tokens with no loss of accuracy. This applies to the context representation only — the provider's tool-calling API may still need full JSON Schema on the wire.

## The cache trade-off

Swapping tool schemas in and out every turn invalidates the cached prefix, and everything after the edit point re-bills at full price. For a short session or a hot tool set, loading a slightly larger but **stable** tool set once is cheaper than churning a precise one.

Change the tool set when the task genuinely changes. Do not re-derive it each turn.

## Choosing between candidates

Prefer the tool that: accepts filters (so reduction happens upstream), returns the smallest sufficient result, is idempotent, and you have called successfully before for this shape of task. Prefer one batched call over N per-record calls, always.
