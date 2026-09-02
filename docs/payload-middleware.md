# Deliverable 3 — Payload Middleware

**Stage 3 of 5.** Production pseudocode for the reduction pipeline (C12), with each operation's effect
on correctness.

Companion documents: [`architecture.md`](architecture.md) (C12) ·
[`context-manager.md`](context-manager.md) (D4) · [`configuration.md`](configuration.md) (D9) ·
[`../skill/references/payload.md`](../skill/references/payload.md) (the runtime instruction).

---

## 1. Position and contract

C12 stands between a tool result and the context. It is the **last** line of defence, not the first:
by the time a payload reaches it, the transfer has already been paid for. Upstream reduction — field
selection, server-side filtering, pagination, aggregation — is always preferred and is decided in the
Decision Engine at stage S12, before the call. C12 exists for the tools you cannot change.

```ts
interface PayloadMiddleware {
  reduce(raw: RawPayload, budget: TokenBudget, task: TaskClass): ReducedPayload;
}
```

Three properties hold on every path through this component:

1. **Archive first.** The complete payload is written to C19 before a single byte is dropped, so every
   manifest entry names a recovery that actually exists (INV-6).
2. **Trust is carried, never recomputed.** The label on the reduced result is the label on the raw
   result. Reduction is not a laundering step (INV-5).
3. **It always returns.** Every failure path degrades to raw-passthrough-under-hard-budget. A
   middleware exception must not fail a turn (INV-2).

---

## 1.4 Implementation status

Rungs 1-5 and 7-8 are implemented in [`../tools/reduce.py`](../tools/reduce.py) as a standalone
pipe, usable without any harness integration:

```bash
<command> | python tools/reduce.py --budget 4000 --query "what you need"
```

**Rung 6 (summarize) is deliberately not implemented there.** It is the only rung that can fabricate
and it requires a model; a deterministic reducer that cannot invent is worth more than one that can.
A caller that wants summarization has a model available and should do it explicitly.

Everything else in this document - the orchestrator's budget re-checks, the drop manifest travelling
with the result into context, the trust label - needs to sit inside the request loop and is not
buildable from outside the harness. See [`phase0-findings.md`](phase0-findings.md).

## 2. The ladder

Eight rungs, escalated only as far as the budget requires.

| # | Rung | Omits | Can fabricate | Reversible from archive |
|---|---|---|---|---|
| 1 | `removeNulls` | Semantically empty values | No | Yes |
| 2 | `removeIrrelevantFields` | Whole fields | No | Yes |
| 3 | `deduplicate` | Repeated records | No | Yes |
| 4 | `rankRecords` | Low-ranked records | No | Yes |
| 5 | `truncate` | Tails of long strings | No | Yes |
| 6 | `summarize` | Detail, in exchange for prose | **Yes** | Yes |
| 7 | `projectAndPaginate` | Columns and pages | No | Yes |
| 8 | `externalize` | Nothing — replaced by a reference | No | Yes |

**The distinction that matters is column four.** Rungs 1–5 and 7–8 produce *absent* information; the
agent sees a manifest entry and can retrieve. Rung 6 produces *different* information — a summary can
assert something the source did not say. It is the only rung that requires validation
(`validateSummary`, D4 §8), the only one gated by `compression.summary_confidence_min`, and the first
one to be skipped when the aggressiveness level is `conservative`.

**Rung 8 is never capped by aggressiveness.** `compression.levels.*.max_ladder_rung` bounds the
*lossy* rungs; externalization is lossless and must always be reachable, or a `conservative`
deployment would have no way to fit an oversized payload except by failing.

---

## 3. `filterPayload` — the orchestrator

