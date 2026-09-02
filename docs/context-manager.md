# Deliverable 4 — Context Manager

**Stage 3 of 5.** Production pseudocode for budgeting, scoring, selection, compaction, working memory,
archival, retrieval, summary validation and checkpointing — with cache-invalidation cost included in
`compressContext()`.

Companion documents: [`architecture.md`](architecture.md) (C3, C4, C16, C19) ·
[`payload-middleware.md`](payload-middleware.md) (D3) · [`decision-engine.md`](decision-engine.md)
(§17 S10–S11) · [`configuration.md`](configuration.md) (D9).

---

## 1. Position and contract

The context manager spans four Stage 1 components: the Budget Manager (C3), the Context Scorer and
Selector (C4), the Raw Archive (C19) and the checkpoint half of the Failure Controller (C16). They
are documented together because they form one loop:

```text
calculateContextBudget → scoreContextItem → selectContext → compressContext
        ↑                                                          │
        └──────────── archiveContext ⇄ retrieveContext ←───────────┘
```

Everything here runs in the **control plane** except `archiveContext` and `retrieveContext`. That
means scoring and selection are pure functions over a state snapshot: no I/O, no model calls, fully
unit-testable, and cheap enough to run inside the overhead budget.

Two invariants shape every function below. **INV-1**: these functions rebuild a projection; they never
mutate canonical state. **INV-7**: any decision that edits a cached prefix is priced against
invalidation before it happens, and `compressContext` is where that pricing lives.

---

## 2. `calculateContextBudget`

```text
function calculateContextBudget(model, task, state) -> BudgetSpec:

  window <- model.capabilities.window
  f      <- cfg.budgets

  # Reserved output is the only allocation sized from the task rather than a
  # fixed fraction: a classification turn does not need 12% of a 200k window.
  op        <- operationClassOf(task)
  reserved_out <- min( f.reserved_output_fraction * window,
                       ceil(cfg.budgets.max_tokens_by_operation_class[op]
                            * cfg.budgets.output_headroom) )
  reserved_out <- max(reserved_out, cfg.budgets.min_reserved_output)

  reserved_sys <- f.reserved_system_skill_fraction * window
  margin       <- f.safety_margin_fraction * window

  available <- window - reserved_out - reserved_sys - margin
  if available <= 0: fail(BUDGET_UNSATISFIABLE, window, model)   # config error, alarmed at load

  scale <- available / ((f.working_fraction + f.tool_results_fraction + f.memory_fraction) * window)

  b <- BudgetSpec {
      family:  model.tokenizer,                                   # INV-8: the stamp
      window,
      reserved_output: reserved_out,
      reserved_system_skill: reserved_sys,
      safety_margin: margin,
      working:      f.working_fraction      * window * scale,
      tool_results: f.tool_results_fraction * window * scale,
      memory:       f.memory_fraction       * window * scale,
      per_tool:     f.per_tool,
      per_modality: f.per_modality,
      thresholds:   cfg.thresholds.bands
  }

  # Task-driven adjustment. Complexity moves the split between working context
  # and tool results; it never touches the reservations or the margin.
  if task.data_heavy:   shift(b, from = working, to = tool_results, amount = 0.10 * window)
  if task.complexity == COMPLEX: shift(b, from = tool_results, to = working, amount = 0.05 * window)

  assertCoherent(b, model)                                        # family match, sum ≤ window
  return b
```

**`scale` is why fractions can be tuned independently.** The three working fractions are expressed
against the whole window in configuration because that is how people reason about them, but they must
actually divide what is left after reservations. Rescaling rather than clamping means a profile that
sets `tool_results_fraction: 0.34` gets 34% *of the available space*, not 34% of the window followed
by a silent truncation of the last allocation in the list.

**Why reserved output is task-sized.** A fixed 12% of a 200k window reserves 24,000 tokens for a
classification answer that will emit sixteen. That is 24,000 tokens of working context given away for
nothing, on every such turn.

