# §24 — Mechanism inventory and right-sizing

**Stage 5 of 5.** One row per optimization mechanism in the specification, scored and given a verdict.
The verdicts populate the phases in [`roadmap.md`](roadmap.md); nothing enters a phase without one,
and `OMIT` mechanisms appear nowhere in the implementation plan.

Companion documents: [`economics.md`](economics.md) (D8, the reference deployment and attribution) ·
[`anti-patterns.md`](anti-patterns.md) (§24) · [`roadmap.md`](roadmap.md) (D12) ·
**[`phase0-findings.md`](phase0-findings.md) (measured data)**.

> ### Revised against measurement
>
> The verdicts below were originally derived from the *modelled* reference deployment in
> `economics.md` §3. A Phase 0 measurement over a real multi-project corpus
> ([`phase0-findings.md`](phase0-findings.md)) contradicted three of its load-bearing assumptions.
> Rows changed by that evidence are marked **[M]** and carry both verdicts, so the reasoning that
> produced the original is still legible. **[M2]** marks a row where a *later* measurement then
> overturned the revision as well — see 17b.
>
> | Assumed | Measured | Rows affected |
> |---|---|---|
> | 35% baseline cache hit rate | **95.4%** — the harness already does this | 8, 9, 10 |
> | Cache writes are a switching tax | **39% of cost**, structural | new: 17b |
> | Sub-agents start cold, so delegation loses | **93.3% hit rate**, already 42% of spend | 64, 65, 66 |
> | Reasoning routing worth 21% | **~2.4%** of total cost here | 56 |
>
> A sixth verdict was added, because "build it" and "don't build it" were both wrong for the first
> group:
>
> ```text
> SATISFIED     the platform already provides this. Verify and monitor; do not build.
> ```
>
> The original scores are not deleted. A different deployment — one on a raw API integration rather
> than a mature agent harness — would land back on them.

---

## 1. How to read this

**This specification is deliberately maximal. A production implementation must not be.** The
specification enumerates roughly fifty mechanisms; this deployment builds twenty-nine, defers seven
and omits twelve. An omission with a reason is a design decision; an unstated omission is a gap, so
every omission below carries its reason.

**Verdicts are per-deployment.** They are made against the reference deployment defined in
[`economics.md`](economics.md) §3: 50,000 turns/month, mixed conversational / analytics / coding, warm
sessions, single provider with prompt caching. A mechanism that is `Core` here may be `OMIT` for a
one-shot API integration, and the per-workload column records where the verdict flips.

**Scoring.**

| Column | Meaning |
|---|---|
| Saving | Expected cost reduction on the reference deployment, from `economics.md` §6 |
| Cx | Implementation complexity — L / M / H |
| Lat | Latency impact — `−` reduces, `0` neutral, `+` adds |
| Risk | Correctness risk if it misbehaves — L / M / H |
| Ovh | Optimizer overhead it introduces — L / M / H |

**Verdicts.**

```text
CORE          Phase 1 · high-certainty savings, low complexity and risk
ADVANCED      Phase 2–3 · real savings, meaningful complexity; needs the core first
EXPERIMENTAL  Phase 4 · economics can go negative; flag-isolated, shadow-proven
DEFER         worthwhile only above a stated scale or workload threshold
OMIT          cost, complexity or risk exceeds value for this deployment
```

---

## 2. Measurement and accounting (§18, §20)

Phase 0. Not optimizations — the instruments that make every later verdict evidence rather than
assumption.

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Per-category token accounting (ledger) | 0% | M | 0 | L | L | **CORE (Phase 0)** |
| 2 | Baseline counterfactual, estimated per turn | 0% | M | 0 | L | L | **CORE (Phase 0)** |
| 3 | Tokenizer adapters + byte bound | 0% | L | 0 | H if absent | L | **CORE (Phase 0)** |
| 4 | Estimator bias monitor | 0% | L | 0 | L | L | **CORE (Phase 0)** |
| 5 | Baseline profiling over a task corpus | 0% | L | 0 | L | — | **CORE (Phase 0)** |
| 6 | Gate runner, shadow and A/B | 0% | M | 0 | L | — | **CORE (Phase 0)** |
| 7 | Measured baselines for gate decisions | 0% | M | 0 | L | — | **CORE (Phase 0)** |

