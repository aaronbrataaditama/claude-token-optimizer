# Deliverable 7 — Sub-Agent Manager

**Stage 4 of 5.** Production pseudocode for spawning, bounding, executing and reducing sub-agent work
(C15), with the deterministic reducer (C13) as the sole write path from parallel branches to shared
state.

Companion documents: [`architecture.md`](architecture.md) (C13, C15) ·
[`model-router.md`](model-router.md) (D6 §9, the delegation gate) ·
[`context-manager.md`](context-manager.md) (D4) · [`configuration.md`](configuration.md) (D9) ·
[`../skill/references/delegation.md`](../skill/references/delegation.md) (the runtime instruction).

---

## 1. Contract

`shouldDelegate` (D6 §9) decides *whether*. This component decides *how*, and enforces the four
prohibitions that make delegation economically real rather than theatre:

| Prohibition | Enforced by |
|---|---|
| A sub-agent never receives the primary's context | `inputs` is an item-ID allowlist; there is no API accepting a context |
| A sub-agent never loads the optimization skill | `forbid_skill_injection`, asserted at spawn |
| A sub-agent's result cannot exceed the trust of its inputs | `trust_ceiling` computed, not declared |
| Delegation cannot recurse without bound | Depth counter, attenuated budgets, session semaphore |

Each is structural. None is a runtime check on a value the caller could have set differently — the
first is enforced by the shape of `SubAgentSpec`, and the rest by assertions that run before any work
is dispatched.

---

## 2. `spawnSubAgent`

```text
function spawnSubAgent(spec, ctx) -> SubAgentHandle | Refusal:

  # --- Structural gates. All cheap, all before any cost is incurred. -------
  if ctx.depth + 1 > cfg.delegation.max_depth:
      return Refusal { RECURSION_DEPTH, ctx.depth }
  if cfg.delegation.max_depth > cfg.delegation.sane_depth_limit
     and not cfg.delegation.explicit_deep_delegation:
      fail(CONFIG, "V6")                                   # caught at load; belt and braces
  if ctx.session.subagents >= cfg.delegation.max_subagents_per_session:
      return Refusal { SESSION_CAP }
  if not semaphore.tryAcquire(cfg.delegation.max_subagents_per_turn):
      return Refusal { CONCURRENCY_CAP }

  # --- Budget, attenuated by depth ------------------------------------------
  # Each level gets a fraction of its parent's remaining allowance, so a tree
  # is bounded by a geometric series rather than by depth × breadth.
  allowance <- ctx.spawn_allowance_usd * (cfg.delegation.depth_attenuation ^ ctx.depth)
  allowance <- min(allowance, remaining(cfg.delegation.spawn_budget_usd_per_turn),
                              remaining(cfg.delegation.spawn_budget_usd_per_session))
  if allowance <= cfg.delegation.min_expected_saving_usd:
      semaphore.release(); return Refusal { SPAWN_BUDGET_EXHAUSTED }

  # --- Contract assertions (V7). A malformed spec never reaches execution. --
  assert spec.inputs is ItemIdAllowlist and spec.inputs.isExplicit()
  assert spec.result_schema != null and cfg.delegation.require_structured_output
  assert cfg.delegation.forbid_skill_injection and not spec.loads_optimizer_skill
  assert spec.tools ⊆ toolsNeededBy(spec.subtask)          # not the primary's tool set
  spec.trust_ceiling <- min(trustOf(i) for i in spec.inputs)          # computed (INV-5)
  spec.model.reasoning <- NONE if cfg.models.reasoning.forbid_for_subagents else spec.model.reasoning

  # --- Minimal context, bounded --------------------------------------------
  projection <- prepareMinimalContext(spec, ctx)           # §3
  if projection.total.tokens > cfg.delegation.max_context_transfer_tokens:
      semaphore.release()
      return Refusal { TRANSFER_TOO_LARGE, projection.total.tokens }

  h <- SubAgentHandle {
      id: newId(), parent: ctx.turn, depth: ctx.depth + 1,
      spec, projection, allowance,
      guards: GuardSpec {
          max_tool_calls: cfg.delegation.max_tool_calls_per_subagent,
          max_retries: 1, wall_clock_ms: spec.wall_ms,
          token_budget: tokensFor(allowance, spec.model) },
      ledger_entry: ledger.openChild(ctx.turn, id)
  }
  ctx.session.subagents++
  return h
```