```text
function band(b, utilization, previous_band) -> Band:
  # Hysteresis: enter on the way up, exit on the way down. Without the
  # previous band there is no hysteresis, only a threshold.
  for band in descending(b.thresholds):
      if utilization >= band.enter: return band.name
  if previous_band != NORMAL and utilization > exitOf(b, previous_band):
      return previous_band                                        # still above exit: stay put
  return NORMAL
```

**Config:** `budgets.*`, `thresholds.bands`.
**Demotion:** a single conservative allocation — `available / 3` each — with the margin doubled.
**Complexity:** O(1).

---

## 3. `scoreContextItem`

```text
function scoreContextItem(item, ctx) -> ContextScore:

  # --- the five factors ----------------------------------------------------
  relevance <- max( lexicalOverlap(item, ctx.query),
                    embeddingSimilarity(item, ctx.query)     if ctx.embeddings_available,
                    cfg.context_manager.scoring.relevance_floor )

  importance <- cfg.context_manager.scoring.importance_by_kind[item.kind]
              * (item.provenance.trust == SYSTEM ? 1.25 : 1.0)
              * (item.kind == STATE or item.class in NEVER_COMPRESS ? 1.5 : 1.0)

  recency <- 0.5 ^ (ctx.turn_index - item.turn_index) / cfg.context.recency_halflife_turns)

  dependency <- 1 + log2(1 + inDegree(ctx.dep_graph, item.id))    # how much depends on it

  future_value <- max over pending in ctx.pending_actions of
                      P(pending needs item)                       # crude prior by kind and reference
                  ?? cfg.context_manager.scoring.future_value_prior

  w <- cfg.context.scoring_weights
  score <- relevance^w.relevance * importance^w.importance
         * recency^w.recency * dependency^w.dependency * future_value^w.future_value

  # --- the economics, which is what the disposition actually uses ----------
  horizon <- ctx.expected_remaining_turns ?? cfg.context.expected_remaining_turns_default

  retention_cost <- item.size.tokens
                  * inputPrice(ctx.model)
                  * cachedDiscountFactor(item, ctx.cache)         # cached items are cheap to keep
                  * horizon

  reconstruction_cost <- estimatedRetrievalCost(item)             # archive read + re-render
                       + P(needed again) * roundTripCost(ctx)     # the turn it would cost to notice

  return ContextScore { relevance, importance, recency, dependency, future_value,
                        score, retention_cost, reconstruction_cost }
```

**The weights are exponents, not multipliers.** `scoring_weights` all default to 1.0, giving a plain
product. Raising a weight above 1 sharpens that factor's influence without changing the ordering when
the factor is uniform — which a linear weight would. It also keeps the score bounded in [0,1] for
factors that are, which makes the numbers comparable across items.

**`cachedDiscountFactor` is the term that makes retention position-aware.** An item sitting in a warm
prefix costs 10% of list price to keep. An identical item in the uncached tail costs full price. The
same item therefore has a genuinely different retention cost depending on where it sits, and this is
the mechanism by which "reduce the tail, preserve the head" falls out of the arithmetic instead of
being a special case.

**Scores order; economics decides.** `score` is deliberately crude and is used only to rank
candidates. Nothing compares it to an absolute threshold, because a product of five heuristics has no
meaningful absolute scale. The five-way disposition below is decided by comparing costs.

**Config:** `context.scoring_weights`, `context.recency_halflife_turns`,
`context.expected_remaining_turns_default`, `context_manager.scoring.*`.
**Demotion:** returns `score = recency` alone, with both costs at wide intervals — which INV-9 turns
into "retain".
**Complexity:** O(1) per item given a precomputed dependency graph.

---

## 4. `selectContext`