**Why all seven are Phase 0 despite saving nothing.** Scenario A and Scenario B in `economics.md`
differ by a factor of four and are indistinguishable without a measured baseline. Building
optimizations before knowing which scenario you are in is how a team spends a quarter on context
scoring in a deployment whose entire problem was an unstable prefix.

Item 4 is the least obvious and the one most often skipped: every decision in this architecture rests
on an estimate, and a persistently biased estimator produces confidently wrong decisions **with no
visible failure**. Nothing else in the system detects it.

---

## 3. Cache and prefix engineering (§9)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 8 **[M]** | Stable-prefix layer ordering | ~~30–55%~~ **≈0%** | **L** | 0 | L | **none** | ~~CORE (Phase 1)~~ → **SATISFIED** |
| 9 **[M]** | Append-only history discipline | ~~3–15%~~ **≈0%** | L | 0 | L | none | ~~CORE (Phase 1)~~ → **SATISFIED** |
| 10 **[M]** | Cache breakpoints at stability boundaries | ~~2–6%~~ **≈0%** | L | 0 | L | none | ~~CORE (Phase 1)~~ → **SATISFIED** |
| 17b **[M2]** | Cache-write TTL selection (5m vs 1h) | ~~+7.1%~~ **−16% on writes** | L | 0 | L | none | ~~CORE (Phase 1)~~ → **OMIT, measured** |
| 11 | Cache-invalidation cost in compaction | 5–20% | M | 0 | L | L | **CORE (Phase 2)** |
| 12 | Compaction batching at boundaries | 2–5% | L | 0 | L | L | **CORE (Phase 2)** |
| 13 | Schema cache | 1–3% | L | − | L | L | **CORE (Phase 1)** |
| 14 | Result cache with idempotency keys | 2–12% | M | − | M | L | **ADVANCED (Phase 2)** |
| 15 | Retrieval cache | 1–3% | L | − | L | L | **ADVANCED (Phase 2)** |
| 16 | Provider capability probe and adapters | enabling | M | 0 | M | L | **CORE (Phase 1)** |
| 17 | Semantic cache | −1 to +2% | H | − | **H** | M | **OMIT** |

**Mechanism 8 was the single highest-value item in this inventory — and the measurement retired it.**
It carried 60% of the modelled savings on the assumption of a 35% accidental baseline hit rate. The
measured rate is 95.4%: the harness already orders the prefix and appends to it. There is nothing
left to win, and the correct action is a monitor on `cache_hit_rate` that alarms if it degrades, not
an implementation.

This is the most useful thing Phase 0 produced. Building mechanism 8 first — as the roadmap
originally said — would have been weeks of work against a lever that was already pulled, and the
attribution table would have credited it with savings it did not create.

**17b is new, and it exists because the measurement found a cost category the design did not model.**
Cache writes are 39% of measured cost, split 188M tokens at the 5-minute rate (1.25×) and 109M at
the 1-hour rate (2.0×). Writes are *not* wasteful here — each written token is read back 21.2 times,
which is a healthy ratio — but the TTL choice is a free 7.1%:

```text
1h writes re-priced at the 5m rate:  write_tokens × price × (2.00 − 1.25)  ≈ 7% of the bill
```

**[M2] The measurement was taken, and 17b is wrong.** The gate above said *"a measurement to take
before it is a change to make"*. `tools/ttl_analysis.py` took it:

```text
gap between consecutive turns      p50 5s   p90 46s   p99 1,829s
  under 1 min                          91.5%
  1-5 min                               5.3%
  5-60 min  (the only window 1h pays)   2.5%
  over 1 hour                           0.7%

relative to the 1-hour write bill (= 100):
  all writes at 5m instead                            62.5
  + rebuilding the prefixes a 5m entry would lose     53.4
                                                    ------
  5m total                                           115.9

=> the 1-hour TTL is EARNING its premium; 5m would cost ~16% more
```

Only 2.5% of gaps land in the window where the longer TTL helps — but the turns *following* those
gaps carry an average prefix of ~235,000 tokens, and rebuilding that 399 times costs more than the
2× premium on every write. **The predicted 7.1% saving is actually a ~16% increase in write cost.**