```text
function filterPayload(raw, budget, task, ctx) -> ReducedPayload:

  # --- 0. Guarantees established before anything is touched -----------------
  trust    <- raw.trust                                   # carried, never recomputed (INV-5)
  ref      <- archiveContext([raw]).first()               # D4 §7; sanitized before write
  manifest <- DropManifest.empty()

  norm <- normalizePayload(raw)                           # §4
  size <- estimateTokens(norm.content, ctx.model.tokenizer)   # §11

  # --- 1. Already fits: choose a representation and stop --------------------
  if size.tokens <= budget.tokens:
      rendered <- selectRepresentation(norm, ctx.model)   # §10
      return ReducedPayload {
          content: rendered, size: exactTokens(rendered), manifest, trust,
          archive_ref: ref, representation: rendered.kind
      }

  # --- 2. Climb the ladder --------------------------------------------------
  level    <- cfg.compression.levels[cfg.compression.aggressiveness]
  max_rung <- level.max_ladder_rung
  p        <- norm

  for rung in cfg.middleware.ladder.rungs:
      if rungIndex(rung) > max_rung and rung not in cfg.middleware.ladder.always_available:
          continue                                        # aggressiveness cap; §2
      if not applicable(rung, p, task):
          continue                                        # e.g. dedupe on a non-record payload

      before <- p
      p, entries <- applyRung(rung, p, budget, task, ctx)
      manifest.add(entries, recover_from = ref)

      if entries.isEmpty():
          continue                                        # rung was a no-op; do not re-estimate
      size <- estimateTokens(p.content, ctx.model.tokenizer)
      if size.tokens <= budget.tokens:
          break
      if regressed(before, p):                            # a rung that grew the payload
          p <- before; manifest.revert(entries); alarm(MIDDLEWARE_RUNG_REGRESSION, rung)

  # --- 3. Final gate: this cannot fail --------------------------------------
  return enforceBudget(p, budget, manifest, trust, ref, ctx)                # §12
```

**Why `applicable` is checked before cost.** Running dedupe over a single object, or ranking over an
unordered map, costs CPU and returns nothing. The check is a shape test against the normalization
result and is why `normalizePayload` runs first.

**Why the payload is re-estimated only after a rung that changed something.** Token estimation over a
large payload is not free (§11); estimating after a no-op rung is pure waste, and on an eight-rung
ladder that is up to seven wasted passes.

**`regressed` exists because reduction can enlarge.** Converting a compact array to a header-delta
form, or adding multiplicity annotations during dedupe, can cost more than it saves on small inputs.
The rung is reverted and the event alarmed rather than silently accepted.

**Config:** `compression.aggressiveness`, `compression.levels.*.max_ladder_rung`,
`middleware.ladder.*`, `budgets.per_tool.*`.
**Demotion:** any exception inside a rung is caught, the rung reverted, and the ladder continues.
An exception in the orchestrator itself falls through to `enforceBudget` on the normalized payload.

---

## 4. `normalizePayload`

Establishes the payload's shape once, so every later rung can ask cheap structural questions instead
of re-walking the tree.

```text
function normalizePayload(raw) -> NormalizedPayload:

  parsed <- parseByContentType(raw)                       # json | ndjson | csv | xml | text | binary
  if parsed.is_error: return NormalizedPayload.opaque(raw)   # never guess at unparseable bytes

  stats <- walk(parsed) collecting:
      record_count, max_depth, key_frequency, mean_string_len, p95_string_len,
      null_count, empty_count, distinct_key_count, uniform_keys (bool),
      total_bytes, leaf_count

  shape <- classify(stats):
      UNIFORM_RECORDS     if record_count > 1 and uniform_keys and max_depth <= 3
      REPETITIVE_DELTA    if UNIFORM_RECORDS and varyingKeyRatio(stats) < 0.35
      DEEP_NESTED         if max_depth > 3
      FLAT_LONG_STRINGS   if max_depth <= 2 and mean_string_len > 120
      SINGLE_LARGE_TEXT   if leaf_count <= 2 and total_bytes > 4096
      OPAQUE              otherwise

  # Flatten only where it is unambiguous. A single-child wrapper object adds a
  # key and a nesting level and carries no information.
  if shape in {UNIFORM_RECORDS, REPETITIVE_DELTA}:
      parsed <- unwrapSingleChildContainers(parsed)

  return NormalizedPayload { parsed, stats, shape, envelope: raw.envelope }
```

**Correctness.** Flattening is restricted to single-child containers because collapsing
`{"data": {"items": [...]}}` to `[...]` is safe, while collapsing `{"result": X, "meta": Y}` loses the
distinction between result and metadata. `OPAQUE` is a real outcome: an unparseable payload skips
rungs 1–7 entirely and goes straight to truncation or externalization, because guessing at structure
in binary or malformed data is how fields get silently corrupted.

