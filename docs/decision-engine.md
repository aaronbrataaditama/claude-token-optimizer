# §17 — The Decision Engine

**Stage 1 of 5.** The central optimization decision procedure: the `NO_OPTIMIZATION` fast path, the
optimization-overhead budget, the resolved evaluation order of the sixteen questions, the `decide()`
algorithm, tie-breaking, escalation, and worked examples.

Companion documents: [`architecture.md`](architecture.md) (Deliverable 2), [`flow.md`](flow.md) (§21).

---

## 1. Contract

The Decision Engine (C8) is a **pure function** from a state snapshot to an immutable `ExecutionPlan`.
It performs no I/O of its own; it consults advisors, each of which reads a snapshot and returns a
decision fragment.

```ts
decide(turn: TurnInput, state: StateSnapshot, meter: OverheadMeter): ExecutionPlan
```

Three properties are non-negotiable:

1. **It always returns a plan.** Never an exception, never null. On internal failure it returns
   `mode: 'DEMOTED'` with a pass-through plan (INV-2).
2. **It charges itself.** Every advisor invocation is metered against the overhead cap before it runs
   and charged after (INV-3). The plan reports its own cost in `overhead`.
3. **It records why.** `trace: DecisionTrace[]` carries one entry per stage evaluated, including the
   stages that were skipped and the reason. A plan whose rationale cannot be reconstructed from its
   trace is a bug, because savings that cannot be attributed cannot be defended.

---

## 2. Step 0 — is optimization worthwhile at all?

`NO_OPTIMIZATION` is a legitimate, first-class outcome. For a small task with a warm cache, a small
context, one or two tools and a cheap model, invoking classifiers, scorers, routers and compression
layers costs more than the turn being optimized.

```text
                        Task
                          |
             Is optimization worthwhile?
              /                        \
            NO                         YES
             |                           |
   fast path: zero optimizer      run the full engine
   overhead, free controls only        (stages 1-16)
```

### 2.1 Eligibility predicate

Deterministic, no LLM, no retrieval. Every input is already in hand from the previous turn's ledger
entry and the cache state. Cost: a handful of comparisons.

```text
fastPathEligible(turn, state) :=
      state.context_utilization        <  cfg.fast_path.max_utilization        // default 0.35
  AND state.candidate_tool_count       <= cfg.fast_path.max_tools              // default 3
  AND state.cache.prefix_warm          == true
  AND task_risk_prior(turn)            == LOW        // from the previous turn, not a new classify
  AND turn.has_media                   == false
  AND estimated_baseline_usd(turn)     <  cfg.fast_path.min_turn_cost_usd      // default 0.01
  AND state.last_turn.flags            has none of {BUDGET_PRESSURE, LOOP_SUSPECTED, RETRY_ESCALATED}
  AND state.consecutive_fast_path      <  cfg.fast_path.max_consecutive        // default 20
```

Two of these deserve explanation.

**`task_risk_prior` uses the previous turn's classification, not a fresh one.** Running the classifier
to decide whether the classifier is worth running is self-defeating. The prior is carried forward and
is deliberately sticky; the first turn of a session is never fast-path eligible, so a real
classification always exists before the fast path can be taken.

**`max_consecutive` forces periodic re-evaluation.** Without it, a session that starts small and
grows slowly can ride the fast path past the point where optimization would pay, because each turn
individually looks small. Every twentieth turn runs the full engine regardless, which bounds the
error at 20 turns of missed savings and produces a fresh prior.

### 2.2 What the fast path still does

The fast path is not "no optimization". It is **no *metered* optimization**. Controls whose cost is a
configuration lookup are always applied, because their overhead is genuinely zero:

| Control | Source | Why it is free |
|---|---|---|
| `max_tokens` tuned per operation class | §13 | Table lookup keyed by intent |
| Stop sequences | §13 | Table lookup |
| Structured output when the caller declared a schema | §13 | Already declared |
| Streaming early termination when a predicate exists | §13 | Already declared |
| Stable prefix ordering and append-only discipline | §9 | Structural in C10; costs nothing to preserve |
| Per-tool hard result budgets | §4 | A number compared to a number |
| Drop manifest on any truncation | INV-6 | ~30 tokens, and correctness-bearing |
| Trust labelling and sanitization | §19 | Fails closed; never skipped |