The reason is the same one that governs everything else in this document: **contexts are enormous**.
At a 235k median rebuild, surviving even a rare gap is worth paying double on every write. A
deployment with small contexts would get the opposite answer.

Verdict: **OMIT**. *Revisit if:* median prefix size falls below roughly 60,000 tokens, at which point
rebuilds become cheap enough that the 2× premium stops paying.

**17 — semantic cache — omitted.** Correctly scoped by tenant, user and authorization fingerprint,
the hit rate collapses to the per-user repeat rate; validation on a hit costs most of what the hit
saves; and the failure mode is serving one user's answer to another. Expected value is roughly
+$0.0009 per turn against a correctness risk that persists across every future code change.
*Revisit if:* a shared static corpus (public documentation search) becomes a workload, where scope is
genuinely uniform.

---

## 4. Output and instruction controls (§13, §14)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 18 | `max_tokens` per operation class | 1–5% | **L** | − | L | none | **CORE (Phase 1)** |
| 19 | Stop sequences | <1% | L | − | L | none | **CORE (Phase 1)** |
| 20 | Structured / constrained output | 1–5% + retries | L | 0 | L | none | **CORE (Phase 1)** |
| 21 | Compact `SKILL.md` with progressive disclosure | 1–3% | L | 0 | L | none | **CORE (Phase 1)** |
| 22 | Instruction deduplication and compression | <1% | M | 0 | M | L | **DEFER** |
| 23 | Streaming early termination | <1% | M | − | M | L | **OMIT** |

**20 is undervalued by its token saving.** The direct output reduction is small; the eliminated
retries are not. A malformed-output retry pays the full input cost of the repeated call — on a
40,000-token context that is one avoided retry worth more than a hundred turns of output trimming.

**22 — instruction compression — deferred.** The always-loaded set is already 676 tokens against an
800 ceiling. There is nothing meaningful to win, and rewriting instructions risks altering semantics.
*Revisit if:* the always-loaded set approaches the ceiling.

**23 — streaming early termination — omitted.** Genuine savings on yes/no gating and first-match
patterns, but it requires an early-exit predicate per operation class and interacts badly with
structured output, which already bounds the generation. The remaining cases are rare enough that the
integration complexity is not repaid.

---

## 5. Payload reduction (§8, §4)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 24 | Upstream query optimization | 3–20% | L | − | L | none | **CORE (Phase 1)** |
| 25 | Per-tool result budgets | 1–15% | L | 0 | L | none | **CORE (Phase 1)** |
| 26 | Drop manifest | 0% (correctness) | L | 0 | **prevents H** | L | **CORE (Phase 1)** |
| 27 | Ladder rungs 1–4 (nulls, fields, dedupe, rank) | 5–20% | M | 0 | M | L | **CORE (Phase 1)** |
| 28 | Ladder rung 5 (truncation) | 1–4% | L | 0 | M | L | **ADVANCED (Phase 2)** |
| 29 | Ladder rung 6 (summarization) + validation | 1–3% | H | + | **H** | M | **ADVANCED (Phase 3)** |
| 30 | Ladder rungs 7–8 (paginate, externalize) | 4–12% | M | 0 | L | L | **CORE (Phase 2)** |
| 31 | Representation selection with measurement | 1–4% | M | 0 | L | M | **ADVANCED (Phase 2)** |
| 32 | Header-delta representation | <1% | M | 0 | L | L | **OMIT** |
| 33 | Per-modality budgets and downscaling | n/a | M | − | M | L | **DEFER** |

**26 costs nothing and prevents a category of failure.** Thirty tokens per reduction, and the failure
it prevents — reduced data that looks complete, producing a confident wrong answer — is the most
expensive kind because it is never detected as a failure at all.

**29 is the only mechanism in the system that can fabricate**, which is why it is Phase 3 behind its
validator rather than Phase 1 alongside the rest of the ladder. Its 1–3% saving does not justify
shipping it early.

**32 — header-delta — omitted.** Marginal over CSV on the same shapes and adds a codec both ends must
agree on. CSV and line-oriented forms capture nearly all of the available win.

**33 — multimodal budgets — deferred.** The reference deployment has no media workload, so the saving
is not "small", it is *undefined*. *Revisit if:* screenshot-driven or document-processing work is
introduced, at which point this becomes CORE immediately — image tokens scale with resolution and are
the easiest large win in any media workload.

