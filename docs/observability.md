# Deliverable 10 — Observability

**Stage 5 of 5.** Metrics, logs, traces, token and cost accounting, decision records, example
telemetry events, dashboards and alerts.

Companion documents: [`economics.md`](economics.md) (D8) · [`test-strategy.md`](test-strategy.md)
(D11) · [`architecture.md`](architecture.md) (C22, C23, C24).

---

## 1. What this has to answer

Four questions, in priority order. Everything below exists to answer one of them.

1. **Is the optimizer saving money?** Against a stated baseline, with the method named.
2. **Is it costing correctness?** Task success by workload, with confidence intervals.
3. **Which mechanism produced which share of the saving?** Attribution, or the next roadmap decision
   is a guess.
4. **Are the estimates the decisions rest on still true?** Bias, or every decision drifts silently.

The fourth is the one that has no natural alarm. A biased estimator throws no exception and fails no
test; it just makes the wrong trade every turn, forever.

---

## 2. The turn ledger

One entry per turn, opened when the plan is emitted and closed after generation. This is the primary
record; metrics are aggregations of it and traces are its spans.

```json
{
  "event": "turn.accounting",
  "turn": "t_40", "session": "s_9c21", "task": "tk_3",
  "ts": "2026-09-02T04:12:33.481Z",
  "tenant": "acme", "workload": "research", "risk": "MEDIUM",
  "mode": "OPTIMIZED",

  "tokens": {
    "input_uncached": 8400, "input_cached": 44100, "cache_write": 0,
    "output": 612, "thinking": 1840,
    "context": 52500, "tool_schema": 2340, "tool_input": 180, "tool_output": 9600,
    "memory": 1200, "retrieval": 340, "subagent": 0, "retry": 0,
    "multimodal": 0, "wasted": 210, "saved_vs_baseline": 31200
  },

  "cost_usd": {
    "input": 0.0252, "cache_read": 0.0132, "cache_write": 0.0,
    "output": 0.0092, "thinking": 0.0276,
    "tool_api": 0.0, "database": 0.003, "code_execution": 0.0,
    "storage": 0.0001, "network": 0.0,
    "subagent": 0.0, "optimization_overhead": 0.0031,
    "total": 0.0814
  },

  "baseline": { "usd": 0.1620, "method": "ESTIMATED", "confidence": 0.7 },
  "savings":  { "usd": 0.0806, "ratio": 0.497 },

  "cache": {
    "prefix_warm": true, "prefix_tokens": 44100, "hit_ratio": 0.84,
    "ttl_remaining_s": 168, "breakpoints_used": 3,
    "invalidation_events": [
      { "cause": "tool_projection_change", "tokens": 0, "usd": 0.0 }
    ]
  },

  "model": { "id": "tier4", "tier": 4, "reasoning": "MEDIUM",
             "thinking_budget": 4096, "thinking_used": 1840,
             "tokenizer": "anthropic", "switched": false },

  "attribution": {
    "prefix_ordering": 0.0396, "reasoning_routing": 0.0210,
    "payload_reduction": 0.0181, "output_controls": 0.0019
  },

  "latency_ms": { "total": 4180, "optimizer": 62, "model": 3610, "tools": 508 },
  "flags": [], "demotions": []
}
```

**Six fields carry most of the diagnostic value.**

`baseline.method` is required on every savings claim. `ESTIMATED` is the per-turn counterfactual from
the ledger and is fine for trends; `MEASURED` comes only from a shadow or A/B run and is the only
thing a gate decision may use. Conflating them is how a project reports savings the invoice does not
show.

`thinking` is a separate line from `output`, though both bill at the output rate. Folded together,
the largest single output-side lever becomes invisible.

`saved_vs_baseline` and `wasted` are different quantities. Saved is what the optimizer avoided; wasted
is what it spent and did not use — a discarded speculative branch, a retry, a retrieval that answered
nothing. Optimizers that report only the first look better than they are.

`cache.invalidation_events` with a cost each. An unattributed cost increase is the hardest thing to
chase; naming the cause at the moment it happens makes it a lookup.

`attribution` splits the saving across mechanisms so the roadmap has evidence. It is estimated, not
measured, and labelled as part of an `ESTIMATED` baseline.

`demotions` lists components that fell back this turn. A cost increase with a named demotion is
explained; the same increase without one is a mystery.

---

## 3. Metrics

Derived from the ledger. Every one is reported **per workload class** — an aggregate hides the case
where an optimization helps analytics and hurts long-document work.

### Cost and savings

| Metric | Definition |
|---|---|
| `cost_per_turn_usd` | `cost_usd.total`, p50 / p95 |
| `savings_ratio` | `savings.usd ÷ baseline.usd` |
| `cost_saved_usd` | Sum over the window |
| `optimization_overhead_ratio` | `optimization_overhead ÷ baseline.usd` |
| `non_token_cost_share` | `(tool_api + database + code_execution + storage + network) ÷ total` |

### Token efficiency