What the fast path skips: classification, context scoring, tool routing, model routing, delegation
planning, compaction analysis, and representation selection. Those are the metered advisors.

### 2.3 Fast-path exit

If a fast-path turn ends with `BUDGET_PRESSURE`, a loop suspicion, an escalated retry, or a tool
result that blew its budget, the next turn is forced through the full engine and the risk prior is
raised. The fast path is optimistic, and the exit is how it is corrected cheaply.

---

## 3. The optimization overhead budget

The entire optimization layer — not just the router — has a budget:

```text
optimization_overhead = router_cost + classifier_cost + summarizer_cost
                      + retrieval_cost + token_estimation_cost + coordination_cost

Enable an optimization only when:  expected_savings > optimization_overhead
```

### 3.1 The cap

```text
overhead_cap = max( cfg.overhead.floor_usd,
                    cfg.overhead.max_fraction * estimated_baseline_turn_cost )

defaults: floor_usd = 0.0005, max_fraction = 0.10
```

The floor exists so that a very cheap turn can still afford a deterministic advisor or two; the
fraction exists so that an expensive turn can afford real analysis. The cap is enforced live by
`OverheadMeter`, and a plan that exceeds it is truncated at the last advisor that fit rather than
abandoned — a partial plan built from three advisors is still better than a pass-through plan.

### 3.2 Advisor scheduling

This is the mechanism that makes INV-3 operational rather than aspirational. Advisors are not a fixed
pipeline. They are **scheduled greedily by expected savings per unit of overhead**, drawing on the
Ledger's EWMA of realized savings per advisor *per workload class*.

```text
for each advisor a in ADVISORS:
    cost_est(a)     <- a.declaredCost(state)                  // cheap, deterministic
    savings_est(a)  <- ledger.priors(workload).realized[a]    // EWMA, wide interval when cold
    ratio(a)        <- savings_est(a).lo / cost_est(a).hi     // conservative: low savings, high cost

sort ADVISORS by ratio descending
for a in ADVISORS:
    if ratio(a) <= 1.0:                    break   // no advisor after this one pays either
    if not meter.canAfford(cost_est(a)):   continue // a cheaper advisor later may still fit
    fragment <- orchestrator.guard(a, deadline, () => a.advise(snapshot), a.neutralFragment())
    meter.charge(a, actual)
    merge(fragment)
```

Four consequences worth stating:

* **A cold advisor has a wide interval, so `ratio` uses its pessimistic bound.** New advisors do not
  get to run on optimism. They earn their slot through shadow runs (C24), which is where their prior
  comes from.
* **Ranking is per workload class.** The tool router earns its slot on tool-heavy research and loses
  it on conversational Q&A, automatically, from measured data rather than from a hand-written rule.
* **`break` versus `continue` is deliberate.** Once ratio drops to 1.0 nothing further is worth
  running at all; but an advisor that merely does not *fit* is skipped while cheaper ones behind it
  are still considered.
* **Dependencies are respected.** Some fragments require others (context sizing needs the model
  choice, INV-8). Ordering is a topological sort within ratio bands, not a free sort; an advisor whose
  prerequisite was skipped contributes its neutral fragment.

### 3.3 The self-reference guard

The engine's own analysis must not consult an LLM more than once per turn without an explicit flag.
An optimizer stack of the form *Optimizer -> Router -> Analyzer -> Scorer -> Estimator -> Sub-Agent*
is the failure this whole section exists to prevent. Configuration validation caps the number of
Tier-2-or-above advisor calls per turn (default: 1) and the fast path caps it at 0.

---

## 4. The sixteen questions, resolved into an evaluation order

The specification lists sixteen questions. Their listed order is thematic; the **evaluation** order
must satisfy three constraints:

1. **Cheapest exits first.** A question that can end the turn should be asked before any question
   that costs money to answer.
2. **Dependencies before dependents.** Context sizing cannot precede model selection, because a
   budget is only valid against a tokenizer (INV-8).
3. **Irreversible commitments last.** Anything that mutates a cached prefix is decided after
   everything that might change what the prefix needs to contain.