**Complexity:** one O(n) walk. All later shape questions are O(1) lookups against `stats`.

---

## 5. `removeNulls`

```text
function removeNulls(p, policy) -> (p, entries):

  removed <- 0
  for each leaf in p.parsed:
      drop <- false
      if leaf.value is null    and policy.drop_explicit_null:   drop <- true
      if leaf.value == ""      and policy.drop_empty_string:    drop <- true
      if leaf.value is []      and policy.drop_empty_collection: drop <- true
      if leaf.value is {}      and policy.drop_empty_collection: drop <- true
      if leaf.key in protectedFields(p): drop <- false          # §6
      if drop: remove(leaf); removed++

  if removed == 0: return (p, [])
  return (p, [ DropEntry { what: "{removed} null/empty leaves",
                           reason: IRRELEVANT, recoverable: true } ])
```

**Correctness.** `drop_explicit_null` defaults to **false**, which is the opposite of the obvious
choice. In many APIs an explicit `null` is a positive statement — *this field was checked and has no
value* — and is distinguishable from the field being absent, which means *not evaluated*. Collapsing
the two changes what the agent can conclude. Empty strings and empty collections carry that
distinction far less often, so they default to droppable.

A protected field is never dropped even when null, because its absence changes an authorization or
join decision downstream.

---

## 6. `removeIrrelevantFields`

The highest-yield rung on most real payloads, and the one with the most ways to go wrong.

```text
function protectedFields(p, ctx) -> Set<FieldPath>:
  return union(
      matching(p, cfg.middleware.relevance.protected_field_patterns),  # acl, scope, tenant, owner…
      p.tool.declared_identity_fields,        # primary keys
      p.tool.declared_join_keys,
      fieldsNamedIn(ctx.query),
      fieldsReferencedBy(ctx.pending_actions),
      securityRelevantFields(p)               # anything C21 marks as authorization-bearing
  )

function removeIrrelevantFields(p, ctx) -> (p, entries):

  protected <- protectedFields(p, ctx)
  scores    <- {}

  for field in p.stats.distinct_keys:
      if field in protected: scores[field] <- 1.0; continue
      scores[field] <- max(
          lexicalOverlap(field, ctx.query),                 # cheap, deterministic
          taskFieldPrior(field, ctx.task.workload),         # learned from the ledger
          declaredUsefulness(p.tool, field)                 # from the tool registry, if present
      )

  dropped <- [ f for f in scores if scores[f] < cfg.middleware.relevance.min_field_relevance ]
  if dropped.isEmpty(): return (p, [])

  # A field that is large AND borderline is a better candidate than a field that
  # is small and clearly irrelevant: dropping the latter saves nothing.
  dropped <- sortBy(dropped, f => -bytesOf(p, f))
  dropped <- takeWhile(dropped, f => bytesOf(p, f) >= cfg.middleware.relevance.min_field_bytes)

  remove(p, dropped)
  return (p, [ DropEntry { what: "fields: " + join(dropped),
                           reason: IRRELEVANT, recoverable: true,
                           retrieve_with: fetch_result(ref, fields=dropped) } ])
```

**Correctness.** Three rules keep this rung from being the component's most dangerous:

* **No scoring decides a protected field.** Authorization, tenancy, identity and join fields short-circuit
  to relevance 1.0 before any heuristic runs. The specification's warning — never remove a field that
  appears irrelevant if a security decision needs it — is enforced structurally, not by tuning the
  threshold.
* **Size gates the drop.** Removing a 12-byte field that scored 0.14 is a rounding error in tokens and
  a real chance of losing something needed. Only fields above `min_field_bytes` are dropped.
* **The manifest names the fields.** Not "some fields removed" — the actual list, and the call that
  brings them back. An agent that can see `body_html` was dropped can ask for it; an agent told
  "irrelevant fields removed" cannot.

---

## 7. `deduplicate`

