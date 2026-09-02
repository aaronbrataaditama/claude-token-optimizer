# Deliverable 6 — Model and Reasoning Router

**Stage 4 of 5.** Production pseudocode for model selection, reasoning effort, cost and quality
estimation, escalation and the delegation gate (C6, and the entry point to C7).

Companion documents: [`architecture.md`](architecture.md) (C6, C7) ·
[`decision-engine.md`](decision-engine.md) (§17 S9) · [`tool-router.md`](tool-router.md) (D5) ·
[`subagent-manager.md`](subagent-manager.md) (D7) · [`configuration.md`](configuration.md) (D9).

---

## 1. The thing that makes model routing harder than it looks

The obvious model of routing is: cheaper model, fewer dollars. That model is wrong in a cached
session, for the same structural reason the compaction trap is:

> **Switching model family abandons a warm prefix.** The cache is keyed to the model. Moving a turn
> from a Tier-4 model with 48,000 cached tokens to a Tier-3 model at a third of the price means
> re-billing all 48,000 tokens at the cheaper model's *full* rate instead of the expensive model's
> *discounted* rate.

```text
stay:   48,000 × $3/M × 0.10  +  new_tokens × $3/M     = $0.0144 + …
switch: 48,000 × $1/M × 1.00  +  new_tokens × $1/M     = $0.0480 + …
```

A model three times cheaper costs three times more on that turn. The break-even depends on how much
of the turn is cached prefix versus new content, and `selectModel` computes it rather than assuming
either direction.

Two consequences run through everything below:

* **Reasoning effort is the cheaper lever, and not only on price.** Raising effort keeps the same
  model, so the prefix stays warm. Escalating tier goes cold. This is why the failure ladder puts
  "raise reasoning" *before* "escalate model" — it is not a price ordering, it is a cache ordering.
* **Routing pays off at boundaries.** Session start, task transition and cold-cache turns are where
  a model switch is nearly free. Mid-task switching needs to clear a real bar.

---

## 2. Tiers and bindings

```yaml
1: deterministic     # code and rules. No model. Always tried first.
2: tiny              # classification, routing, extraction, dedupe detection
3: small             # simple summarization, straightforward generation
4: large             # planning, ambiguity, high-risk actions
```

Tiers are **roles**, bound to model ids per deployment. Routing rules are written against tiers, so a
provider swap changes `models.bindings` and nothing else. A rule written against a model id is a rule
that silently becomes wrong on the next provider update.

---

## 3. `selectModel`

```text
function selectModel(task, step, budget, ctx) -> ModelSpec:

  # --- Tier 1: no model at all ---------------------------------------------
  if cfg.models.routing.prefer_deterministic and hasDeterministicSolution(step):
      return ModelSpec.deterministic()                    # the cheapest routing outcome there is

  op    <- operationClassOf(step)
  floor <- cfg.models.routing.quality_floor_by_risk[task.risk]
  want  <- cfg.models.routing.by_operation_class[op] ?? cfg.models.routing.default_tier

  # --- Candidates: the wanted tier and everything above it -----------------
  cands <- [ tierSpec(t) for t in tiers where t >= want ]
  cands <- [ m for m in cands if estimateQuality(m, task, op).lo >= floor ]
  cands <- [ m for m in cands if fitsWindow(m, ctx.projection, budget) ]     # §7
  if cands.isEmpty():
      return escalateUntilFits(task, step, budget, ctx)    # window pressure, not quality

  # --- Price each candidate INCLUDING the cache consequence ----------------
  for m in cands:
      m.total <- estimateModelCost(m, ctx.projection, ctx)  # §5, carries switching cost

  best <- argmin(cands, m => m.total.mid.usd_total)
  cur  <- ctx.current_model

  # --- The switch gate ------------------------------------------------------
  if best.id != cur.id and cfg.models.routing.switching_cost_aware:
      stay <- estimateModelCost(cur, ctx.projection, ctx)
      if stay.hi.usd_total <= best.lo.usd_total * cfg.models.routing.switch_margin:
          trace(MODEL_ROUTE, chosen = cur.id, reason = "switch does not clear the margin")
          best <- cur

  spec <- ModelSpec {
      model: best.id, tier: best.tier,
      reasoning: selectReasoningEffort(task, step, best),   # §4
      max_tokens: cfg.budgets.max_tokens_by_operation_class[op],
      stop: stopSequencesFor(op),
      structured_output: schemaFor(step),
      stream_early_exit: earlyExitPredicateFor(op),
      tokenizer: best.tokenizer
  }

  if spec.tokenizer != budget.family:
      budget <- estimator.recomputeFor(budget, spec.tokenizer)      # INV-8, mandatory (V5)
      assertWithinByteBound(ctx.projection, budget)

  # --- The router must pay for itself --------------------------------------
  if cfg.models.routing.router_must_pay_for_itself:
      saved <- estimateModelCost(tierSpec(cfg.models.routing.default_tier), ctx.projection, ctx)
               .mid.usd_total - spec_cost(spec)
      ledger.record(ROUTER_ATTRIBUTION, { saved, overhead: ctx.meter.spentOn(MODEL_ROUTER) })

  return spec
```