### 4.1 Mapping

| Stage | Question | Spec Q# | Short-circuits? |
|---|---|---|---|
| S0 | Is optimization worthwhile at all? | — (added) | Yes, to fast path |
| S1 | Can existing context solve it? | Q2 | Yes, to answer |
| S2 | Can memory solve it? | Q3 | Yes, to answer |
| S3 | Can an existing result or cache entry be reused? | Q4 | Yes, to answer |
| S4 | Is a tool required at all? | Q1, Q6a | Yes, to S9 |
| S5 | Can generated code over external data solve it? | Q5 | Redirects execution |
| S6 | Which tools, and how much schema? | Q6b, Q7 | No |
| S7 | Which operations are independent? | Q11 | No |
| S8 | Should work be delegated? | Q10 | No |
| S9 | Which model and reasoning effort? | Q9 | No; forces budget recompute |
| S10 | How much context is required? | Q8 | No |
| S11 | Should context be compacted? | Q15 | No |
| S12 | What result size, and what representation? | Q12, Q13 | No |
| S13 | Should results be cached? | Q14 | No |
| S14 | What output controls apply? | — (added, from §13) | No |
| S15 | Is the final answer budget sufficient? | Q16 | Loops once to S10 |
| S16 | Emit plan and register expected savings | — | Terminal |

Two deviations from the listed order, each with a reason:

* **Q8 (how much context) moved after Q9 (which model).** A budget computed at 70% of a 128k window
  is not valid after routing to a different family. Sizing context before choosing the model produces
  a budget that must be recomputed anyway, and worse, may have driven selection decisions using the
  wrong numbers.
* **Q5 (code bypass) moved ahead of Q6/Q7 (tool and schema selection).** On data-heavy tasks the
  bypass eliminates the tool projection entirely; deciding disclosure levels for tools that will be
  imported into a sandbox rather than rendered into context is wasted analysis.

### 4.2 Stage detail

**S1 — Can existing context solve it?** Free. Checks whether the answer is already present in the
current projection. This is a *sufficiency* check, not a similarity check: partial presence does not
short-circuit, because answering from partially present information is precisely how confident-wrong
answers are produced. Requires `confidence >= cfg.sufficiency.min` or it falls through (INV-9).

**S2 — Can memory solve it?** VOI-gated retrieval. `netInformationValue` for a memory query includes
the retrieval cost, the tokens the hits will occupy, and the probability that a hit changes the
answer. Query carries `as_of` so superseded facts are excluded, and `min_trust` so untrusted material
cannot silently become the basis of an answer.

**S3 — Can an existing result be reused?** Free lookups against C20: result cache by idempotency key,
semantic cache by normalized query. The semantic cache is scope- and auth-keyed and validated before
reuse when the answer is time- or authorization-sensitive. A personalized or permission-scoped query
that would need validation costing more than the call it saves is not a cache candidate at all.

**S4 — Is a tool required?** Combines "can this be solved without a tool" with "is a tool required".
If no, the turn jumps directly to S9 (model and reasoning) — there is nothing to route, parallelize
or delegate.

**S5 — Bypass check.** Triggered by `TaskClass.data_heavy`, by an estimated aggregate payload above
`cfg.bypass.min_payload_tokens`, or by more than `cfg.bypass.min_calls` homogeneous calls. When it
fires, the plan becomes a single `CODE_BYPASS` step and stages S6-S8 are largely bypassed. The
economic test is not marginal on qualifying tasks: routing 4,000 records through context to compute
one aggregate loses to a 40-line script by an order of magnitude, and the check is deliberately
early because everything after it would be analysis of a path not taken.

**S6 — Tools and schema.** C5 returns candidates, per-tool disclosure levels, and critically
`prefix_stable` plus `churn_cost`. The engine then makes the trade the specification calls out: a
smaller, churning tool projection versus a larger, stable one. `churn_cost` is compared against the
schema tokens saved over `expected_remaining_turns`. For short sessions and hot tool sets the stable
projection usually wins.

**S7 — Independence.** Builds the dependency graph (architecture §9.3). Independent operations become
a `PARALLEL` step with a `ReducerSpec`. Uncertain dependency resolves to sequential (INV-9).