```text
function deduplicate(p, ctx) -> (p, entries):

  if p.shape not in {UNIFORM_RECORDS, REPETITIVE_DELTA}: return (p, [])

  key <- p.tool.declared_identity_fields
         ?? canonicalContentHash                          # order-insensitive, null-normalized

  seen <- {}                                              # key -> first index
  count <- {}
  for i, record in p.records:
      k <- key(record)
      if k in seen: count[k]++ ; markForRemoval(i)
      else:         seen[k] <- i ; count[k] <- 1

  if noneMarked(): return (p, [])
  removeMarked(p)

  if cfg.middleware.dedupe.record_multiplicity:
      for k where count[k] > 1:
          annotate(p.records[seen[k]], "_count", count[k])

  return (p, [ DropEntry { what: "{n} duplicate records collapsed",
                           reason: DUPLICATE, recoverable: true } ])
```

**Correctness.** Deduplication is the rung most likely to be assumed lossless and not be.

* **Multiplicity is information.** Three identical rows may mean three events. Collapsing them to one
  silently changes the answer to "how many". `record_multiplicity` annotates the survivor with a
  count, which costs a handful of tokens and preserves the fact.
* **Identity must be declared or content-derived, never guessed.** Matching on a subset of fields that
  happens to look like a key merges records that differ elsewhere. When the tool declares no identity
  fields, the fallback is a hash of the whole normalized record — strict, and safe.
* **Canonicalization matters.** The hash normalizes key order and null representation, or two
  serializations of the same record fail to match and the rung achieves nothing.

---

## 7.1 `rankRecords`

Rung 4. The rung that does the most work on large result sets, and the one where losing the total
count silently changes what the agent can answer.

```text
function rankRecords(p, budget, ctx) -> (p, entries):

  if p.shape not in {UNIFORM_RECORDS, REPETITIVE_DELTA}: return (p, [])

  for r in p.records:
      r.rank <- max( lexicalOverlap(r, ctx.query),
                     predicateSignal(r, ctx.query),      # sla_breached, status=open, date in range
                     taskRecordPrior(r, ctx.task.workload) )
      if matchesExplicitPredicate(r, ctx.query): r.rank <- 1.0   # never rank out an exact match

  ordered <- sortDescending(p.records, by (rank, stableTiebreak(r)))   # deterministic
  n <- largest k such that estimateTokens(take(ordered, k)) <= budget.tokens
  n <- max(n, cfg.middleware.rank.min_records)
  if n >= len(p.records): return (p, [])

  dropped <- len(p.records) - n
  p.records <- take(ordered, n)

  # The total is not optional. Keeping 50 of 4,312 without saying 4,312 changes
  # the answer to every aggregate question the agent might ask of this result.
  if cfg.middleware.rank.preserve_total_count:
      annotate(p.envelope, "_total", len(ordered))
      annotate(p.envelope, "_returned", n)
      annotate(p.envelope, "_ranked_by", describeSignal(ctx))

  return (p, [ DropEntry {
      what: "records {n+1}..{len(ordered)} of {len(ordered)} (ranked by {signal})",
      reason: LOW_RANK, recoverable: true,
      retrieve_with: fetch_result(ref, range = n+1 .. len(ordered)) } ])
```

**Correctness.**

* **The total count survives.** `_total` and `_returned` cost about eight tokens and preserve every
  "how many" answer. Without them a ranked result is indistinguishable from a complete one, which is
  precisely the outcome INV-6 forbids.
* **The ranking signal is named**, both in the envelope and in the manifest. An agent that knows the
  records were ranked by SLA breach can reason about what is likely missing; one told only that
  records were dropped cannot.
* **Exact predicate matches are never ranked out.** If the query names a specific identifier, status
  or date range, matching records score 1.0 before any heuristic runs — the same short-circuit that
  protects fields in rung 2.
* **Ordering is deterministic.** A stable tiebreak means the same payload and query always produce
  the same 50 records, so a retry or a replay is comparable.

---

## 8. `truncate`