```text
function selectContext(items, budget, ctx) -> SelectionResult:

  for it in items: it.score <- scoreContextItem(it, ctx)

  # --- 1. Disposition per item, by economics not by rank -------------------
  for it in items:
      it.disposition <- decide(it, budget, ctx)

  # --- 2. Dependency closure: promote what retained items depend on --------
  if cfg.context.dependency_closure:
      kept <- [ it for it in items if it.disposition == RETAIN ]
      for id in transitiveDeps(ctx.dep_graph, kept):
          if items[id].disposition in {DISCARD, RETRIEVE_LATER}:
              items[id].disposition <- RETAIN
              trace(PROMOTED_BY_DEPENDENCY, id)

  # --- 3. Fit ---------------------------------------------------------------
  selected <- [ it for it in items if it.disposition in {RETAIN, COMPRESS} ]
  total    <- sum(sizeAfterDisposition(it) for it in selected)

  while total > budget.working:
      victim <- weakestReducible(selected)         # lowest score, tail-first among ties
      if victim == null: break                     # everything left is protected
      demote(victim)                               # RETAIN → COMPRESS → EXTERNALIZE
      total <- recompute(selected)

  return SelectionResult { items, total, band: band(budget, total / budget.window, ctx.previous_band) }


function decide(it, budget, ctx) -> Disposition:

  s <- it.score

  # Protected classes never leave verbatim.
  if it.class in cfg.context.disposition.never_compress and it.canonical_ref == null:
      return RETAIN

  # Discard requires BOTH conditions, never just a low score.
  if s.future_value < cfg.context_manager.scoring.low_future_value
     and s.reconstruction_cost.hi < s.retention_cost.lo:
      return DISCARD

  # Large and uncertain: store it, keep a reference.
  if it.size.tokens > cfg.context.disposition.externalize_when_tokens_above
     and s.future_value < cfg.context_manager.scoring.high_future_value:
      return EXTERNALIZE

  # Cheaper to fetch again than to carry.
  if s.reconstruction_cost.hi < s.retention_cost.lo:
      return RETRIEVE_LATER

  # Frequently needed and losslessly representable smaller.
  if s.relevance > cfg.context_manager.scoring.high_relevance and losslesslyCompressible(it):
      return COMPRESS

  return RETAIN                                    # the default, and the tie-break (INV-9)
```

**Three properties worth stating.**

*`RETAIN` is the default and the tie-break.* Every other branch requires a positive reason. An item
that falls through every test is kept, which is the conservative direction INV-9 demands.

*`DISCARD` requires both low future value and cheap reconstruction.* Either alone is insufficient —
this is the specification's rule made mechanical. An item that is cheap to reconstruct but likely
needed becomes `RETRIEVE_LATER`; an item unlikely to be needed but expensive to rebuild is `RETAIN`
or `EXTERNALIZE`.

*Dependency closure runs after disposition, before fitting.* An item with a near-zero score that a
retained decision was built on is promoted. This is what stops the deletion of the apparently
irrelevant message that a later conclusion rests on — the case that produces an agent confidently
contradicting itself six turns later with no visible cause.

**Complexity:** O(n log n) for the ordering, plus O(V+E) for the closure.

---

## 5. `compressContext`

The function the deliverable singles out, because it is where cache-invalidation cost enters.