**S8 — Delegation.** C7 chooses among native, lightweight router, sub-agent, parallel sub-agents and
escalation, including cache state in the economics. A warm primary prefix raises the bar for
delegation, because the sub-agent starts cold.

**S9 — Model and reasoning.** C6 selects tier and reasoning effort per step, not per turn: a
classification step inside a turn whose final answer needs a Tier-4 model still gets Tier-2 with
`reasoning: NONE`. Any change of model family triggers `C2.recomputeFor` on every budget (INV-8), and
the recomputation is verified against the `bytes` bound before the plan may proceed.

**S10 — Context sizing.** C3 allocates; C4 scores and assigns dispositions; dependency closure
promotes items that retained items depend on. The output is the selected item set and the budget
split, not yet a rendered projection.

**S11 — Compaction.** The most consequential stage economically, and the one most often got wrong.
See §5.

**S12 — Result size and representation.** Per-tool budgets and the C12 reduction ladder depth,
plus candidate representations measured rather than assumed.

**S13 — Caching.** What to cache, with what key, at what TTL, and — for side-effectful calls — under
what idempotency key. Scope and auth fingerprints are part of every key by construction.

**S14 — Output controls.** `max_tokens` for the operation class, stop sequences, structured output,
streaming early-exit predicate. These also apply on the fast path.

**S15 — Answer budget sufficiency.** If the reserved output budget cannot accommodate the expected
answer, the engine re-enters S10 **once** with tighter dispositions. One bounded iteration only: a
loop here is an optimizer that oscillates instead of deciding, and after one pass it accepts the
tighter plan or demotes.

**S16 — Emit.** Validate (§7), register expected savings with C22, return.

---

## 5. Compaction: the decision that most often goes backwards

Stage S11 is where an intuitive optimization becomes a cost increase. The gate:

```text
compaction_benefit =
      ( tokens_removed x input_price x expected_remaining_turns )
    - ( cache_discount_lost x cached_tokens_invalidated x expected_remaining_turns )
    - summarization_cost

COMPACT_NOW only when compaction_benefit > 0
```

Three rules refine it.

### 5.1 Reduce the tail, preserve the head

Items differ enormously in what removing them costs, based purely on position. Removing an item
*before* the last cache breakpoint invalidates everything after it; removing an item *after* the last
breakpoint invalidates nothing. So the engine's first move under budget pressure is never "summarize
the conversation" — it is to externalize the largest items in the uncached tail, which yields the same
token relief at zero invalidation cost.

The candidate ordering under pressure is therefore:

```text
1. Externalize large TURN-stability items in the uncached tail   (zero cache cost)
2. Externalize large TASK-stability items after the last breakpoint
3. Retrieve-later conversions for items whose reconstruction is cheap
4. Compact the cached head                                        (full invalidation cost)
```

Step 4 is reached only when steps 1-3 cannot free enough, which on most turns they can.

### 5.2 Batch at boundaries

Pending compactions accumulate and are applied in a single prefix edit at a cache boundary — session
start, task transition, or TTL lapse. Two separate compactions of the same prefix pay the
invalidation cost twice for one benefit, and the specification's instruction to compact at cache
boundaries is exactly this.

### 5.3 Emergency overrides economics

In the `EMERGENCY` band the gate is suspended. The alternative to compaction there is a turn that
does not fit in the window, and a failed turn costs more than an invalidated cache. This is the one
place where the engine knowingly takes a negative-benefit action, and it is recorded as such in the
ledger so it appears as an emergency event rather than as an inexplicable cost spike.

Entering `EMERGENCY` frequently is a signal that the budget allocation or the retention policy is
wrong, and C24 treats its rate as a regression metric.

---

## 6. `decide()` — the algorithm