```text
function truncate(p, budget, ctx) -> (p, entries):

  entries <- []
  fields  <- longStringFields(p, min_bytes = cfg.middleware.truncation.min_field_bytes)
  fields  <- sortBy(fields, f => -bytesOf(p, f))

  for f in fields:
      if withinBudget(p, budget): break
      if f in protectedFields(p, ctx): continue
      if fieldClass(f) in cfg.context.disposition.never_compress: continue     # code, ids, contracts

      original_len <- len(p[f])
      keep         <- allowanceFor(f, budget)
      cut_at       <- boundary(p[f], keep, cfg.middleware.truncation.boundary)
      p[f]         <- p[f][0 .. cut_at] + marker(original_len - cut_at)

      entries.add(DropEntry {
          what: "field {f}: {original_len - cut_at} of {original_len} chars",
          reason: TRUNCATED, recoverable: true,
          retrieve_with: fetch_result(ref, field=f, from=cut_at) })

  return (p, entries)
```

**Correctness.**

* **`boundary` never cuts mid-token.** Semantic boundary means: end of sentence, else end of word,
  else a codepoint boundary. Cutting mid-multibyte produces mojibake; cutting mid-word produces a
  fragment the model will happily complete into something that was never in the data.
* **The marker carries the offset.** `…[+4,182 chars]` tells the agent both that the value is
  incomplete and how much is missing. A bare ellipsis tells it neither, and a silently truncated
  string is indistinguishable from a short one.
* **`never_compress` classes are exempt.** A truncated identifier, code block, URL or API contract is
  worse than an absent one — it looks usable and is not. Those fields skip truncation and, if they
  cannot fit, go to externalization instead.

---

## 9. `summarize`

The only rung that can produce content the source did not contain.

```text
function summarize(p, ctx) -> (p, entries):

  if cfg.compression.aggressiveness == "conservative": return (p, [])
  targets <- repetitiveRegions(p, min_records = cfg.middleware.summarize.min_records)
  entries <- []

  for t in targets:
      if alreadySummarized(t.content_hash): continue         # never twice (config invariant)
      if classOf(t) in cfg.context.disposition.never_compress: continue

      s <- model.generate(
             prompt   = summarizePrompt(t),
             model    = cfg.models.routing.by_operation_class.simple_summarization,
             reasoning= NONE,
             max_tokens = cfg.budgets.max_tokens_by_operation_class.summarization,
             structured = SUMMARY_SCHEMA)                     # facts, counts, ranges, outliers

      v <- validateSummary(s, t)                              # D4 §8
      if not v.ok or v.confidence < cfg.compression.summary_confidence_min:
          continue                                            # keep the original; externalize later

      s.trust <- t.trust                                      # INV-5: unchanged, not upgraded
      replace(p, t, s)
      recordSummarized(t.content_hash)
      entries.add(DropEntry {
          what: "{t.record_count} records summarized", reason: SUMMARIZED,
          recoverable: true, retrieve_with: fetch_result(ref, range=t.range) })

  return (p, entries)
```

**Correctness.** Four constraints, each closing a specific failure:

* **Structured output, not prose.** The summary schema asks for counts, ranges, distinct values and
  outliers. A free-text summary of 400 records is where fabricated specifics come from; a schema with
  numeric slots is checkable against the source.
* **Validation is mandatory and failure is silent-safe.** A summary that fails validation is
  discarded and the original kept. The rung producing nothing is a fine outcome; rung 8 will handle
  the size.
* **Never twice.** Summarizing a summary compounds loss and is untraceable. Content hashes of
  already-summarized regions are recorded for the session.
* **Trust is copied, not derived.** A summary of untrusted records is untrusted. This is the exact
  laundering path the specification warns about, and the assignment is a single line precisely so it
  cannot be forgotten in a refactor.

---

## 9.1 `projectAndPaginate`

Rung 7. Two operations because they share a constraint: both define a window over the data, and both
are useless if the window cannot be described well enough to ask for the next one.