**`switch_margin` makes the switch gate asymmetric on purpose.** Staying is compared on its
*pessimistic* bound against the alternative's *optimistic* bound, and must still clear a margin. The
default is 1.15: a switch must look at least 15% cheaper before it is taken. Model switching has costs
this arithmetic does not capture — quality variance across families, tokenizer recomputation,
differing structured-output fidelity — and the margin is where that unmodelled risk is priced.

**Quality is a floor, not a term in the objective.** Candidates below the risk-appropriate quality
floor are removed *before* cost is considered. Trading a percentage point of task success for a
percentage of cost is exactly what the acceptance criteria forbid, and expressing quality as a hard
filter rather than a weighted term makes that structural.

**Config:** `models.routing.*`, `models.bindings`, `budgets.max_tokens_by_operation_class`.
**Demotion:** the configured default tier at `reasoning: MEDIUM`. Expensive, always capable.

---

## 4. `selectReasoningEffort`

```text
function selectReasoningEffort(task, step, model) -> Reasoning:

  if not model.supports_reasoning: return NONE

  # Sub-agents never get extended thinking. Mechanical work does not need it,
  # and a worker that thinks is a worker whose delegation stopped paying.
  if ctx.is_subagent and cfg.models.reasoning.forbid_for_subagents: return NONE

  base <- cfg.models.reasoning.by_operation_class[operationClassOf(step)]
          ?? cfg.models.reasoning.default

  # Risk raises effort; it never lowers it.
  if task.risk == HIGH:   base <- max(base, HIGH)
  if task.risk == MEDIUM: base <- max(base, MEDIUM)

  # Ambiguity is the other legitimate reason to think harder.
  if task.classification_confidence < cfg.models.routing.escalate_on_low_confidence_below:
      base <- raise(base, 1)

  # A retry that has already escalated stays escalated for this step.
  if step.escalated_reasoning: base <- max(base, step.escalated_reasoning)

  budget_tokens <- cfg.models.reasoning.thinking_tokens[base]
  return Reasoning { tier: base, thinking_tokens: budget_tokens }
```

**Effort is per step, not per turn.** A turn whose final answer needs Tier 4 with `HIGH` reasoning can
still contain a classification step that runs Tier 2 with `NONE`. Setting one effort level for a whole
turn pays planning-grade thinking for every mechanical sub-step in it, and thinking tokens are billed
as output — the most expensive tokens in the system.

**`NONE` is the right answer more often than it feels.** Classification, extraction, field filtering,
formatting and routing are the bulk of an agentic turn's model calls by count, and none of them
benefit from deliberation. The configuration lists them explicitly rather than leaving it to judgement.

**Thinking tokens are tracked as their own ledger line.** They are invisible in the response and
billed as output; a system that folds them into `llm_output` cannot tell an expensive answer from an
expensive deliberation, and will tune the wrong thing.

---

## 5. `estimateModelCost`