```text
function decide(turn, state, meter) -> ExecutionPlan:

  trace <- []
  plan  <- Plan.pass_through(turn, state)        // always a valid answer from this point on

  # ---------- S0: fast path ----------
  if fastPathEligible(turn, state):
      plan.mode <- NO_OPTIMIZATION
      applyFreeControls(plan, turn)              // max_tokens, stop, structured, budgets, manifest
      trace.add(S0, taken=FAST_PATH, overhead=0)
      return finalize(plan, trace, meter)

  # ---------- setup ----------
  baseline <- costModel.baseline(turn, state)                  // the counterfactual (Estimate)
  meter.cap <- max(cfg.overhead.floor_usd,
                   cfg.overhead.max_fraction * baseline.mid.usd_total)
  task      <- guard(C1, () => classifier.classify(turn, state), NEUTRAL_TASK_CLASS)
  schedule  <- scheduleAdvisors(meter.cap, ledger.priors(task.workload))

  # ---------- S1..S3: can we avoid the work entirely? ----------
  if sufficient(state.projection, turn) with confidence >= cfg.sufficiency.min:
      return finalize(plan.answerFromContext(), trace.add(S1, exit=CONTEXT), meter)

  if schedule.includes(MEMORY) and voi(memoryQuery(turn), state) > 0:
      hits <- memory.retrieve({ ...query, as_of: now, min_trust: cfg.min_trust, budget: b.memory })
      if answers(hits, turn) with confidence >= cfg.sufficiency.min:
          return finalize(plan.answerFromMemory(hits), trace.add(S2, exit=MEMORY), meter)
      plan.addContext(hits)                       // not sufficient alone, still useful

  reuse <- cache.lookup(turn)                     // result cache, then semantic cache
  if reuse.hit and reuse.valid_for(scope, auth, freshness):
      return finalize(plan.answerFromCache(reuse), trace.add(S3, exit=CACHE), meter)

  # ---------- S4: is a tool needed? ----------
  needs_tool <- toolNecessity(turn, task, state)  // deterministic rules first, Tier-2 only if scheduled
  if not needs_tool:
      trace.add(S4, exit=NO_TOOL)
      goto S9

  # ---------- S5: code-execution bypass ----------
  if bypassCandidate(task, state):
      est_bypass <- costModel.price(CODE_BYPASS_step(turn), plan.model, plan.projection)
      est_native <- costModel.price(NATIVE_sequence(turn), plan.model, plan.projection)
      if est_bypass.hi.usd_total < est_native.lo.usd_total:      // conservative comparison
          plan.steps <- [ CODE_BYPASS { program, sandbox, budget } ]
          trace.add(S5, chosen=BYPASS, saved=est_native.mid - est_bypass.mid)
          goto S9

  # ---------- S6: tools and schema ----------
  cands <- toolRouter.candidates(task, k=cfg.router.k)
  proj  <- toolRouter.project(cands, cache.state())
  if not proj.prefix_stable:
      schema_saved <- proj.tokens_saved_vs_stable * input_price * expected_remaining_turns
      if schema_saved <= proj.churn_cost.hi:
          proj <- toolRouter.projectStable(cands)      // larger, but the prefix survives
          trace.add(S6, chosen=STABLE_PROJECTION, reason=CACHE_CHURN)
  plan.projection.schemas <- proj

  # ---------- S7: independence ----------
  graph <- dependencyGraph(candidateOperations(turn, proj))
  if graph.independentGroups().any() and cfg.parallelism.enabled:
      plan.steps <- parallelize(graph)                 // PARALLEL step + ReducerSpec
  else:
      plan.steps <- sequential(graph)

  # ---------- S8: delegation ----------
  if schedule.includes(DELEGATION):
      d <- delegationPlanner.plan(task, state, cache.state())
      if d.k != NATIVE and expectedTotalCost(d).hi < expectedTotalCost(NATIVE).lo:
          plan.steps <- applyDelegation(plan.steps, d)
      trace.add(S8, chosen=d.k)

  # ---------- S9: model and reasoning ----------
  S9:
  plan.model <- modelRouter.select(task, plan.budgets, cache.state())
  for step in plan.steps:
      step.model <- withReasoning(plan.model, modelRouter.reasoningEffort(task, step))
  if plan.model.tokenizer != plan.budgets.family:
      plan.budgets <- estimator.recomputeFor(plan.budgets, plan.model.tokenizer)   # INV-8
      assertWithinByteBound(plan.projection, plan.budgets)

  # ---------- S10: context sizing ----------
  S10:
  items <- state.candidateItems()
  for it in items: it.score <- selector.score(it, scoringContext(task, plan))
  for it in items: it.disposition <- selector.decide(it, plan.budgets)
  keep  <- selector.dependencyClosure(retained(items), state.depGraph)   # promote dependencies
  plan.projection.items <- keep

  # ---------- S11: compaction ----------
  band <- budgets.band(utilization(plan.projection))
  if band == EMERGENCY:
      plan.compaction <- COMPACT_NOW(targets=emergencyTargets(keep), reason=EMERGENCY)
  else if band in {COMPACT, AGGRESSIVE}:
      relief <- tailFirstCandidates(keep)            # §5.1 ordering
      if relief.sufficient():
          plan.compaction <- DEFER                   # externalize the tail instead; no prefix edit
          plan.projection.items <- applyExternalization(keep, relief)
      else:
          benefit <- costModel.compactionBenefit(headTargets(keep), cache.state(), horizon)
          plan.compaction <- (benefit.lo > 0)
                             ? COMPACT_NOW(headTargets(keep))
                             : DEFER(at_boundary=TASK_TRANSITION)
  else:
      plan.compaction <- DEFER

  # ---------- S12..S14: results, caching, output ----------
  for step in plan.steps where step.k == NATIVE_TOOL:
      step.budget <- budgets.budgetFor(step.tool, task)
      step.reduction_depth <- reductionDepthFor(step.budget, expectedSize(step))
      step.representation_candidates <- representationCandidates(step.tool)
      step.cache_policy <- cachePolicy(step, scope, auth)
      step.idem <- idempotencyKey(step)
  applyFreeControls(plan, turn)                       # same controls the fast path applies

  # ---------- S15: answer budget sufficiency ----------
  if plan.budgets.reserved_output < expectedAnswerTokens(turn, task):
      if not retried_once:
          tighten(plan.budgets); retried_once <- true; goto S10
      else:
          plan.mode <- DEMOTED                        # cannot fit; fall back rather than truncate
          trace.add(S15, exit=DEMOTE_INSUFFICIENT_OUTPUT_BUDGET)

  # ---------- S16: emit ----------
  return finalize(plan, trace, meter)


function finalize(plan, trace, meter):
    plan.trace     <- trace
    plan.overhead  <- meter.spent
    plan.estimated <- { baseline, optimized: costModel.price(plan), savings: baseline - optimized }
    validate(plan)                                    # §7; on failure return pass-through DEMOTED
    ledger.open(plan.turn, plan)
    return plan
```