**`TRANSFER_TOO_LARGE` is a refusal, not a truncation.** If the minimal context needed for a subtask
exceeds the transfer cap, delegation was the wrong shape for that subtask — the work belongs in the
primary, where the context already is. Truncating the transfer instead produces a sub-agent working
from partial information and returning a confident answer built on it.

**Depth attenuation, not just a depth limit.** With `depth_attenuation: 0.5` a depth-1 agent gets half
the parent's allowance and depth-2 a quarter, so the worst-case spend of an entire tree converges
regardless of branching. A bare depth limit bounds the tree's *height* and lets its *cost* grow with
breadth at every level.

---

## 3. `prepareMinimalContext`

```text
function prepareMinimalContext(spec, ctx) -> ProjectionSpec:

  # Start from nothing. This is the whole design: a sub-agent's context is
  # constructed from an allowlist, never filtered down from the primary's.
  items <- []

  for id in spec.inputs:
      it <- ctx.state.item(id)
      if it == null: fail(SPEC, "input {id} not resolvable")
      if not securityEnvelope.authorize(READ, spec.auth, it.provenance.scope): continue
      items.add(it)

  # Dependency closure, but only within the allowlist. An input whose meaning
  # depends on an item the caller did not list is a spec error, surfaced rather
  # than silently repaired by pulling in the primary's context.
  missing <- transitiveDeps(ctx.dep_graph, items) \ set(spec.inputs)
  if missing.any():
      ledger.record(SUBAGENT_SPEC_INCOMPLETE, spec.role, missing)
      if cfg.delegation.strict_inputs: fail(SPEC, "unlisted dependencies: {missing}")
      items <- items ++ resolve(missing)                   # permissive mode: add and record

  # Large inputs travel as references, not payloads. The sub-agent fetches what
  # it needs, which is usually less than the whole thing.
  for it in items where it.size.tokens > cfg.delegation.inline_threshold_tokens:
      replaceWithReferenceStub(items, it)

  layers <- [
      SystemLayer(roleprompt(spec.role)),                  # purpose-scoped, not the primary's
      SchemaLayer(toolProjectionFor(spec.tools)),          # only the subtask's tools
      TurnLayer(items ++ [ resultContract(spec.result_schema) ])
  ]
  # No skill layer. No memory layer. No conversation history. By construction.

  p <- ProjectionSpec { layers, breakpoints: [], total: sum(sizeOf(layers)) }
  assert not containsAny(p, ctx.projection.history)        # the summariser trap, asserted
  return p
```

**The assertion on the last line is worth its cost.** The named anti-pattern — a summariser sub-agent
handed the full context it was spawned to shrink — cannot be prevented by intent alone, because it
arrives through refactoring: someone adds "just the recent history for context" and the delegation
silently stops paying. Comparing the sub-projection against the primary's history layer catches it in
development.

**No memory layer, deliberately.** A sub-agent that can query long-term memory is a second agent with
its own retrieval costs and its own opportunity to pull untrusted material into a result the primary
will trust. If a subtask needs a fact from memory, the primary retrieves it and lists it as an input —
which also makes it visible in the transfer cost.

---

## 4. `executeSubAgent`

```text
function executeSubAgent(h, ctx) -> BranchResult:

  ck <- checkpointContext(ctx.working)                     # D4 §9; O(1)
  deadline <- now() + h.guards.wall_clock_ms

  try:
      raw <- runtime.run(
          projection = h.projection,
          model      = h.spec.model,                       # reasoning NONE for mechanical work
          guards     = h.guards,
          structured = h.spec.result_schema,
          ledger     = h.ledger_entry)

      result <- compressResult(raw, h)                     # §5

      if result.invalid:
          if h.retries < cfg.delegation.result_schema_retries:
              h.retries++
              # Restate the contract; do NOT resend or enlarge the context.
              return executeSubAgent(h.withContractRestated(), ctx)
          terminateSubAgent(h, "result contract not met")
          return BranchResult.failed(h, SCHEMA_VIOLATION, ck)

      result.trust <- h.spec.trust_ceiling                 # INV-5, applied on the way out
      ledger.closeChild(h.ledger_entry, realized = result.cost)
      recordDelegationROI(h, result)                       # §9
      return BranchResult.ok(h, result, ck)

  catch TimeoutError:
      terminateSubAgent(h, "deadline exceeded")
      return BranchResult.failed(h, TIMEOUT, ck)
  catch BudgetExceeded:
      terminateSubAgent(h, "allowance exhausted")
      return BranchResult.failed(h, BUDGET, ck)
  finally:
      semaphore.release()
```