```text
function compressContext(selection, cacheState, ctx) -> CompactionDecision:

  band <- selection.band

  # --- Emergency: economics suspended ---------------------------------------
  if band == EMERGENCY and cfg.thresholds.emergency_suspends_economics:
      targets <- emergencyTargets(selection)       # largest reducible, tail first, then head
      ledger.record(EMERGENCY_COMPACTION, tokensOf(targets))
      return CompactionDecision { action: COMPACT_NOW, targets,
                                  net_benefit: unpriced, reason: "emergency band",
                                  at_boundary: EMERGENCY }

  if band in {NORMAL, PRUNE}:
      return CompactionDecision { action: DEFER, targets: [], reason: "below compaction band" }

  # --- Tail first: same relief, zero cache cost -----------------------------
  if cfg.compression.compaction.tail_first:
      tail   <- itemsAfter(lastBreakpoint(cacheState), selection)
      relief <- sortBy(tail, it => -it.size.tokens)
      freed  <- 0 ; chosen <- []
      needed <- tokensToReach(selection, exitOf(cfg.thresholds, band))

      for it in relief:
          if it.class in cfg.context.disposition.never_compress and it.canonical_ref == null:
              continue
          chosen.add(it) ; freed += it.size.tokens
          if freed >= needed: break

      if freed >= needed:
          return CompactionDecision {
              action: DEFER,                       # no prefix edit at all
              externalize: chosen,                 # applied by selectContext's caller
              net_benefit: freed * inputPrice(ctx.model) * horizon(ctx)
                           - externalizationCost(chosen),
              reason: "tail externalization sufficient; head untouched",
              at_boundary: TASK_TRANSITION }       # head compaction stays queued

  # --- Head compaction: price it --------------------------------------------
  targets <- headCompactionTargets(selection, cacheState)
  targets <- [ t for t in targets if not alreadyCompacted(t.content_hash) ]   # never twice
  if targets.isEmpty():
      return CompactionDecision { action: DEFER, reason: "all candidates already compacted" }

  horizon <- ctx.expected_remaining_turns ?? cfg.context.expected_remaining_turns_default
  edit_at <- earliestPosition(targets)

  tokens_removed   <- sum(t.size.tokens - estimatedCompactedSize(t) for t in targets)
  cached_after     <- cachedTokensAfter(cacheState, edit_at)
  discount         <- providerCapabilities(ctx.model).cached_input_discount
  price            <- inputPrice(ctx.model)

  gain <- tokens_removed * price * horizon
  loss <- cached_after   * price * discount * horizon
  cost <- summarizationCost(targets, ctx)

  benefit <- Estimate { lo: gain.lo - loss.hi - cost.hi,
                        mid: gain.mid - loss.mid - cost.mid,
                        hi: gain.hi - loss.lo - cost.lo }

  ledger.record(COMPACTION_EVALUATED, { tokens_removed, cached_after, benefit })

  if cfg.compression.compaction.require_positive_benefit and benefit.lo <= 0:
      queueForBoundary(targets, cfg.compression.compaction.batch_at_boundaries)
      return CompactionDecision { action: DEFER, targets, net_benefit: benefit,
                                  reason: "compaction_benefit not positive",
                                  at_boundary: TASK_TRANSITION }

  return CompactionDecision { action: COMPACT_NOW, targets, net_benefit: benefit,
                              reason: "positive net benefit",
                              at_boundary: currentBoundary(ctx) }
```

**`benefit.lo <= 0`, not `benefit.mid <= 0`.** Compaction is judged on its pessimistic bound. An
optimization whose central estimate is positive but whose interval crosses zero is not taken, because
the downside — invalidating a warm prefix for nothing — is paid every remaining turn while the upside
is bounded by the tokens removed. INV-9 in a single comparison operator.

**Order of the three branches is the whole design.** Emergency first, because a turn that does not fit
is worse than any cost. Tail externalization second, because it delivers the same relief at zero cache
cost and on most turns it is sufficient. Head compaction last, priced, and usually deferred. The
common case is that this function returns `DEFER` while still freeing the tokens that were needed —
which is not a contradiction: `DEFER` refers to the *prefix edit*, and the externalization it returns
alongside is what actually creates the headroom.

**Batching is why deferral is not just postponement.** Deferred targets accumulate and are applied in
one edit at the next boundary. Two separate compactions of the same prefix pay `loss` twice for one
`gain`; batching pays it once. `max_pending_batched` bounds the queue so a session that never reaches
a boundary does not accumulate unboundedly.

**Config:** `thresholds.*`, `compression.compaction.*`, `context.expected_remaining_turns_default`,
`context.disposition.never_compress`, provider `cached_input_discount`.
**Demotion:** returns `DEFER` with empty targets — the context grows, nothing breaks.

---

## 6. `createWorkingMemory`

```text
function createWorkingMemory(task, state, selection) -> WorkingState:

  return WorkingState {
      items:   selection.retained ++ selection.compressed ++ referenceStubs(selection.externalized),
      pending: state.canonical.pending_actions,
      branch_results: {},
      checkpoints: [],
      manifest: DropManifest.merge(selection.manifests)     # accumulates across the turn
  }
```