---

## 7. Plan validation before emit

A plan that violates an invariant must never reach the data plane. `validate()` is a cheap set of
assertions, and any failure demotes the whole plan rather than repairing it — a partially repaired
plan is a plan nobody reasoned about.

| Check | Invariant |
|---|---|
| Every budget's `family` equals `plan.model.tokenizer` | INV-8 |
| Projection total plus reserved output is within the window, by bytes bound as well as tokens | INV-8 |
| Any non-append prefix edit is backed by an approved `CompactionDecision` with positive net benefit, or the `EMERGENCY` band | INV-7 |
| Every reduction step carries a manifest slot | INV-6 |
| Every `SUBAGENT` step has an explicit `inputs` allowlist and a `result_schema` | §11 |
| Every `SUBAGENT` step has `trust_ceiling == min(trust of inputs)` | INV-5 |
| Every side-effectful step has an idempotency key | §9 |
| `PARALLEL` steps have a `ReducerSpec` with `order: DEPENDENCY_THEN_DISPATCH` | INV-4 |
| No speculative branch is side-effectful | §12 |
| `plan.overhead <= meter.cap`, or `mode == DEMOTED` | INV-3 |
| Delegation depth within `max_delegation_depth` | §11 |

---

## 8. Tie-breaking and uncertainty

INV-9 in operation. `resolveTie(a, b, confidence)` applies when two options are within
`cfg.tie.band` (default 5%) on expected cost, or when the deciding estimate's confidence is below
`cfg.tie.min_confidence` (default 0.6):