```text
function projectAndPaginate(p, budget, ctx) -> (p, entries):

  entries <- []

  # --- Column projection ----------------------------------------------------
  keep <- columnsNamedIn(ctx.query)
        ∪ protectedFields(p, ctx)                    # §6: auth, tenancy, identity, join keys
        ∪ p.tool.identity_fields
        ∪ columnsReferencedBy(ctx.pending_actions)

  if keep ⊂ p.stats.distinct_keys:
      removed <- p.stats.distinct_keys \ keep
      project(p, keep)
      entries.add(DropEntry {
          what: "columns: " + join(removed), reason: BUDGET, recoverable: true,
          retrieve_with: fetch_result(ref, fields = removed) })

  if withinBudget(p, budget): return (p, entries)

  # --- Pagination -----------------------------------------------------------
  # The page boundary is keyed on the sort order, not on an offset. An offset
  # into a result whose order is not pinned returns overlapping or missing
  # records on the next call — the classic paginate-a-live-query bug.
  order <- p.envelope._sort ?? p.tool.default_sort ?? stableSortKey(p)
  require(order != null, "cannot paginate an unordered result")

  size <- largest k such that estimateTokens(take(p.records, k)) <= budget.tokens
  size <- max(size, cfg.middleware.pagination.min_page_size)
  page <- take(sortBy(p.records, order), size)
  cursor <- cursorAfter(last(page), order)           # value-based, not index-based

  p.records <- page
  annotate(p.envelope, "_page", { size, order, cursor, total: len(p.records) })

  entries.add(DropEntry {
      what: "page 1 of {ceil(total/size)}; {total − size} records beyond the cursor",
      reason: BUDGET, recoverable: true,
      retrieve_with: fetch_result(ref, after = cursor, order = order) })

  return (p, entries)
```

**Correctness.**

* **Projection keeps what a later step needs, not only what the query mentions.** Identity fields,
  join keys and anything a pending action references survive, because a projected result that cannot
  be joined back to its source is a dead end.
* **Cursors, not offsets.** `after = <value>` against a pinned sort order is stable when the
  underlying data changes between calls; `offset = 50` is not, and the failure — a record silently
  skipped or seen twice — is invisible in the result.
* **An unordered result is not paginated.** The function requires a sort key and refuses without one,
  falling through to externalization rather than inventing an order. A page of an unordered set is not
  reproducible and cannot be continued.
* **The page describes itself.** Size, order, cursor and total in the envelope mean the agent can
  decide whether to fetch more, rather than guessing whether it has seen everything.

---

## 10. `selectRepresentation`

```text
function selectRepresentation(p, model) -> Rendered:

  cands <- candidatesFor(p.shape):
      UNIFORM_RECORDS    -> [CSV, LINES, COMPACT_JSON]
      REPETITIVE_DELTA   -> [HEADER_DELTA, CSV, COMPACT_JSON]
      DEEP_NESTED        -> [COMPACT_JSON, YAML?]
      FLAT_LONG_STRINGS  -> [KV, COMPACT_JSON]              # YAML excluded by rule below
      SINGLE_LARGE_TEXT  -> [RAW_TEXT]
      OPAQUE             -> [RAW_TEXT]

  # YAML is a candidate only where its structure actually pays. On payloads
  # dominated by long strings its block scalars and indentation cost more than
  # the braces they replace.
  if YAML in cands and not (p.stats.key_repetition   >= cfg.middleware.representation.yaml_requires.min_key_repetition
                        and p.stats.mean_string_len  <= cfg.middleware.representation.yaml_requires.max_mean_string_len
                        and p.stats.max_depth        <= cfg.middleware.representation.yaml_requires.max_depth):
      cands.remove(YAML)

  cands <- take(cands, cfg.middleware.representation.max_candidates)

  best <- argmin(cands, c => estimateTokens(render(c, sample(p)), model.tokenizer).tokens
                             / sampleFraction(p))
  rendered <- render(best, p)
  rendered.size <- exactTokens(rendered, model.tokenizer)    # measure the winner exactly
  return rendered
```

**Correctness.** Representation is a lossless choice — the same data, differently serialized — with
two exceptions the function must respect: CSV cannot express nesting, and `HEADER_DELTA` cannot
express records with divergent keys. Both are excluded by the shape classification rather than
discovered at render time.

The YAML rule is the concrete form of design principle 7. It is stated as a gate rather than left to
the measurement because measuring three candidates on every payload costs more than the rule saves,
and the rule is right often enough that the measurement only has to arbitrate the remaining cases.

---

## 11. `estimateTokens`