```text
function estimateModelCost(model, projection, ctx) -> Estimate<TokenCost>:

  # --- How much of this projection is already cached, for THIS model? ------
  warm <- (ctx.cache.model_id == model.id) ? ctx.cache.prefix_tokens : 0
  cold <- projection.total.tokens - warm

  p <- prices(model)                                       # per-million, from the binding
  caps <- providerCapabilities(model)

  input_cost <- cold * p.input
              + warm * p.input * (1 - caps.cached_input_discount)

  # A cold prefix on a new model must also be WRITTEN to that model's cache.
  write_cost <- (warm == 0 and caps.prompt_cache_supported
                 and projection.total.tokens >= caps.min_prefix_tokens)
                ? projection.cacheable_tokens * p.cache_write : 0

  out_tokens   <- expectedOutputTokens(ctx.step, model)     # from the ledger, per operation class
  think_tokens <- model.reasoning.thinking_tokens * thinkingUtilization(ctx.step)

  return Estimate {
      llm_input: projection.total.tokens,
      llm_output: out_tokens,
      llm_thinking: think_tokens,
      cache_read: warm, cache_write: (write_cost > 0 ? projection.cacheable_tokens : 0),
      usd_llm: input_cost + write_cost
             + out_tokens * p.output + think_tokens * p.output,   # thinking bills as output
      usd_non_token: 0,
      latency_p50_ms: model.latency.p50 + thinkingLatency(think_tokens, model),
      latency_p95_ms: model.latency.p95 + thinkingLatency(think_tokens, model)
  }
```

**`warm` is conditioned on the model id.** This single line is what makes switching costs visible.
A cost model that computes cached tokens without checking which model they are cached against will
report a switch as free and be wrong by the size of the prefix.

**`write_cost` is the term that punishes churn twice.** Moving to a new model pays full price for the
input *and* pays to populate the new model's cache — a cost that only returns value if the session
stays on that model for several more turns. On a one-off step it is pure loss, which is why a
lightweight classification is better run as a sub-request that does not disturb the primary's cache
than as a "cheap" reroute of the main turn.

**`thinkingUtilization`** is the observed fraction of the thinking budget actually consumed for this
operation class, from the ledger. Budgeting 16,384 thinking tokens and habitually using 2,000 makes
every routing comparison wrong in the same direction, which is exactly the silent bias C23 exists to
catch.

---

## 6. `estimateQuality`

```text
function estimateQuality(model, task, op) -> Estimate<number>:

  # Measured first, always. A hand-written capability table is a guess that
  # never updates and quietly encodes last year's model lineup.
  obs <- gates.successRate(model.id, task.workload, op)
  if obs.n >= cfg.models.routing.min_quality_samples:
      return wilsonInterval(obs.successes, obs.n, cfg.gates.acceptance.confidence_level)

  # Not enough observations: fall back to the tier prior, widened by how far
  # this case sits from anything measured.
  prior <- cfg.models.routing.tier_quality_prior[model.tier]
  width <- cfg.models.routing.unmeasured_quality_width
         * (1 + distanceFromNearestMeasured(model, task.workload, op))

  return Estimate { lo: prior - width, mid: prior, hi: min(1.0, prior + width),
                    confidence: 0.3, basis: PRIOR }
```

**A Wilson interval, not a point estimate.** Nineteen successes out of twenty is not 0.95; it is
roughly 0.75–0.99 at 95% confidence, and a quality floor of 0.90 should reject it. Point estimates on
small samples are how an under-powered model gets promoted on a lucky run.

**Unmeasured combinations widen rather than default.** A model that has never been measured on
long-document work does not inherit its Q&A score at full confidence. The widening pushes `lo` below
the floor for high-risk tasks, so unmeasured models are not selected for risky work — they earn their
way in through shadow runs (C24), which is the same discipline the advisor scheduler uses.

---

## 7. `fitsWindow` and `escalateUntilFits`

```text
function fitsWindow(model, projection, budget) -> bool:
  b <- (model.tokenizer == budget.family) ? budget
                                          : estimator.recomputeFor(budget, model.tokenizer)
  return projection.totalIn(model.tokenizer).tokens + b.reserved_output
         <= model.window - b.safety_margin
     and projection.total.bytes <= model.window * cfg.estimation.bytes_per_token_fallback  # backstop


function escalateUntilFits(task, step, budget, ctx) -> ModelSpec:
  # Window pressure, not quality pressure. Try in ascending cost.
  for m in tiersByWindowAscendingCost():
      if fitsWindow(m, ctx.projection, budget): return finalize(m, task, step, budget, ctx)

  # Nothing fits. This is a context problem, not a model problem — hand it back
  # to S10/S11 with a tighter allocation rather than truncating silently.
  raise ContextDoesNotFit { needed: ctx.projection.total.tokens,
                            largest_window: maxWindow(), suggest: TIGHTEN_AND_RETRY }
```

**Recomputation happens inside the fit test, not after selection.** Choosing a model and then
discovering the budget was computed for a different tokenizer is a bug that surfaces as an overflow at
generation time — the worst possible moment. INV-8 is enforced where the decision is made.