**The retry restates the contract and nothing else.** A sub-agent that returned malformed output does
not need more context; it needs the schema said again. Resending an enlarged context on retry is how a
delegation that was marginally profitable becomes a loss, and it is the reflex worth designing out.

**`BranchResult.failed` carries the checkpoint.** The reducer never sees a failed branch's partial
output; the checkpoint is what the dispatcher rolls back to, and the failure contributes exactly one
normalized line (D4 §10).

**Trust is stamped on exit, not on entry.** Computing the ceiling at spawn and applying it at return
means no code path inside execution can raise it, whatever the sub-agent produced or claimed.

---

## 5. `compressResult`

```text
function compressResult(raw, h) -> StructuredResult:

  parsed <- parseAgainst(raw, h.spec.result_schema)
  if parsed.is_error:
      return StructuredResult.invalid(parsed.error)

  # Strip everything the contract did not ask for. Preambles, restatements of
  # the question, and chain-of-thought are the bulk of a verbose return.
  clean <- projectTo(parsed, h.spec.result_schema)
  assert not containsReasoningTrace(clean)

  size <- estimateTokens(render(clean), h.parent_model.tokenizer)
  if size.tokens > cfg.delegation.result_max_tokens:
      # Over-contract results are reduced by the normal middleware, with a
      # manifest — a sub-agent result is a payload like any other.
      clean <- payloadMiddleware.reduce(clean, budget(cfg.delegation.result_max_tokens), h.task)

  return StructuredResult {
      value: clean, size, confidence: parsed.confidence ?? null,
      trust: h.spec.trust_ceiling, provenance: provenanceOf(h),
      cost: h.ledger_entry.realized }
```

**Structured output is enforced, not requested.** An instruction to "return JSON" is a suggestion a
model can decline under load; a schema constraint at the API level is not. This is also where the
output-token saving comes from — the schema has no slot for a preamble.

**A sub-agent result is a payload.** Running it through the same middleware means an over-long result
gets the same drop manifest and the same recoverability as any tool response, rather than a special
truncation path that loses information silently.

---

## 6. `reduceParallelResults` — the sole write path

```text
function reduceParallelResults(branches, spec, ctx) -> ReducedResult:

  assert spec.order == "DEPENDENCY_THEN_DISPATCH"          # V9
  assert spec.atomic                                       # INV-4

  # --- 1. Deterministic ordering. Completion order is never consulted. -----
  ok     <- [ b for b in branches if b.succeeded ]
  failed <- [ b for b in branches if not b.succeeded ]
  ordered <- topologicalSort(ok, by = spec.dependencies)
             .thenBy(b => b.dispatch_index)

  # --- 2. Deduplicate overlapping facts ------------------------------------
  facts <- []
  for b in ordered:
      for f in factsOf(b.result):
          twin <- findEquivalent(facts, f, spec.dedupe)
          if twin == null: facts.add(f.withSource(b))
          else:            twin.corroborate(b)             # agreement raises confidence

  # --- 3. Resolve conflicts by explicit precedence -------------------------
  disputed <- []
  for group in conflictingGroups(facts):
      winner <- applyPrecedence(group, cfg.parallelism.reducer.conflict_precedence)
      if winner == null:
          disputed.add(Disputed { claims: group, reason: NO_PRECEDENCE_RESOLVES })
      else:
          keep(winner); demoteOthersToHistory(group \ winner)

  # --- 4. Trust floor across the merge -------------------------------------
  merged_trust <- min(f.trust for f in facts)              # INV-5 across the whole reduction

  # --- 5. One atomic commit -------------------------------------------------
  tx <- canonicalState.transaction(ctx.task)
  tx.applyFacts(facts)
  tx.applyDisputed(disputed)                               # visible, never silently dropped
  tx.applyPending(mergePendingActions(ordered))
  for b in failed: tx.applyErrorLine(normalize(b.failure))
  tx.commit()                                              # the only write in the entire turn

  ledger.record(REDUCE, { branches: len(branches), failed: len(failed),
                          facts: len(facts), disputed: len(disputed) })

  return ReducedResult { facts, disputed, trust: merged_trust,
                         cost: sum(b.result.cost for b in ok) }
```

**Ordering is topological first, dispatch index second.** Dependency order is what makes the result
*correct*; dispatch index is what makes it *reproducible* among independent branches. Both are needed:
topological sort alone leaves ties, and ties resolved by completion time reintroduce the
non-determinism the reducer exists to remove.

**Corroboration is not deduplication.** Two branches independently reporting the same fact is
evidence, and collapsing them to one silently discards it. The survivor records that it was
corroborated, which feeds the confidence the primary sees.