```text
function estimateTokens(content, family) -> TokenCount:

  bytes <- byteLength(content)

  if bytes <= cfg.estimation.exact_below_bytes:
      a <- adapters[family] ?? null
      if a != null:
          return TokenCount { family, tokens: a.count(content), bytes, exact: true }

  # Large payload, or no adapter: sample and extrapolate.
  if a != null:
      windows <- sampleWindows(content, n = cfg.estimation.sample_windows,
                                        size = cfg.estimation.sample_window_bytes)
      rates   <- [ a.count(w) / byteLength(w) for w in windows ]
      if variance(rates) > cfg.estimation.extrapolation_variance_alarm:
          alarm(ESTIMATOR_SAMPLE_VARIANCE, family)          # heterogeneous content
      est <- mean(rates) * bytes
  else:
      est <- bytes / cfg.estimation.bytes_per_token_fallback

  est <- est * cfg.estimation.approximate_safety_multiplier
  return TokenCount { family, tokens: ceil(est), bytes, exact: false }
```

**Correctness.** Three properties this function must have, all of them consequences of INV-8:

* **`bytes` is always populated**, exact or not. It is the tokenizer-independent bound that survives a
  model handoff and prevents overflow when the target family's adapter is unavailable.
* **`exact: false` propagates.** An inexact count flows into the budget check, and C23 tracks the bias
  of approximate counts separately from exact ones. A system that cannot distinguish measured from
  estimated cannot tell a tuning problem from a measurement problem.
* **Estimation cost is itself a cost.** Tokenizing a 4 MB payload three times to choose between
  representations is a real expense charged against the overhead budget. Sampling with a variance
  check is the compromise: cheap enough to run per candidate, and the winner alone is measured
  exactly, so the final budget arithmetic is never based on an extrapolation.

---

## 12. `enforceBudget`

The one function that cannot fail. Whatever arrives, something within budget leaves.

```text
function enforceBudget(p, budget, manifest, trust, ref, ctx) -> ReducedPayload:

  rendered <- selectRepresentation(p, ctx.model)
  size     <- rendered.size                                  # exact by construction (§10)

  if size.tokens <= budget.tokens:
      return ReducedPayload { rendered, size, manifest, trust, archive_ref: ref }

  # --- Externalize: lossless, always available, never capped ---------------
  summary <- structuralSummary(p)          # shape, counts, key ranges, first N identifiers
  stub    <- render(SUMMARY_REF, {
                summary, ref,
                retrieve_with: fetch_result(ref, selector = "<field|range|query>") })
  manifest.add(DropEntry { what: "full payload ({size.tokens} tokens)",
                           reason: BUDGET, recoverable: true, ref })

  s <- exactTokens(stub, ctx.model.tokenizer)
  if s.tokens <= budget.tokens:
      return ReducedPayload { stub, s, manifest, trust, archive_ref: ref, representation: SUMMARY_REF }

  # --- Last resort: a reference and a manifest line -----------------------
  # Reached only when the budget is smaller than a structural summary, which
  # means the budget is misconfigured. Emit the minimum and alarm.
  alarm(BUDGET_BELOW_MINIMUM_STUB, ctx.tool, budget.tokens)
  minimal <- render(REF_ONLY, { ref, tokens_withheld: size.tokens })
  return ReducedPayload { minimal, exactTokens(minimal), manifest, trust, archive_ref: ref }
```

**Correctness.** The three-stage descent guarantees termination: a chosen representation, else a
structural summary plus reference, else a bare reference. The final rung is ~20 tokens and always
fits, so there is no input for which this function cannot return. The alarm on the last branch matters
— reaching it means a per-tool budget was set below the size of a reference, which is a configuration
error that would otherwise present as mysteriously uninformative tool results.

**Demotion:** if `selectRepresentation` throws, the raw normalized content is rendered as
`COMPACT_JSON` and the descent continues from there.

---

## 13. Effect on correctness — the summary table

Required by the deliverable, and the table to reach for when deciding whether a rung is safe to
enable in a given profile.

