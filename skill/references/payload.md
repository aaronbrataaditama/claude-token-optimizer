# Reducing a tool result

Load when a tool result is over budget, or you are choosing how to represent one.

## Before the call — upstream beats everything

Reducing a payload after it arrives is the fallback. Ask for less first:

- Field selection: `fields=id,name,status`, `SELECT id, name, status` — never `SELECT *`.
- Server-side `limit`, filter, sort, aggregate, paginate.
- A `detail=minimal|standard|full` parameter, or a search/summary endpoint, where the tool offers one.
- The relevant section of a document, not the document.

A filter applied at the source costs nothing to transfer, nothing to parse and nothing to reduce.

## The ladder — escalate only as far as the budget requires

Write the full payload to the archive **first**, so every drop has a recovery path. Then:

1. Remove nulls and empties.
2. Remove fields irrelevant to the question. Keep anything an authorization decision depends on.
3. Remove duplicate records.
4. Rank records by relevance; keep the top N.
5. Truncate long low-value string fields.
6. Summarise repetitive content.
7. Project to the needed columns; paginate.
8. Store the whole thing externally, return a summary plus a reference.

Stop at the first rung that fits. Never blindly truncate data that could change the answer.

## Representation — measure, do not assume

YAML is not automatically smaller than JSON. On payloads dominated by long string values it is usually larger. Estimate two or three candidates against the target model's tokenizer and take the smallest that preserves the semantics:

| Shape | Usually smallest as |
|---|---|
| Uniform records, many rows | CSV or line-oriented |
| Deep nesting, few values | Compact JSON |
| Flat key/value, long strings | Key-value lines |
| Repetitive with a few varying fields | Header + delta rows |
| Large, mostly unread | Summary + reference |

## The drop manifest — never optional

Anything removed gets one line, rendered with the result:

```
[reduced 4,312 → 50 records (low_rank); full set at result:9f3a1c;
 retrieve_with fetch_result("9f3a1c", range=51..4312)]
```

Reasons are one of: irrelevant, duplicate, low_rank, truncated, budget, summarized. About thirty tokens, and it prevents a whole class of confident wrong answers — the forbidden outcome is reduced data that looks complete.

## Correctness rules

- Trust label travels with the result unchanged. A summary of untrusted content is untrusted content.
- Never drop a field because it looks irrelevant if a permission or tenancy check reads it.
- Code, identifiers, schemas and exact numbers are never summarised in place. Keep the canonical version in the archive and reference it.