**Disputes are committed, not resolved by fiat.** When the precedence rules — higher trust, then more
recent `valid_from`, then lower dispatch index — do not separate two claims, both are written as
disputed and surfaced. Picking one at random is the failure INV-9 exists to prevent, and it is
invisible afterwards.

**Failed branches contribute one line each and nothing else.** They appear in the transaction only as
normalized errors, so the committed state contains no residue from work that did not complete.

---

## 7. `mergeResult`

The single-branch case, routed through the same path so there is exactly one way state is written.

```text
function mergeResult(result, ctx) -> void:
  reduceParallelResults([BranchResult.ok(handle(result), result, ctx.checkpoint)],
                        ReducerSpec.singleton(), ctx)
```

Deliberately trivial. A separate merge path for the single-branch case is how the dedupe, conflict and
trust-floor logic ends up applying only to parallel work — and then a bug that only appears with one
sub-agent becomes very hard to explain.

---

## 8. `terminateSubAgent`

```text
function terminateSubAgent(h, reason) -> void:
  runtime.cancel(h.id)                                     # cooperative, then hard at grace expiry
  archive.put(h.partialOutput(), provenance = provenanceOf(h))   # for diagnosis only
  h.partialOutput <- null                                  # never reaches context or the reducer
  semaphore.release()
  ledger.closeChild(h.ledger_entry, realized = h.spent, outcome = reason)
  circuitBreaker(h.spec.role).record(failure)
  ctx.session.subagents_terminated++
```

**Partial output is archived and then dropped.** It is genuinely useful for diagnosis and genuinely
dangerous in context: a half-finished analysis reads as an analysis. The archive keeps it; the turn
does not see it.

**A circuit breaker keyed on the sub-agent role.** A "summarise these findings" delegation that fails
four times in two minutes is not going to succeed on the fifth, and the breaker sends the work back to
the primary rather than continuing to pay for it.

---

## 9. Closing the economic loop

```text
function recordDelegationROI(h, result) -> void:
  predicted <- h.spec.predicted_native_cost                # captured at shouldDelegate time
  actual    <- h.ledger_entry.realized
  ledger.record(SUBAGENT_ROI, {
      role: h.spec.role, workload: h.task.workload,
      predicted_saving: predicted - h.spec.predicted_sub_cost,
      realized_saving:  predicted - actual,
      transfer_tokens:  h.projection.total.tokens,
      cold_prefix_tokens: h.projection.total.tokens })     # all of it was cold
  biasMonitor.record(h.spec.predicted_sub_cost, actual, DELEGATION_ESTIMATOR)
```

This is what makes the delegation planner improve rather than merely execute. `subagent_roi` per role
per workload becomes the prior `shouldDelegate` consults on the next comparable task, and the bias
monitor catches a planner that systematically underestimates delegation cost — which is the direction
the error usually runs, because transfer and coordination are easy to forget and easy to under-count.

**Note what `cold_prefix_tokens` records.** For a sub-agent it is the entire projection: none of it was
cached. Recording it separately means the ledger can answer "how much of our delegation spend is cold
prefix?", which is the number that decides whether delegation is worth doing at all in a warm-session
workload.

---

## 10. Protections, and what each one actually stops

| Protection | Mechanism | Failure it prevents |
|---|---|---|
| Recursive delegation | Depth counter in the handle, checked before spawn | An agent that delegates its own delegation indefinitely |
| Runaway breadth | Per-turn and per-session count caps, plus a semaphore | Fan-out that exhausts concurrency and rate limits |
| Runaway cost | Dollar spawn budgets, per turn and per session | Four sub-agents at 30,000 tokens each, "within" a count cap |
| Tree cost growth | Depth attenuation of the allowance | Cost growing with breadth at every level under a height-only limit |
| Duplicated transfer | Explicit input allowlist, reference stubs above a threshold | The same context paid for N times across N branches |
| The summariser trap | Assertion that the sub-projection shares nothing with primary history | A shrinking task handed the thing it was meant to shrink |
| Optimizer recursion | `forbid_skill_injection`, asserted at spawn | The optimizer's overhead recreated inside every worker |
| Trust laundering | Ceiling computed at spawn, applied at return, floored again at reduce | Untrusted content becoming trusted by passing through a sub-agent |
| Verbose returns | Enforced structured output, then middleware reduction | Chain-of-thought and preambles billed as output |
| Non-determinism | Topological then dispatch ordering, single atomic commit | State that depends on which branch finished first |
| Residue | Checkpoint rollback, partial output archived and dropped | A failed branch's half-answer read later as data |
| Persistent bad delegation | ROI recording plus a role-keyed circuit breaker | Paying repeatedly for a delegation that never pays back |