| Operation | What it can cost you | What it must preserve | Detectable by the agent | Safe when |
|---|---|---|---|---|
| `normalizePayload` | Result/metadata distinction if flattening over-reaches | Envelope, nesting that carries meaning | n/a — no data removed | Flattening limited to single-child containers |
| `removeNulls` | The checked-and-empty vs never-evaluated distinction | Protected fields, explicit nulls by default | Manifest count | `drop_explicit_null: false` |
| `removeIrrelevantFields` | A field needed later, or by a security check | Auth, tenancy, identity, join keys, query-named fields | Manifest names every field | Protected set short-circuits scoring |
| `deduplicate` | Multiplicity — "how many" answers | Distinct records, count annotations | Manifest count | Identity declared or full-content hash |
| `rankRecords` | Records below the cut | Top-N ordering rationale, total count | Manifest states N of M | Ranking signal relates to the query |
| `truncate` | Tails of long values | Codepoint integrity, offsets, never-compress classes | Marker carries the missing length | Semantic boundary, `never_compress` exempt |
| `summarize` | **Accuracy** — it can assert what the source did not | Numbers, identifiers, trust label | Manifest states the range summarized | Structured output plus validation plus confidence floor |
| `projectAndPaginate` | Columns and pages outside the window | Page boundaries, total count | Manifest states the range | Page identity is stable across calls |
| `externalize` | Nothing | Everything, by reference | Reference plus retrieval call | Always |
| `selectRepresentation` | Nesting (CSV), divergent keys (delta) | All values | n/a | Shape classification excludes unsuitable forms |
| `estimateTokens` | Nothing; an underestimate causes overflow downstream | The byte bound, the `exact` flag | n/a | Safety multiplier on inexact counts |
| `enforceBudget` | Whatever the descent had to give up | A working reference, always | Manifest plus stub | Always — it is the terminating case |

---

## 14. Worked example

A ticket search returning 4,312 records against a 4,000-token `database_query` budget, on the
`database-analytics` profile (`aggressiveness: balanced`, `max_ladder_rung: 6`).

```text
raw                                   1,284 KB   ~338,000 tok   archived → result:9f3a1c
normalize        UNIFORM_RECORDS, 4,312 records, 34 keys, mean string 61 B
rung 1  nulls           −  11,400 tok   (18,912 empty leaves)
rung 2  irrelevant      − 214,000 tok   (fields: body_html, watchers[], changelog[], _links)
                                         protected and kept: id, key, tenant_id, assignee_id, acl
rung 3  dedupe          −       0 tok   (no duplicates; rung skipped, no re-estimate)
rung 4  rank top 50     −  110,900 tok  (relevance: sla_breached, then breach_minutes desc)
                                        → 1,700 tok — under budget, ladder stops
                                        rungs 5 and 6 never run
representation   CSV 1,700 tok · LINES 1,930 · COMPACT_JSON 2,410   → CSV
final                                    1,712 tok exact
```

Rendered into context:

```text
[reduced 4,312 → 50 records (low_rank); 4 fields removed (irrelevant);
 full set at result:9f3a1c;
 retrieve_with fetch_result("9f3a1c", range=51..4312 | fields=[...])]
```

Two things this example is chosen to show. **The ladder stopped at rung 4** — no truncation, no
summarization, so nothing in the result can be wrong, only absent. And **rung 2 did most of the
work**: 63% of the payload was four fields nobody asked for. On real API responses that is the usual
shape, which is why field relevance is the rung worth investing accuracy in and summarization is the
rung worth avoiding.

---

## 15. Test hooks (for D11)

| Test | Asserts |
|---|---|
| Archive-before-reduce | Every `DropEntry.ref` resolves to content containing the dropped material |
| Protected-field fuzz | No generated payload loses an auth, tenancy, identity or join field at any aggressiveness |
| Multiplicity | Dedupe over N identical records preserves N in an annotation |
| Truncation integrity | No output contains a broken codepoint; every marker's offset resolves |
| Summary fabrication | Every numeric and identifier in a summary appears in the source |
| Trust propagation | Reduced trust label equals raw trust label on 100% of paths |
| Representation regression | For each shape, the chosen form is within 5% of the best of all forms |
| Estimator agreement | Sampled estimate within 15% of exact count across the corpus |
| Termination | `enforceBudget` returns within budget for budgets from 10 to 10⁶ tokens |
| Rung regression | No rung increases token count without being reverted and alarmed |