---

## 6. Context management (§4, §5, §6, §7)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 34 | Budget manager with hysteresis bands | 2–6% | M | 0 | L | L | **CORE (Phase 2)** |
| 35 | Task-sized reserved output | 1–4% | L | 0 | L | L | **CORE (Phase 2)** |
| 36 | Externalization + selective retrieval | 2–18% | M | + | L | L | **CORE (Phase 2)** |
| 37 | Five-way disposition by economics | 3–9% | M | 0 | M | M | **ADVANCED (Phase 2)** |
| 38 | Five-factor context scoring | included in 37 | M | 0 | M | M | **ADVANCED (Phase 2)** |
| 39 | Never-compress floor | 0% (correctness) | L | 0 | **prevents H** | none | **CORE (Phase 1)** |
| 40 | Checkpoint / rollback | 0% (correctness) | M | 0 | **prevents H** | L | **CORE (Phase 2)** |
| 41 | Code-execution bypass | 0–45% | H | − | M | M | **ADVANCED (Phase 3)** |
| 42 | Context dependency graph — explicit edges | 1–3% + correctness | M | 0 | L | L | **ADVANCED (Phase 3)** |
| 43 | Context dependency graph — inferred edges | <1% | **H** | 0 | M | H | **OMIT** |
| 44 | Semantic memory + task state | 2–6% | M | 0 | M | L | **ADVANCED (Phase 2)** |
| 45 | Episodic memory tier | <1% | M | + | M | M | **OMIT** |
| 46 | Memory provenance + temporal validity | 0% (correctness) | M | 0 | **prevents H** | L | **CORE (Phase 2)** |
| 47 | Session resumption bundle | 5–15% on resumed sessions | M | − | L | L | **CORE (Phase 2)** |
| 48 | Memory schema versioning + migration | 0% (correctness) | L | 0 | **prevents H** | none | **CORE (Phase 2)** |

**41 is the highest-variance entry in the inventory.** Zero on conversational, 25–45% on analytics.
It is `ADVANCED` rather than `CORE` only because it needs a sandbox with the same authorization
boundaries as native execution, which is real infrastructure. **For an analytics-only deployment it is
`CORE (Phase 1)` and everything else here is secondary** — it is also the one lever in `economics.md`
that survives against an already-tuned baseline.

**43 — inferred dependency edges — omitted.** Explicit edges (42) are cheap: an argument reference or
a declared consumption is a fact. Inferring that message 41 supports conclusion 78 requires either a
model call per pair or an embedding pipeline, and the errors are asymmetric — a missed edge deletes
something load-bearing. The cheap half delivers most of the correctness benefit.

**45 — episodic memory — omitted.** Three tiers were specified; two carry the value. Semantic memory
holds durable facts, task state holds the current objective, and the archive holds everything else
addressably. A distinct episodic tier adds a store, a retrieval path and a relevance model for
"important previous interactions" — a category that in practice resolves to either a semantic fact or
an archive reference. *Revisit if:* cross-session personalization becomes a product requirement with
its own success metric.

---

## 7. Tool selection (§2, §3)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 49 | Metadata-only tool registry | enabling | L | 0 | L | L | **CORE (Phase 1)** |
| 50 | Compact schema signatures | 1–6% | L | 0 | L | L | **CORE (Phase 1)** |
| 51 | Cache-stability-aware projection | 2–6% | M | 0 | L | L | **ADVANCED (Phase 3)** |
| 52 | Progressive disclosure levels 0–3 | −2 to +6% | M | 0 | M | M | **ADVANCED (Phase 3)** |
| 53 | Tool necessity / value-of-information | 2–8% | M | − | M | M | **ADVANCED (Phase 3)** |
| 54 | Model-based tool reranker | <1% | M | + | M | **H** | **OMIT** |
| 55 | Call batching and combination | 5–20% on loop-shaped work | M | − | L | L | **CORE (Phase 2)** |

**50 before 52.** Compact signatures cut 40–60% off schema text at essentially no risk and no cache
consequence — they change what a schema *renders as*, not which schemas are present. Progressive
disclosure changes the *set*, which is where churn cost lives. Doing 50 first captures much of the
schema saving without any of the cache risk, and is why 52 is Phase 3 with a negative floor on
conversational workloads.