Deliberately thin. Working memory is a **view**, assembled per turn and discarded at phase 11 after
the reducer commits. It holds no authority: every item in it is either a projection of something in
C17/C18 or a reference into C19. This is INV-1 expressed as a data structure — there is nothing in
working memory that would be lost if the process died, which is what makes checkpointing cheap (§9)
and rollback total (§10).

The externalized items appear as **reference stubs**, not as absences. An item that was moved out of
context leaves behind its identity, its size and its retrieval call. The agent can see that something
exists and is not currently loaded, which is the difference between incomplete context and context
that appears complete.

---

## 7. `archiveContext` and `retrieveContext`

```text
function archiveContext(items) -> ArchiveRef[]:

  refs <- []
  for it in items:
      content, findings <- securityEnvelope.sanitize(it.render())   # BEFORE the write (V3)
      if findings.any(): ledger.record(PII_REDACTED, it.id, findings.classes)

      ref <- archive.put(content, provenance = it.provenance ++ {
                 scope: it.provenance.scope,           # tenant-scoped storage and encryption
                 retention: cfg.security.retention.archive_days })
      it.canonical_ref <- ref                          # the item now knows where its truth lives
      refs.add(ref)
  return refs
```

Sanitization runs before the write, not before the read. Externalization creates a durable copy of
everything the agent has seen; a store that holds unredacted secrets and redacts on retrieval has
already leaked them to backups, replicas and the index.

```text
function retrieveContext(ref, selector, budget, ctx) -> ContextItem | Gap:

  cached <- cache.retrieval.get(key(ref, selector, ctx.scope, ctx.auth))
  if cached: return cached

  if not securityEnvelope.authorize(READ, ctx.auth, ref.scope):
      return Gap { reason: NOT_AUTHORIZED, ref }        # a gap, never a silent empty result

  raw <- archive.get(ref, selector)                     # selective: one field, one range, one query
  if raw == null: return Gap { reason: EXPIRED, ref }   # retention TTL lapsed

  item <- materialize(raw, provenance = archive.provenanceOf(ref))
  item.provenance.trust <- archive.provenanceOf(ref).trust    # label restored, never upgraded

  if item.size.tokens > budget.tokens:
      item <- payloadMiddleware.reduce(item, budget, ctx.task)    # D3, recursively bounded

  cache.retrieval.put(..., ttl = cfg.cache.retrieval.ttl_seconds)
  return item
```

**The selector is the entire point.** An archive that can only return whole objects turns
externalization from a saving into an expensive detour: store 20 KB, then pay 20 KB to read back one
field. `archive.get(ref, selector)` supports field paths, index ranges and simple predicates, and the
manifest entries written by D3 are phrased as exactly those selectors so the agent can copy them.

**A failed retrieval returns a `Gap`, never nothing.** `NOT_AUTHORIZED` and `EXPIRED` are different
facts and both are actionable; an empty result conflates them with "no such data" and invites the
agent to conclude the data does not exist.

---

## 8. `validateSummary`

The gate on the only lossy operation that can fabricate (D3 §9).

```text
function validateSummary(summary, original) -> ValidationResult:

  failures <- []

  # --- 1. Critical-field preservation --------------------------------------
  for cls in cfg.context_manager.summary_validation.critical_classes:
      src <- extract(original, cls)                    # ids, numbers, dates, names, decisions
      out <- extract(summary,  cls)
      missing <- src \ out
      if cls in cfg.context.disposition.never_compress and missing.any():
          failures.add(MISSING_CRITICAL, cls, missing)

  # --- 2. No invented specifics --------------------------------------------
  invented <- extract(summary, ENTITIES) \ extract(original, ENTITIES)
  if count(invented) > cfg.context_manager.summary_validation.max_new_entities:
      failures.add(FABRICATED_ENTITIES, invented)

  # --- 3. Numbers and identifiers match exactly ----------------------------
  if cfg.context_manager.summary_validation.numeric_match_required:
      for n in extract(summary, NUMBERS):
          if n not in extract(original, NUMBERS): failures.add(NUMERIC_MISMATCH, n)
  if cfg.context_manager.summary_validation.identifier_match_required:
      for id in extract(summary, IDENTIFIERS):
          if id not in extract(original, IDENTIFIERS): failures.add(IDENTIFIER_MISMATCH, id)

  # --- 4. No contradiction with retained context ---------------------------
  for claim in claimsOf(summary):
      if contradicts(claim, ctx.retained_facts): failures.add(CONTRADICTION, claim)

  # --- 5. Trust unchanged ---------------------------------------------------
  if summary.trust.level != original.trust.level: failures.add(TRUST_ELEVATED)   # INV-5

  confidence <- 1.0 - (weightedSeverity(failures) / maxSeverity)
  return ValidationResult { ok: failures.isEmpty(), failures, confidence }
```