**The byte backstop catches a missing adapter.** If the target family has no tokenizer adapter, the
token count is an extrapolation; the byte comparison is crude and always available, and it is what
prevents an overflow on a model the deployment has not been calibrated for.

---

## 8. `shouldEscalate`

```text
function shouldEscalate(failure, current, step, ctx) -> Escalation:

  if step.escalations >= cfg.models.routing.max_escalation_steps:
      return Escalation { action: ABORT, reason: "escalation budget exhausted" }

  match classify(failure):

    DETERMINISTIC ->                                       # bad argument, schema violation
        return Escalation { action: FIX_WITHOUT_LLM, patch: derivePatch(failure) }

    TRANSIENT ->
        return Escalation { action: RETRY, after: backoff(step.attempts) }

    CAPABILITY ->
        # Rung 2 before rung 3: same model, more thinking. Keeps the prefix warm.
        if cfg.models.reasoning.escalate_before_model_tier
           and current.reasoning.tier < HIGH
           and current.supports_reasoning:
            return Escalation { action: RAISE_REASONING,
                                to: raise(current.reasoning.tier, 1),
                                cost: deltaThinkingCost(current, ctx),
                                cache: PRESERVED }

        next <- nextTierUp(current)
        if next == null: return Escalation { action: FALLBACK_TOOL }

        return Escalation { action: ESCALATE_MODEL, to: next,
                            cost: estimateModelCost(next, ctx.projection, ctx)
                                  - estimateModelCost(current, ctx.projection, ctx),
                            cache: LOST }                  # priced, not hidden

    FATAL ->
        return Escalation { action: ABORT, reason: normalize(failure) }
```

**`cache: PRESERVED` versus `cache: LOST` is the field that makes the ladder ordering explicit in the
data, not just in the code path.** Raising reasoning is cheap partly because it is free of cache
consequences; escalating tier is expensive partly because it is not. Recording which one happened lets
the ledger explain a cost spike as "three tier escalations" rather than as an unattributed increase.

**Nothing here retries an identical call.** `RETRY` is reachable only from `TRANSIENT`, where the
input is unchanged but the world is expected to have changed. Every other path alters something — the
arguments, the effort, the model, or the tool.

---

## 9. `shouldDelegate`

The gate in front of Deliverable 7. It answers *whether*; D7 answers *how*.

```text
function shouldDelegate(task, step, ctx) -> DelegationDecision:

  if not cfg.delegation.enabled:               return { k: NATIVE }
  if ctx.depth >= cfg.delegation.max_depth:    return { k: NATIVE, reason: DEPTH }
  if ctx.subagents_this_turn >= cfg.delegation.max_subagents_per_turn:
                                               return { k: NATIVE, reason: TURN_CAP }
  if ledger.spawnSpendThisTurn() >= cfg.delegation.spawn_budget_usd_per_turn:
                                               return { k: NATIVE, reason: SPAWN_BUDGET }

  native <- estimateNativeCost(step, ctx)

  # --- The full cost of delegating, including the parts usually forgotten --
  transfer <- prepareMinimalContextSize(step, ctx) * inputPrice(subModel(step))
  cold     <- coldPrefixPenalty(step, ctx)      # the sub-agent starts with no cache
  routing  <- ctx.meter.estimateFor(DELEGATION_PLANNER)
  work     <- estimateModelCost(subModel(step), subProjection(step, ctx), ctx)
  merge    <- resultIntegrationCost(step)       # validate, compress, reduce, commit
  sub      <- transfer + cold + routing + work.usd_total + merge

  if sub.hi >= native.lo - cfg.delegation.min_expected_saving_usd:
      trace(DELEGATION, chosen = NATIVE, sub, native)
      return { k: NATIVE }

  # --- Which shape of delegation? ------------------------------------------
  if independentSubtasks(step) > 1 and cfg.parallelism.enabled:
      return { k: PARALLEL_SUBAGENTS, specs: splitInto(step), reducer: reducerSpec(ctx) }
  if isPureRouting(step):
      return { k: LIGHTWEIGHT_ROUTER, model: tierSpec(2) }
  return { k: SUBAGENT, spec: subAgentSpecFor(step, ctx) }
```

**`coldPrefixPenalty` is the term the specification calls out and most implementations omit.**
Delegating from a warm primary to a cold sub-agent changes the economics: the sub-agent pays full
price for context the primary was getting at a 90% discount. On a warm session this term alone
frequently makes delegation net-negative for anything short of substantial work.