**54 — model-based reranker — omitted.** This is anti-pattern 3 measured: on the reference
deployment it breaks even at best, and the deterministic ranking plus incumbency bonus already picks
correctly in the overwhelming majority of cases. *Revisit if:* a deployment reaches thirty or more
candidate tools with genuinely overlapping capabilities.

---

## 8. Model and reasoning routing (§10)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 56 **[M]** | Reasoning-effort routing by operation class | ~~5–30%~~ **~2.4%** | **L** | − | L | none | **CORE (Phase 1)** — verdict holds |
| 57 | Deterministic-first (Tier 1) | 1–4% | L | − | L | none | **CORE (Phase 1)** |
| 58 | Model-tier routing by operation class | 2–8% | M | − | M | L | **ADVANCED (Phase 3)** |
| 59 | Switching-cost-aware selection | prevents loss | M | 0 | L | L | **ADVANCED (Phase 3)** |
| 60 | Quality floors from measured success rates | 0% (correctness) | M | 0 | **prevents H** | L | **ADVANCED (Phase 3)** |
| 61 | Escalation ladder incl. raise-reasoning rung | 2–6% | M | − | L | L | **CORE (Phase 2)** |
| 62 | Provider batch inference | 0% interactive | M | **++** | L | L | **OMIT** |

**56 keeps its verdict but loses its ranking.** Modelled at 21% of savings, it measures at **~2.4% of
total cost** — thinking is 18.1% of output tokens, but output is only 13% of the bill, so the whole
category is small. It stays `CORE` because it is a lookup table with zero runtime overhead and a
negative latency impact: at that price, 2.4% is still worth taking. It is simply not the second-biggest
lever, and a roadmap that sequenced work by its modelled rank would have been wrong.

The lesson generalises: **a percentage of a category is not a percentage of a bill.** The model
reasoned in category shares; the measurement reasons in dollars.

**58 is Phase 3, not Phase 1, because of 59.** Naive tier routing on a warm session loses money —
D6 §11 shows a model at a third of the price costing 2.2× more. Tier routing without switching-cost
awareness is worse than no tier routing at all, so they ship together.

**62 — batch inference — omitted.** Meaningful discounts, but it converts an interactive turn into a
deferred one. Wrong shape for an agent loop. *Revisit if:* an offline evaluation or bulk-enrichment
workload appears, where it becomes clearly correct.

---

## 9. Delegation and parallelism (§11, §12)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 63 | Parallel branches + deterministic reducer | 0% tokens, 20–50% latency | M | **−−** | M | L | **ADVANCED (Phase 3)** |
| 64 **[M]** | Sub-agent delegation | ~~−3 to +8%~~ **42% of spend already** | H | − | H | M | ~~DEFER~~ → **SATISFIED, ROI unmeasured** |
| 65 **[M]** | Sub-agent result contracts | included in 64 | L | 0 | L | L | ~~DEFER~~ → **SATISFIED** |
| 66 **[M]** | Depth attenuation + spawn budgets | 0% (bounds cost) | L | 0 | prevents H | L | ~~DEFER~~ → **ADVANCED (Phase 2)** |
| 67 | Trust ceiling propagation | 0% (correctness) | L | 0 | **prevents H** | none | **CORE (Phase 1)** |
| 68 | Speculative execution | negative | H | −− | **H** | M | **OMIT** |

**63 saves no tokens at all** and is in the plan anyway, because the specification's latency
constraint is a constraint and parallelism is how it is met when a turn has independent work. It is
listed honestly: its saving column is zero.

**64–66 — the deferral was wrong, and it was wrong for a specific, instructive reason.**

The original verdict deferred the whole delegation cluster on the cold-prefix penalty: a sub-agent
starts with no cache, so it pays full price for context the primary gets at a 90% discount. That
argument is laid out in `anti-patterns.md` §7 and it is arithmetically sound. Its premise is false
here:

```text
                turn share   cost share   hit rate   tokens/turn
main sessions        38.3%        58.0%      97.1%       235,813
sub-agents           61.7%        42.0%      93.3%       111,503
```