| Metric | Definition |
|---|---|
| `context_reduction_ratio` | `1 − optimized_context ÷ baseline_context` |
| `tool_payload_reduction_ratio` | `1 − reduced ÷ raw`, per tool |
| `compression_ratio` | `compacted ÷ original`, where compaction ran |
| `thinking_share` | `thinking ÷ (output + thinking)` |
| `wasted_token_ratio` | `wasted ÷ input` |
| `tokens_per_successful_task` | Total tokens ÷ tasks completed successfully |

### Cache

| Metric | Definition |
|---|---|
| `cache_hit_rate` | `input_cached ÷ (input_cached + input_uncached)` |
| `schema_cache_hit_rate` | Schema renders served from cache |
| `cache_invalidation_rate` | Events per 100 turns |
| `cache_invalidation_cost_usd` | Sum of event costs |
| `prefix_stability` | Turns since last prefix edit, p50 |

### Decisions

| Metric | Definition |
|---|---|
| `fast_path_rate` | Share of turns with `mode = NO_OPTIMIZATION` |
| `fast_path_blocked_by` | Histogram over blocking conditions |
| `advisor_run_rate` | Per advisor, share of turns |
| `advisor_roi` | Realized saving ÷ metered overhead, per advisor |
| `tool_call_reduction` | vs baseline calls per task |
| `bypass_rate` / `bypass_savings_usd` | Code-execution bypass usage and effect |
| `compaction_deferral_rate` | Share of compaction evaluations returning `DEFER` |
| `subagent_roi` | Realized saving ÷ delegation cost |
| `demotion_rate` | Per component |

### Quality and reliability

| Metric | Definition |
|---|---|
| `task_success_rate` | With a Wilson interval, per workload |
| `manifest_retrieval_rate` | Turns where the agent fetched dropped data back |
| `retry_rate` / `escalation_rate` | Per class and per rung |
| `loop_detection_rate` | Loops caught per 1,000 turns |
| `disputed_fact_rate` | Reducer conflicts surfaced unresolved |
| `emergency_band_rate` | Emergency compactions per 100 turns |

**`manifest_retrieval_rate` is the correctness canary for the whole reduction pipeline.** It measures
how often reduction removed something that was actually needed. Rising means budgets are too tight or
field relevance is miscalibrated — and unlike a success-rate drop, it moves immediately and is cheap
to attribute.

**`tokens_per_successful_task`, not per turn.** An optimization that cuts per-turn tokens by 30% and
causes a retry on one turn in four has increased the cost of getting work done. Per-turn metrics
cannot see that; this one can.

---

## 4. Decision records

One per turn, alongside the ledger entry. The schema is in
[`decision-engine.md`](decision-engine.md) §11. It answers "why did this turn cost that?" and is what
makes a plan replayable.

Retained at full fidelity for `telemetry.decision_trace_retention_days`, then sampled. The trace is
the largest telemetry object and the least often read — but when it is read, nothing else substitutes.

---

## 5. Traces

One span tree per turn. Span names match component ids so a flame graph reads as the architecture.

```text
turn t_40                                                      4,180 ms
├── C0.admit                                                        3
├── C1.classify                                                    18
├── C9.baseline                                                     4
├── C8.decide                                                      62   ← optimizer total
│   ├── advisor:C2.estimate                                         9
│   ├── advisor:C3.budget                                           2
│   ├── advisor:C4.score                                           31
│   └── advisor:C5.project_tools                                   14
├── C10.project                                                     41
├── C11.execute                                                    508
│   └── tool:docs.search                                           496
│       ├── upstream_params_bound                                    -
│       └── C12.reduce                                              38
├── C13.reduce_commit                                               11
├── model.generate                                                3,610
│   └── thinking                                                  1,240
└── C22.account                                                      6
```

**The optimizer's span is its latency budget made visible.** 62 ms against a 4,180 ms turn is 1.5%,
against a p95 constraint of +10%. If this span grows past a few percent, the advisor scheduler is
funding work that is not paying for itself, and `advisor_roi` will say which one.

Span attributes carry `tokens_in`, `tokens_out`, `usd`, `cache_hit`, `demoted` — so a trace answers
cost questions without a join against the ledger.

---

## 6. Alerts

Ordered by how badly you want to know.

| Alert | Condition | Why |
|---|---|---|
| **Estimator bias** | `abs(bias) > 0.20` over 500 turns, any estimator | The silent failure. No other alarm fires. |
| **Bias monitor liveness** | No bias sample in 1 hour | The demotion nothing downstream notices |
| **Savings regression** | `savings_ratio` down >10 points week over week, any workload | The point of the system |
| **Overhead ratio** | `optimization_overhead_ratio > 0.10` sustained | The optimizer stopped paying |
| **Manifest retrieval** | `manifest_retrieval_rate > 0.05` | Reduction is removing needed data |
| **Success drop** | Wilson lower bound below the gate floor, any workload | The constraint that outranks cost |
| **Cache collapse** | `cache_hit_rate` down >20 points | Usually one prefix edit, usually recent |
| **Emergency band** | `> 2` per 100 turns | Budget allocation is wrong, not the compaction policy |
| **Demotion** | Any component `> 1%` of turns | Fail-open working, and something is broken |
| **Component disabled** | Breaker opened | Demotion has become permanent |
| **Loop caught** | Any | Cheap to page on; expensive to miss |
| **Cross-tenant key** | Any cache key without a scope component | Should be impossible; alarm if not |
| **Spawn budget** | Turn or session budget exhausted | Delegation is running away |
| **p95 latency** | `> +10%` vs baseline, any workload | The other constraint |