**The comparison is `sub.hi >= native.lo`.** Delegation must be cheaper on the pessimistic reading of
itself against the optimistic reading of doing it yourself, *and* clear a minimum absolute saving.
Delegation adds failure modes, latency and coordination; it should not be taken for a rounding error.

**A dollar spawn budget, not just a count.** Four sub-agents each burning 30,000 tokens is not
bounded by "max 4 sub-agents". Counts bound concurrency; only a budget bounds cost.

---

## 10. Failure and demotion

| Function | Failure | Demotion | Cost |
|---|---|---|---|
| `selectModel` | Bindings or prices unavailable | Default tier, `MEDIUM` reasoning | Highest per-turn cost |
| `selectReasoningEffort` | Class map unavailable | `models.reasoning.default` | Thinking tokens on mechanical work |
| `estimateModelCost` | Cache state unavailable | Assume cold: `warm = 0` | Overstates cost; biases toward staying put |
| `estimateQuality` | Gate data unavailable | Tier prior at `confidence: 0.3` | Conservative tier selection |
| `fitsWindow` | Adapter missing | Byte backstop only | Over-reservation, no overflow |
| `shouldEscalate` | Classifier unavailable | Treat as `CAPABILITY`, raise reasoning first | One extra cheap attempt |
| `shouldDelegate` | Estimator unavailable | `NATIVE` | Latency on parallelizable work |

`estimateModelCost` demoting to `warm = 0` is deliberately biased. Assuming a cold cache overstates
the cost of *every* candidate including the incumbent, and since the switch gate compares against the
incumbent's pessimistic bound, the net effect is to make switching harder. Under uncertainty, staying
put is the conservative move.

---

## 11. Worked example — the cheaper model that costs more

Turn 22. Current model Tier 4 at $3/M in, $15/M out, 48,000-token warm prefix, 90% cache discount.
The step is a straightforward summarisation, which `by_operation_class` maps to Tier 3 ($1/M in,
$5/M out). New content this turn: 2,100 tokens. Expected output: 600.

```text
STAY on Tier 4
  cached input   48,000 × $3/M × 0.10                       = $0.0144
  new input       2,100 × $3/M                              = $0.0063
  output            600 × $15/M                             = $0.0090
                                                              ────────
                                                              $0.0297

SWITCH to Tier 3
  input (all cold) 50,100 × $1/M                            = $0.0501
  cache write      50,100 × $1/M × 0.25                     = $0.0125
  output              600 × $5/M                            = $0.0030
                                                              ────────
                                                              $0.0656
```

A model at a third of the price costs **2.2×** more on this turn. `switch_margin` never gets consulted;
the raw comparison already refuses.

The routing that *does* pay here is orthogonal to model choice:

```text
reasoning: MEDIUM → NONE on Tier 4
  thinking      ~1,800 × $15/M avoided                      = $0.0270 saved
final                                                         $0.0297 → $0.0117
```

**Same model, 61% cheaper turn.** The lever that worked was effort, not tier — and it worked precisely
because it left the cache alone. When the session later reaches a task transition and the prefix is
rebuilt anyway, the Tier-3 switch becomes free to make and is taken then.

---

## 12. Test hooks (for D11)

| Test | Asserts |
|---|---|
| Switching arithmetic | For warm prefixes above the break-even, a cheaper tier is not selected |
| Cache keying | `estimateModelCost` reports `warm = 0` whenever the cache model id differs |
| Quality floor | No model whose interval `lo` is below the risk floor is ever selected |
| Wilson bounds | 19/20 does not clear a 0.90 floor; 190/200 does |
| Per-step effort | A turn contains steps at differing reasoning tiers |
| Sub-agent effort | No sub-agent request carries a non-zero thinking budget |
| Thinking accounting | Thinking tokens appear on their own ledger line, never folded into output |
| Ladder ordering | `RAISE_REASONING` precedes `ESCALATE_MODEL` on every capability failure |
| No identical retry | `RETRY` is emitted only for transient classes |
| Tokenizer coherence | Every returned spec's budget family matches its tokenizer |
| Delegation asymmetry | Delegation is chosen only when `sub.hi < native.lo − min_saving` |
| Spawn budget | A turn cannot exceed the dollar spawn budget regardless of sub-agent count |
| Router attribution | Router overhead and attributed saving are both recorded every routed turn |