**Sub-agents run at a 93.3% hit rate.** They are not cold — the harness shares cached prefixes across
them. They also receive less than half the context per turn, which is precisely the minimal-context
property D7 §3 was designed to enforce and which the platform already provides.

So delegation is not a build decision: it is already deployed and already 42% of spend. The verdict
becomes `SATISFIED` for the mechanism and **`ADVANCED (Phase 2)` for the bounding controls (66)** —
depth attenuation and dollar spawn budgets are the parts that are *not* provided, and 42% of spend
running without a cost ceiling is worth bounding.

**What is still unmeasured is ROI.** That the delegated turns ran efficiently does not establish
that they were cheaper than doing the work natively; that needs the A/B in `test-strategy.md` §6, not
a transcript. The honest position is: the blocking objection is retired, the economic case is open.

The generalisable failure: **the design reasoned about a platform it had not measured.** Every
premise in the anti-pattern analysis was a property of an assumed harness, and one real harness
falsified it.

**68 — speculative execution — omitted.** It pays for branches it discards; it cannot touch anything
side-effectful; and it needs a well-calibrated `P(branch_needed)`, which the estimator bias monitor
suggests will not be well-calibrated for some time. Expected value is negative on all three workloads.
*Revisit if:* a latency-critical workload appears with a reliably bimodal branch structure and
measured branch probabilities above 0.7.

---

## 10. Failure handling and safety (§16, §19)

| # | Mechanism | Saving | Cx | Lat | Risk | Ovh | Verdict |
|---|---|---|---|---|---|---|---|
| 69 | Retry budgets + exponential backoff | 1–5% | L | 0 | L | none | **CORE (Phase 1)** |
| 70 | Error classification + deterministic fix | 2–6% | M | − | L | L | **CORE (Phase 2)** |
| 71 | Normalized one-line errors | 1–3% | L | 0 | L | none | **CORE (Phase 1)** |
| 72 | Loop detection | 1–8% (tail) | M | 0 | L | L | **CORE (Phase 2)** |
| 73 | Circuit breakers | 1–4% (tail) | L | − | L | L | **CORE (Phase 2)** |
| 74 | Fail-open demotion for every component | 0% (availability) | M | 0 | **prevents H** | L | **CORE (Phase 1)** |
| 75 | Trust labelling + min-trust propagation | 0% (security) | M | 0 | **prevents H** | L | **CORE (Phase 1)** |
| 76 | PII sanitization before external writes | 0% (compliance) | M | + | **prevents H** | L | **CORE (Phase 1)** |
| 77 | Tenant-scoped cache and store keys | 0% (security) | L | 0 | **prevents H** | none | **CORE (Phase 1)** |
| 78 | Erasure propagation incl. embeddings | 0% (compliance) | M | 0 | **prevents H** | none | **CORE (Phase 2)** |

**Every row here saves 0–8% and none is optional.** Loop detection and circuit breakers earn their
place on the tail rather than the median: they do nothing on a healthy turn and prevent the runaway
that costs more than a week of median turns.

**75–78 are Phase 1 because externalization is Phase 2.** The moment the archive exists, the agent's
entire history acquires a durable copy and a compliance surface. The sanitizer must precede the store,
not follow it.

---

## 11. Summary

**As originally modelled:**

```text
CORE (Phase 0)         7    measurement and accounting
CORE (Phase 1)        22    cache ordering, output controls, upstream reduction, ladder 1–4,
                            signatures, reasoning routing, safety
CORE / ADVANCED (2)   16    budget manager, externalization, dispositions, memory, failure handling
ADVANCED (Phase 3)     9    bypass, disclosure, tool VOI, tier routing, parallelism, summarization
DEFER                  7    delegation cluster, multimodal, instruction compression
OMIT                  12    semantic cache, streaming early exit, header-delta, inferred graph,
                            episodic memory, model reranker, batch inference, speculative execution
                      ───
                       73   line items across 78 numbered mechanisms
```

**After measurement** — six rows moved, one added:

```text
SATISFIED              5    prefix ordering (8), append-only (9), breakpoints (10),
                            delegation (64), result contracts (65)
                            → verify and monitor; do not build
CORE (Phase 1)        18    output controls, upstream reduction, ladder 1–4, per-tool budgets,
                            signatures, reasoning routing, safety, TTL selection (17b, new)
CORE / ADVANCED (2)   17    + spawn budgets and depth attenuation (66), promoted from DEFER
ADVANCED (Phase 3)     9    unchanged
DEFER                  4    multimodal, instruction compression, and two sub-items
OMIT                  12    unchanged — no omission depended on a falsified assumption
                      ───
                       74   line items
```

### The findings that survived measurement, and the ones that did not

**1. ~~Four fifths of the value is in Phases 0–1~~ — half of Phase 1 was already done.** The modelled
claim was that prefix ordering, reasoning routing, `max_tokens` and upstream query parameters account
for ~82% of savings. Measured: prefix ordering and append-only history were **already satisfied by the
platform** (95.4% hit rate) and reasoning routing is worth **~2.4%**, not 21%. The shape of the claim
was right — the cheap mechanisms dominate — but on a mature harness the cheapest ones are already
built, and what remains is the category the model ranked *below* them.

**2. The mechanisms that sound most like optimization still mostly lose — with one retraction.**
Semantic caching, speculative execution, a model-based tool reranker and inferred dependency graphs
remain omitted; the arithmetic against them did not depend on any falsified assumption. **Sub-agent
delegation is the retraction.** It was deferred on a cold-prefix argument that the measurement showed
to be false for this platform, where sub-agents run at a 93.3% hit rate and are already 42% of spend.

**3. The correctness mechanisms have no savings column and are not negotiable.** Unchanged, and
strengthened by everything above. Drop manifests, never-compress floors, trust propagation,
sanitization, provenance, checkpoint/rollback and fail-open demotion contribute 0% and are all `CORE`.
They are what makes savings *keepable*: an optimizer that saves 60% and occasionally produces a
confidently wrong answer has not saved anything, because the cost of a wrong answer is not denominated
in tokens. Nothing in the measurement touched them, because they were never justified by savings.

**4. (New, from the data.) Cost tracks context volume almost 1:1, and nothing else comes close.**
Cache reads and writes together are **93% of measured spend**, and both scale with how many tokens
are in the context rather than how well they are cached:

```text
cut the median context 10%  →  save ~10% of the bill
cut it 25%                  →  save ~24%
cut it 50%                  →  save ~48%
```

That points at externalization, selective retention and per-tool result budgets — mechanisms 25, 27,
30 and 36, which the original plan spread across Phases 1 and 2 as a secondary concern. On this
deployment they are the primary concern and everything else is rounding.

### The meta-finding

Three of the four claims above needed revision after two weeks of measurement, and the revisions were
not small: a 60%-of-savings mechanism went to zero, a deferred cluster turned out to be 42% of live
spend, and a cost category worth 39% of the bill was absent from a 78-mechanism inventory.

The design was not careless — every verdict followed from its stated assumptions, and those
assumptions were written down where they could be checked. **That is the only reason the errors were
findable.** An inventory that had asserted its verdicts without naming the reference deployment would
have been just as wrong and impossible to correct.

This is the argument for Phase 0 restated as evidence rather than advice.

### What would change these verdicts

| Change | Verdict shifts |
|---|---|
| Analytics becomes the dominant workload | 41 (bypass) → CORE Phase 1 |
| A media workload is introduced | 33 (multimodal) → CORE immediately |
| A shared static corpus appears | 17 (semantic cache) → ADVANCED, under V8 |
| Tool count exceeds ~30 with overlap | 54 (reranker) → ADVANCED |
| An offline/bulk workload appears | 62 (batch) → CORE for that path |
| **Moving off a mature harness to a raw API integration** | 8, 9, 10, 64, 65 revert from SATISFIED to CORE — nothing provides them |
| **Turn-to-turn gaps measured under 5 minutes** | 17b becomes actionable: re-price 1h writes at the 5m rate |
| **A delegation ROI A/B is run** | 64 resolves either way; it is currently 42% of spend on an unmeasured economic case |

The sixth row is the important one. The `SATISFIED` verdicts are **properties of the platform, not of
the problem.** They are the rows most likely to flip back, and they carry 60% of the originally
modelled savings — so a deployment that changes harness should re-run Phase 0 before assuming any of
this transfers.