**Every check is extractive, not judgemental.** No second model call is made to grade the summary —
that would cost more than the summarization it validates and would introduce a second thing that can
be wrong. Set difference over extracted numbers, identifiers and entities is deterministic, cheap and
catches the failure mode that actually occurs: a plausible summary containing a number that was never
in the data.

**Failure is cheap.** A rejected summary means the original stays and externalization handles the
size. There is no repair path, no retry with a better prompt: the value of this gate comes from being
unambiguous.

---

## 9. `checkpointContext`

```text
function checkpointContext(working) -> Checkpoint:

  if len(working.checkpoints) >= cfg.context_manager.checkpoint.max_per_turn:
      collapse(working.checkpoints)                    # keep the first; the rest are unreachable

  ck <- Checkpoint {
      id: nextId(),
      items:    persistentSnapshot(working.items),     # structural sharing: O(1), not a deep copy
      pending:  persistentSnapshot(working.pending),
      manifest: working.manifest.mark(),               # a position, not a copy
      branch_results: persistentSnapshot(working.branch_results),
      taken_at: now()
  }
  working.checkpoints.push(ck)
  return ck
```

**Structural sharing is what makes checkpointing affordable enough to be unconditional.** Working
state is an immutable persistent structure, so a checkpoint is a root pointer. Deep-copying context
before every tool call would cost more than the tool calls, and a checkpoint that is expensive is a
checkpoint that gets skipped exactly where it was needed.

**`max_per_turn` with collapse** bounds a pathological turn — a twelve-iteration tool loop each taking
a checkpoint. Only the first is reachable as a rollback target once the loop is recognized as a loop,
so the intermediates are collapsed rather than retained.

## 10. `rollbackContext`

```text
function rollbackContext(ck, failure, working) -> WorkingState:

  restored <- WorkingState {
      items:    ck.items,
      pending:  ck.pending,
      branch_results: ck.branch_results,
      manifest: working.manifest.truncateTo(ck.manifest),
      checkpoints: working.checkpoints.upTo(ck)
  }

  # The archive keeps everything; context keeps one line.
  detail_ref <- archive.put(failure.raw, provenance = failure.provenance)
  restored.items.append(ContextItem {
      kind: MESSAGE, stability: TURN,
      render: () => normalizedError(failure, detail_ref),      # exactly one line
      provenance: { source: "failure_controller", trust: SYSTEM }
  })

  assert noResidue(restored, failure.branch_id)                # nothing from the failed branch
  ledger.record(ROLLBACK, failure.branch_id, tokensDiscarded(working, restored))
  return restored
```

**`noResidue` is asserted, not assumed.** The rollback is only worth taking if it is total: a partial
result from a failed branch left in context is worse than no attempt at all, because it reads as data
rather than as a failure. The assertion is cheap — every item carries the branch that produced it —
and it is the difference between a rollback mechanism and a rollback intention.

**The manifest truncates rather than resets.** Reductions performed *before* the checkpoint are still
in effect and must remain declared. Resetting the manifest would leave context that has been reduced
but no longer says so, which is the one outcome INV-6 forbids.

---

## 11. Failure and demotion