Only the first four are worth waking someone for. The rest are dashboard-and-ticket — the system is
fail-open, so most of these describe a system that is *working correctly and expensively*.

---

## 7. Dashboards

**Executive — one row per workload.** Cost per turn, savings ratio with its baseline method, task
success with its interval, p95 latency, and monthly spend against forecast. Nothing else. If the
savings ratio and the success interval are both green, no further detail is needed.

**Attribution — where the money comes from.** A stacked series of `attribution` over time, plus the
`advisor_roi` table. Answers "what should we build next?" and "what should we turn off?". This is the
dashboard the roadmap is revised from.

**Cache health.** Hit rate, prefix stability, invalidation events with causes and costs, compaction
deferral rate. The single most predictive panel for cost regressions, because prefix ordering is 60%
of the saving (`economics.md` §4) and one careless edit removes it.

**Correctness.** Manifest retrieval rate, disputed facts, summary validation rejections, retries by
class, escalations by rung, loops caught. Everything here should be flat and near zero; movement is
the signal.

**Decision behaviour.** Fast-path rate and `fast_path_blocked_by` histogram, advisor run rates,
compaction actions, bypass rate, demotion rates. Tuning surface: `fast_path_blocked_by` is the clearest
evidence available about whether the fast-path thresholds are set correctly, and it costs nothing to
record.

---

## 8. Cardinality and cost

Telemetry is not free, and an observability stack that costs a meaningful fraction of what it measures
is its own anti-pattern.

| Signal | Volume | Retention | Control |
|---|---|---|---|
| Ledger entry | 1 per turn, ~2 KB | 90 days | Always full; this is the record |
| Decision record | 1 per turn, ~4 KB | 14 days full, then 1-in-20 | Sampled after the window |
| Trace | 1 per turn, ~12 spans | 7 days, 1-in-10 sampled | Head sampling; always keep demoted and failed turns |
| Metrics | Pre-aggregated | 13 months | Label set is fixed |

**Fixed label set:** `workload`, `tenant_class`, `model_tier`, `mode`, `component`. Never `tenant_id`,
`session_id`, `tool_id` as a metric label — those live in the ledger, which is queryable, rather than
in a time series, which is not. Unbounded label cardinality is how a metrics bill overtakes an
inference bill.

**Always retain, never sample:** turns that demoted, failed, hit a guard, triggered an alarm, or ran
in shadow. Those are the ones anyone will want, and they are rare.

---

## 9. Attribution: how savings are actually computed

The mechanism behind the `attribution` block, because it is the field most likely to be believed
uncritically.

```text
per turn:
  baseline_est <- cost of this same turn on the pass-through path
                  (full projection, all candidate schemas, default tier + MEDIUM reasoning,
                   no reduction, cache hit rate = the session's observed accidental rate)

  for each enabled mechanism m:
      attribution[m] <- baseline_est(without m) − baseline_est(with m)

  normalize so Σ attribution = savings.usd
```

Three honest caveats, stated wherever the number is shown:

* **It is estimated, not measured.** Mechanisms interact — prefix ordering and compaction both act on
  the same cached tokens — so the marginal attributions do not decompose cleanly. Normalization hides
  that rather than solving it.
* **It is only valid against the recorded baseline assumptions.** The accidental cache hit rate in
  particular is a strong assumption doing a lot of work.
* **Gate decisions use measured A/B, never this.** Attribution steers the roadmap; it never approves
  a flag. `gates.rollout.shadow_before_cutover` and a measured baseline are what promote an
  optimization.

---

## 10. Failure of the observability plane itself

| Component | Failure | Behaviour |
|---|---|---|
| Ledger | Write unavailable | Reduced-fidelity local log; execution unaffected; attribution gap recorded |
| Bias monitor | Stops sampling | Estimates continue unvalidated — liveness alarm is the only protection |
| Gate runner | Cannot run | **Fails closed on change**: blocks promotion of new flags; runtime unaffected |
| Trace exporter | Unavailable | Dropped; ledger retains cost data |
| Metrics pipeline | Unavailable | Dashboards stale; alerts on staleness itself |

Two deliberate asymmetries. The gate runner is the only observability component that **blocks**
anything, and it blocks change rather than traffic. And the bias monitor is the acknowledged weak
point of the whole architecture: its failure is invisible downstream, which is why its own liveness
alarm is part of the operational contract rather than a nice-to-have.