| Situation | Resolution |
|---|---|
| Retain versus discard | **Retain** |
| Compress versus retain | **Retain** |
| Summarize versus externalize | **Externalize** — lossless |
| Reuse cache versus re-fetch | **Re-fetch**, when the answer is time- or authorization-sensitive |
| Parallel versus sequential | **Sequential** |
| Smaller versus larger model | **Larger**, when `task.risk` is HIGH; smaller otherwise |
| Lower versus higher reasoning effort | **Higher**, when `task.risk` is HIGH |
| Delegate versus native | **Native** |
| Bypass versus native | **Native**, unless the bypass wins on the conservative comparison |
| Trust conflict between two facts | **Surface both as disputed**; never silently pick |

Note the asymmetry in the model rows: uncertainty resolves toward the *more expensive* option only
when risk is high. On low-risk work, uncertainty resolving toward the expensive branch every time
would make the optimizer a cost amplifier — INV-9 is about not trading correctness for savings, not
about maximizing spend.

---

## 9. Escalation ladder

Applied by C16 on failure; the engine records it in the plan's `guards`. Ordered by cost, and each
rung requires that something has actually changed.

```text
0. Deterministic fix without an LLM      malformed argument, wrong enum, missing required field
1. Retry with changed input              only if the input differs; never the same call twice
2. Raise reasoning effort, same model    cheapest genuine capability increase
3. Escalate model tier                   next tier up, budgets recomputed for the new tokenizer
4. Fall back to an alternative tool      per the registry's declared equivalences
5. Abort with a normalized error         one line to context, details to the archive
```

Rung 2 is the rung most systems lack. Between "retry identically" and "call the expensive model"
there is a cheaper option that frequently works, and treating it as a routing decision rather than a
model attribute is what makes it available.

---

## 10. Worked examples

Prices used below are illustrative placeholders for a Tier-4 model at $3/M input, $15/M output, with
a 90% cached-input discount. The point is the shape of the arithmetic, not the constants; real values
come from Deliverable 8 (Stage 5).

### 10.1 Conversational Q&A, turn 7 — `NO_OPTIMIZATION`

**Situation.** 11,400-token context (utilization 0.09 of a 128k window), warm prefix, 2 candidate
tools, `task_risk_prior = LOW`, no media, estimated baseline $0.004.

**Decision.** All fast-path conditions hold. `mode = NO_OPTIMIZATION`.

| | Value |
|---|---|
| Optimizer overhead | ~0 (7 comparisons, no model call) |
| Free controls applied | `max_tokens = 400`, stop sequences, per-tool budgets, prefix order preserved |
| Turn cost | $0.004 |
| Cost had the full engine run | $0.004 + ~$0.0026 advisor overhead = $0.0066 |

**The point.** Running the engine would have increased this turn's cost by roughly 65% to find
savings that do not exist on an 11k-token turn. The fast path is not a convenience; on a
conversational workload it is the difference between an optimizer that pays and one that does not.

### 10.2 Database analytics — code-execution bypass

**Situation.** *"Which of our 4,312 open tickets breached SLA last quarter, grouped by team?"*
`workload = ANALYTICS`, `data_heavy = true`.

**S5 comparison.**

| Path | Shape | LLM tokens | Non-token | Total |
|---|---|---|---|---|
| Native, paginated + middleware | 14 tool calls, ~9k reduced tokens each accumulating in context | ~96,000 in / ~8,400 out | $0.02 DB | ~$0.42 |
| Code bypass | 1 script; query, join, aggregate in-sandbox; 210-token result | ~11,200 in / ~590 out | $0.004 exec + $0.01 DB | ~$0.06 |

`est_bypass.hi` ($0.071) is below `est_native.lo` ($0.38), so the bypass is taken on the conservative
comparison. Stages S6-S8 are skipped: there is no tool projection to size, nothing to parallelize in
context, and nothing to delegate.

**Optimizer overhead:** $0.0003. Two deterministic advisors ran; no advisor called a model.

**What the middleware path would have got wrong.** It is not that payload filtering fails — it works,
and reduces each response substantially. It is that filtering reduces a payload *that still has to
cross the context boundary*, fourteen times, while the bypass never lets it cross once. This is why
S5 sits early: everything after it would have been careful analysis of the wrong path.

### 10.3 Long research session, turn 40 — compaction deferred

