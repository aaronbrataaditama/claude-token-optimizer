# Managing context under pressure

Load when context utilisation crosses the prune threshold, at a task transition, or before any summarisation.

## The thing most people get wrong

Not all tokens cost the same to remove. A token **before** the last cache breakpoint is cached: removing it also destroys the discount on everything after it, every remaining turn. A token **after** the breakpoint is not cached: removing it saves exactly one token.

So summarising old conversation — the intuitive move — is frequently the most expensive option available.

## Order of relief

1. **Externalise large recent items.** Tool results and artifacts from this turn or task, sitting outside the cache. Store, leave a reference and a manifest line. Zero cache cost.
2. **Externalise task-scoped items** after the last breakpoint.
3. **Convert to retrieve-later** anything cheap to fetch again.
4. **Compact the cached head** — only if the arithmetic is positive, or you are about to overflow.

Steps 1–3 usually free enough. Reach step 4 rarely.

## The compaction test

```
gain = tokens_removed × input_price × remaining_turns
loss = cached_tokens_after_the_edit × cached_discount × remaining_turns
       + the summarisation call itself

compact only if gain − loss > 0
```

If it is negative, defer. Batch pending compactions and apply them in one edit at the next cache boundary: session start, task change, or cache expiry. Two separate compactions of the same prefix pay the invalidation twice for one benefit.

**Exception:** if the turn will not otherwise fit, compact anyway. A failed turn costs more than a cold cache.

## Deciding per item

Compare the cost of keeping it against the cost of getting it back:

| | When |
|---|---|
| **Retain** | Relevant now and cheap to keep |
| **Compress** | Needed often, and losslessly representable |
| **Externalise** | Large, might be needed, store and reference |
| **Discard** | Low future value **and** cheap to reconstruct |
| **Retrieve later** | Fetching again costs less than holding it |

A 20 KB response summarised to 2 KB is the wrong answer if you need one field from it five turns later. The right answer is a reference plus an on-demand fetch of that field.

## Never

- Summarise the same content twice.
- Delete a message that a later decision depended on — trace the dependency before removing.
- Compress requirements, constraints, decisions, IDs, names, dates, numbers, configuration, code, API contracts, unresolved issues or commitments.
- Rewrite earlier turns cosmetically. Reformatting invalidates the cache for no gain.
- Let a summary of untrusted content become trusted. The label survives compression.

If a summary is uncertain, keep the original externally and hold a pointer.