| Function | Failure | Demotion | Cost |
|---|---|---|---|
| `calculateContextBudget` | Window unknown, fractions invalid | Even thirds, doubled margin | Under-utilized window |
| `scoreContextItem` | Embeddings or graph unavailable | `score = recency`, wide cost intervals | Everything retained |
| `selectContext` | Scoring failed | Retain by recency until full, externalize the rest | Larger context, no loss |
| `compressContext` | Cache state or price unavailable | `DEFER`, empty targets | Context grows |
| `createWorkingMemory` | — | Cannot fail; it is a projection | — |
| `archiveContext` | Store unavailable | Session-scoped in-memory archive | References die at session end |
| `retrieveContext` | Store unavailable, unauthorized, expired | Returns a typed `Gap` | Agent sees the gap and can say so |
| `validateSummary` | Extractors unavailable | `ok: false` — reject the summary | Summarization silently disabled |
| `checkpointContext` | Snapshot failed | No checkpoint; the caller runs without rollback | Failed steps may leave residue |
| `rollbackContext` | Checkpoint missing | Drop all turn-local items and re-project from canonical state | Loses good work, keeps correctness |

Two rows are worth reading twice. `validateSummary` demotes to **reject**, so a broken validator
disables summarization rather than allowing unvalidated summaries — fail-closed on the one operation
that can fabricate. And `rollbackContext` without a checkpoint falls back to full re-projection from
canonical state, which is expensive and always correct, because INV-1 guarantees canonical state was
never touched by the failed work.

---

## 12. Worked example — turn 40, continued

The Stage 1 example, now with the functions that produce it.

```text
calculateContextBudget   window 200,000 · reserved_out 4,200 (final_answer, task-sized)
                         reserved_sys 12,000 · margin 6,000 · available 177,800
                         working 100,700 · tool_results 49,200 · memory 26,900
utilization 0.78         band(0.78, prev=COMPACT) → COMPACT      (enter 0.70, exit 0.60)

scoreContextItem         62,000 tok in the cached head: retention_cost × 0.10 (warm)
                         21,000 tok in the uncached tail: retention_cost × 1.00

selectContext            two tool results in the tail, 21,000 tok, future_value 0.2
                         → EXTERNALIZE (size 12,400 and 8,600, both > 1,500 threshold)

compressContext          band = COMPACT, tail_first = true
                         needed to reach exit(0.60) = 20,600 tok
                         tail relief available      = 21,000 tok  ≥ needed
                         → DEFER, externalize [r1, r2]
                            net_benefit = 21,000 × $3/M × 6 − $0.001 = +$0.377
                         head compaction queued for TASK_TRANSITION
                         (had it been priced now: gain $0.324 − loss $0.713 − $0.020 = −$0.409)

archiveContext           sanitize → put → result:9f3a1c, result:9f3a1d
                         stubs left in context: 2 × ~30 tok including retrieval calls
utilization 0.62         above exit 0.60 — band stays COMPACT, no oscillation
```

The last line is the hysteresis doing its job. Utilization landed at 0.62, above the exit threshold,
so the band does not flip to `PRUNE` and back on the next tool result. With a single 0.70 threshold
this session would have compacted, dropped to 0.62, compacted again on the next result, and
invalidated the prefix twice for one benefit.

---

## 13. Test hooks (for D11)

| Test | Asserts |
|---|---|
| Budget coherence | For 10⁴ random configs and windows: allocations sum within the window, family stamped |
| Tokenizer handoff | Recomputed budgets never exceed the target window; byte bound respected |
| Hysteresis | A sawtooth utilization trace produces no band oscillation within the enter/exit gap |
| Disposition determinism | Same snapshot → same dispositions, across 10³ runs |
| Dependency closure | No retained conclusion has an unretained, unreferenced dependency |
| Compaction sign | For warm-prefix traces, total cost never exceeds the no-compaction baseline |
| Never-twice | No content hash is summarized more than once per session |
| Summary validation | Every fabricated number and identifier in a mutation corpus is rejected |
| Archive round-trip | `retrieveContext(ref, selector)` returns exactly the material named in the manifest |
| Sanitization ordering | No archive write contains material the sanitizer would have redacted |
| Rollback totality | After rollback, zero items carry the failed branch id; manifest truncated, not reset |
| Checkpoint cost | Checkpoint time is O(1) in working-set size across a 10⁵-item sweep |