**Situation.** Utilization 0.78, so the `COMPACT` band. Warm prefix of 62,000 tokens with the last
breakpoint after the memory layer. 44,000 tokens sit *before* the point a conversation summary would
edit. `expected_remaining_turns = 6`. Candidate compaction would remove 18,000 tokens.

**The naive move — summarize the conversation:**

```text
gain = 18,000 x $3/M x 6 turns                      = $0.324
loss = 44,000 x ($3/M x 0.90) x 6 turns             = $0.713
summarization call                                   = $0.020
compaction_benefit = 0.324 - 0.713 - 0.020          = -$0.409
```

Negative. `DEFER`.

**What the engine does instead (§5.1, tail first).** Two tool results totalling 21,000 tokens sit
*after* the last breakpoint. Externalizing them to the archive with references and a drop manifest
frees more tokens than the summary would have, and invalidates nothing:

```text
gain = 21,000 x $3/M x 6 turns                      = $0.378
loss = 0                                             (nothing cached was touched)
externalization cost (archive write, refs)           = $0.001
net                                                  = +$0.377
```

Utilization falls to 0.62, below the `COMPACT` band's exit threshold of 0.60... and here the
hysteresis matters: 0.62 is still above the exit, so the band does not immediately flip back and
forth. The pending head compaction is queued for the next task transition, where the prefix will be
rewritten once, for free.

**The point.** Same relief, opposite sign on the ledger, decided by *position relative to the cache
breakpoint* rather than by size or score. This is the single decision in the system where intuition
most reliably points the wrong way, and it is why cache state is an input to the control plane
(ADR-6) rather than a billing detail.

---

## 11. Telemetry emitted per decision

Every plan produces one structured decision event. This is the record C22 attributes savings against
and C23 measures bias against.

```json
{
  "turn": "t_40",
  "mode": "OPTIMIZED",
  "workload": "RESEARCH",
  "risk": "MEDIUM",
  "fast_path_eligible": false,
  "fast_path_blocked_by": ["context_utilization", "estimated_baseline_usd"],
  "overhead": { "usd": 0.0031, "cap_usd": 0.0180, "advisors_run": ["C2","C3","C4","C9"],
                "advisors_skipped": ["C7","C15"], "skip_reason": "ratio<=1" },
  "stages": [
    { "s": "S1", "exit": null, "confidence": 0.31 },
    { "s": "S3", "exit": null, "cache": "miss" },
    { "s": "S9", "model": "tier4", "reasoning": "MEDIUM", "tokenizer_recomputed": false },
    { "s": "S11", "band": "COMPACT", "action": "DEFER",
      "compaction_benefit_usd": -0.409, "alternative": "EXTERNALIZE_TAIL",
      "alternative_net_usd": 0.377, "tokens_freed": 21000, "cache_tokens_invalidated": 0 }
  ],
  "estimated": { "baseline_usd": 0.94, "optimized_usd": 0.52, "savings_usd": 0.42,
                 "baseline_method": "ESTIMATED" },
  "guards": { "max_tool_calls": 12, "max_retries": 2, "max_subagents": 0 }
}
```

`fast_path_blocked_by` is included deliberately: the distribution of *why* turns fail the fast-path
test is the clearest signal available about whether the fast-path thresholds are tuned correctly, and
it costs nothing to record.

---

## 12. Engine failure and demotion

| Failure | Detection | Behaviour |
|---|---|---|
| An advisor throws or times out | `guard()` deadline | Neutral fragment; the plan is built without it; recorded in the trace |
| Overhead cap reached mid-schedule | `OverheadMeter` | Stop scheduling; emit the plan built so far |
| `validate()` fails | Assertion | Discard the plan; emit `mode: DEMOTED` pass-through |
| Classifier unavailable | `guard()` | Neutral task class; INV-9 makes every downstream choice conservative |
| Cost model unavailable | `confidence: 0` | Every comparison ties; INV-9 resolves conservatively throughout |
| Engine itself throws | Orchestrator catch | `mode: DEMOTED` pass-through |

In every row the turn still executes and still produces a correct answer. The only thing lost is the
saving — which is exactly the property INV-2 exists to guarantee, and the reason this system can be
deployed in front of a production agent without becoming a new source of outages.