---

## 11. Failure and demotion

| Function | Failure | Demotion | Cost |
|---|---|---|---|
| `spawnSubAgent` | Any gate refuses | `NATIVE` execution in the primary | Latency; no correctness impact |
| `prepareMinimalContext` | Input unresolvable, unlisted dependency | Refuse the spawn (strict) or add and record (permissive) | Native execution, or a wider transfer |
| `executeSubAgent` | Timeout, budget, schema violation | Terminate, roll back, execute natively | The spawn cost is lost |
| `compressResult` | Unparseable output | One contract-restating retry, then terminate | One extra sub-agent call |
| `reduceParallelResults` | Precedence does not resolve | Commit as disputed | The primary must resolve it |
| `reduceParallelResults` | Transaction fails | Roll back all branches; nothing commits | Whole turn's parallel work lost, state consistent |
| `terminateSubAgent` | Cancellation ignored | Hard kill at grace expiry; handle abandoned | Orphan spend, alarmed |

The row worth noting is the transaction failure: **all or nothing**. Committing three of four branches
would leave state that no execution path could have produced, and debugging that is harder than
redoing the work.

---

## 12. Worked example — three parallel branches, one failure

Analytics task: reconcile SLA breaches across three regions. Independent, so `PARALLEL_SUBAGENTS`.

```text
shouldDelegate    native  $0.41 (three sequential 12k-token result sets through context)
                  sub     $0.14 = transfer $0.02 + cold $0.05 + routing $0.001
                                + work $0.06 + merge $0.01
                  sub.hi $0.16 < native.lo $0.38 − $0.02        → DELEGATE

spawn             depth 0 → 1 · allowance $0.25 × 0.5^0 = $0.25 · semaphore 3/4
                  each spec: role "regional SLA reconciliation", tools [db.query],
                             inputs [sla_policy_v3, region_filter_{n}], reasoning NONE,
                             result_schema { region, breaches:int, worst_minutes:int,
                                             top_teams:[{team,count}] }
                  trust_ceiling = min(USER, TOOL) = TOOL

execute           EU     ok    418 tok result   $0.041
                  APAC   ok    402 tok result   $0.039
                  US     fail  timeout at 30 s  $0.022 spent, archived, rolled back

reduce            ordered by dispatch index: [EU(0), APAC(1)]      US absent
                  facts: 2 regional summaries, no overlap → no corroboration
                  conflicts: none
                  merged_trust = TOOL
                  commit: 2 fact sets + 1 error line
                          "[subagent:us-region failed — deadline exceeded at 30s; details err:4c81]"

realized          $0.102 against a predicted $0.14 and a native $0.41
                  SUBAGENT_ROI recorded: predicted saving $0.27, realized $0.31
```

Two things this is chosen to show. **The failed branch cost $0.022 and contributed one line** — no
partial regional data entered context, so the primary cannot mistake two regions for three; the error
line is what tells it the answer is incomplete. And **the ROI beat prediction**, which is recorded and
feeds back: the delegation estimator was 12% pessimistic on this workload, and the bias monitor will
correct the prior rather than leaving the planner permanently conservative.

---

## 13. Test hooks (for D11)

| Test | Asserts |
|---|---|
| Determinism | 1,000 runs with randomized completion order produce byte-identical committed state |
| Ordering | Dependent branches commit in dependency order regardless of dispatch or completion |
| Atomicity | A transaction failure leaves zero branch results committed |
| No context leakage | No sub-agent projection shares an item with the primary's history layer |
| No skill injection | No sub-agent projection contains the optimizer skill text |
| Trust ceiling | For every input trust combination, result trust equals the minimum |
| Trust floor at merge | Merged trust equals the minimum across all contributing facts |
| Recursion bound | A deliberately recursive spec terminates at `max_depth` |
| Tree cost bound | A branching recursive workload's total spend converges under attenuation |
| Spawn budget | Cost caps bind before count caps when results are large |
| Corroboration | Two branches asserting the same fact yield one fact with corroboration recorded |
| Dispute surfacing | Unresolvable conflicts appear as disputed, never silently resolved |
| Residue | After a branch failure, zero committed items carry that branch id |
| Result contract | A non-conforming result retries exactly once, with no context enlargement |
| ROI loop | Every completed sub-agent writes a `SUBAGENT_ROI` record and a bias sample |
