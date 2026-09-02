# Deliverable 12 — Phased Implementation Roadmap

**Stage 5 of 5.** Phases populated from the mechanism inventory's verdicts. Nothing appears here
without a verdict, and `OMIT` mechanisms appear nowhere.

Companion documents: [`mechanism-inventory.md`](mechanism-inventory.md) (the verdicts) ·
[`economics.md`](economics.md) (D8) · [`test-strategy.md`](test-strategy.md) (D11) ·
[`observability.md`](observability.md) (D10) ·
**[`phase0-findings.md`](phase0-findings.md) (measured data)**.

> ### Phase 0 has been run, and it reordered this plan
>
> The phases below were sequenced by the *modelled* attribution in `economics.md` §4. That
> measurement now exists ([`phase0-findings.md`](phase0-findings.md)) and it inverts the ordering:
>
> | | Modelled share | Measured on real data |
> |---|---|---|
> | Prefix ordering + append-only (Phase 1) | 60% | **already satisfied** — 95.4% hit rate |
> | Reasoning-effort routing (Phase 1) | 21% | **~2.4%** of total cost |
> | Context volume (Phase 2) | 5–15% | **93% of cost**, ~1:1 with spend |
> | Cache writes | not modelled | **39% of cost** |
>
> **Phase 1 is mostly already done by the platform; Phase 2 is the whole game.** §11 below is the
> revised plan for the measured deployment. Phases 0–3 are kept as written, because they remain the
> correct sequence for a deployment that has *not* measured itself — and because the revision is only
> legible against them.

---

## 1. How the phases were populated

```text
CORE (Phase 0)        →  Phase 0
CORE (Phase 1)        →  Phase 1
CORE / ADVANCED (2)   →  Phase 2
ADVANCED (Phase 3)    →  Phase 3
EXPERIMENTAL          →  Phase 4   (empty for this deployment — see §7)
DEFER                 →  the deferred register, with its threshold
OMIT                  →  nowhere
```

Phase 0 ships first regardless of verdicts, because it is what makes the verdicts evidence-based
rather than assumed. Every phase is gated by the regression criteria in `test-strategy.md` §6 before
the next begins.

**The shape of this plan is unusual and worth stating up front.** Phases 0 and 1 contain the cheapest
mechanisms in the specification and capture roughly 82% of the available saving
(`economics.md` §4). Phases 2 and 3 contain most of the engineering and compete for the remaining
18% against their own overhead. A team that ships Phase 1 and stops has done well; a team that skips
Phase 0 to get to Phase 3 will not be able to tell whether it did.

---

## 2. Phase 0 — Measure

**Goal:** know which scenario you are in before optimizing anything.
**Ships:** nothing user-visible. **Saves:** nothing.

| # | Mechanism | Deliverable |
|---|---|---|
| 1 | Per-category token accounting | D8, D10 |
| 2 | Baseline counterfactual, estimated per turn | D10 §9 |
| 3 | Tokenizer adapters + byte bound | D3 §11 |
| 4 | Estimator bias monitor | D10 §6 |
| 5 | Baseline profiling over a representative task corpus | D11 §7 |
| 6 | Gate runner, shadow and A/B harness | D11 §6 |
| 7 | Measured baselines for gate decisions | D11 §6 |

**Exit criteria**

* A per-category token profile exists for every workload class, from real traffic.
* Cache hit rate, session length distribution and thinking-token share are measured.
* The largest cost category is identified and named. Phase 1 is re-ordered to attack it first.
* Shadow mode runs end to end with zero effect on live turns.

**Why this is not optional.** `economics.md` §5 shows the same implementation producing a 62% saving
against a bloated baseline and 15% against a lean one. Those are different projects with different
justifications. Phase 0 is roughly two weeks of work that determines whether the following three
months are worth doing.

---

## 3. Phase 1 — Cheap wins

**Goal:** capture the majority of the available saving with mechanisms that carry no runtime overhead
and no correctness risk.
**Expected:** 30–50% on a bloated baseline; 5–10% on a lean one.

| # | Mechanism | Why it is here |
|---|---|---|
| 8 | Stable-prefix layer ordering | 60% of the reference deployment's saving, zero overhead |
| 9 | Append-only history discipline | Prevents cosmetic-rewrite invalidation |
| 10 | Cache breakpoints at stability boundaries | Free, provider-native where supported |
| 13 | Schema cache | Trivial, removes repeated schema rendering |
| 16 | Provider capability probe and adapters | Everything downstream depends on it |
| 18 | `max_tokens` per operation class | Table lookup |
| 19 | Stop sequences | Table lookup |
| 20 | Structured / constrained output | Small direct saving; large avoided-retry saving |
| 21 | Compact `SKILL.md` with progressive disclosure | The meta-bloat guard, enforced at deploy |
| 24 | Upstream query optimization | Cheaper than any post-hoc reduction |
| 25 | Per-tool result budgets | Bounds the tail |
| 26 | Drop manifest | 30 tokens; prevents confident wrong answers |
| 27 | Ladder rungs 1–4 | Field removal alone is ~63% of a typical payload |
| 39 | Never-compress floor | Correctness, load-time enforced |
| 49 | Metadata-only tool registry | Enabling for everything in Phase 3 |
| 50 | Compact schema signatures | 40–60% off schema text, no cache consequence |
| 56 | Reasoning-effort routing by operation class | 21% of the saving, and it is a lookup table |
| 57 | Deterministic-first (Tier 1) | Cheapest possible routing outcome |
| 67 | Trust ceiling propagation | Must exist before delegation, not after |
| 69 | Retry budgets + backoff | Bounds the tail |
| 71 | Normalized one-line errors | Removes stack traces from context |
| 74 | Fail-open demotion for every component | INV-2, before anything can fail |
| 75 | Trust labelling + min-trust propagation | Before externalization exists |
| 76 | PII sanitization before external writes | Before the archive exists |
| 77 | Tenant-scoped cache and store keys | Structural, cheap, unrepresentable otherwise |

Also in Phase 1: the `NO_OPTIMIZATION` fast path (§17 S0) and the metered overhead budget. They belong
here rather than later because they are what keeps the *later* phases honest — an overhead meter added
after the advisors is an overhead meter that will be tuned to pass.

**Exit criteria**

* Suite gate passes: cost reduction CI lower bound > 0 in every class, success drop CI upper bound
  ≤ 0.02, p95 latency increase ≤ 0.10.
* `cache_hit_rate` improved and stable; `cache_invalidation_rate` below 2 per 100 turns.
* `optimization_overhead_ratio` < 0.02.
* Fail-open sweep passes for every component built so far.

**Sequencing note.** Do 8 and 9 before anything else in this phase and measure the result on its own.
They are the highest-value change in the plan and they are also the change most likely to be
undermined later by an unrelated edit to the prefix — establishing the measurement first makes that
regression detectable.

---

## 4. Phase 2 — Context management

**Goal:** manage a context that has stopped fitting, without undoing Phase 1's cache gains.
**Expected:** a further 5–15%.

| # | Mechanism | Notes |
|---|---|---|
| 11 | Cache-invalidation cost in compaction | The gate that prevents Phase 2 from destroying Phase 1 |
| 12 | Compaction batching at boundaries | One prefix edit, not several |
| 14 | Result cache with idempotency keys | Mutations must not double-execute |
| 15 | Retrieval cache | Small, cheap |
| 28 | Ladder rung 5 (truncation) | Semantic boundaries, offsets in the marker |
| 30 | Ladder rungs 7–8 (paginate, externalize) | Requires the archive |
| 31 | Representation selection with measurement | Sample to choose, measure the winner |
| 34 | Budget manager with hysteresis bands | Hysteresis is mandatory, not a refinement |
| 35 | Task-sized reserved output | Stops reserving 24,000 tokens for a 16-token answer |
| 36 | Externalization + selective retrieval | The archive, with a selector API |
| 37 | Five-way disposition by economics | Needs retention/reconstruction costing |
| 38 | Five-factor context scoring | Ordering only; economics decides |
| 40 | Checkpoint / rollback | Structural sharing, or it gets skipped |
| 44 | Semantic memory + task state | Two tiers, not three (45 omitted) |
| 46 | Memory provenance + temporal validity | `as_of` on every query |
| 47 | Session resumption bundle | Never replay raw history |
| 48 | Memory schema versioning + migration | Persistent memory outlives deployments |
| 55 | Call batching and combination | Kills the per-record loop |
| 61 | Escalation ladder incl. raise-reasoning rung | Cheapest real capability increase |
| 70 | Error classification + deterministic fix | Fix without an LLM where possible |
| 72 | Loop detection | Tail protection |
| 73 | Circuit breakers | Tail protection |
| 78 | Erasure propagation incl. embeddings | The archive created the obligation |

**Exit criteria**

* Compaction never increases total session cost on the warm-prefix benchmark traces.
* `manifest_retrieval_rate` < 0.05 — reduction is not removing needed data.
* `emergency_band_rate` < 2 per 100 turns.
* Long-running-session class: facts from turn 3 still correct at turn 40.
* Erasure test passes end to end, including embeddings.

**The risk in this phase is Phase 1.** Compaction, externalization and dispositions all edit or
displace context that Phase 1 arranged for caching. Mechanism 11 is the gate that keeps them from
paying for themselves with money Phase 1 already earned, and it must land before 37 and 38, not
alongside them.

---

## 5. Phase 3 — Routing and the bypass

**Goal:** the mechanisms with real savings and real ways to lose money.
**Expected:** a further 3–10% overall; 25–45% on analytics turns specifically.

| # | Mechanism | Notes |
|---|---|---|
| 29 | Ladder rung 6 (summarization) + validation | The only rung that can fabricate; ships behind its validator |
| 41 | Code-execution bypass | Highest variance in the plan; sandbox is real infrastructure |
| 42 | Context dependency graph — explicit edges only | Inferred edges omitted (43) |
| 51 | Cache-stability-aware tool projection | Must precede 52 |
| 52 | Progressive disclosure levels 0–3 | Negative on conversational; flag per profile |
| 53 | Tool necessity / value-of-information | Needs `value_of_correct_decision_usd` tuned |
| 58 | Model-tier routing by operation class | Ships with 59, never before |
| 59 | Switching-cost-aware selection | Without it, 58 loses money on warm sessions |
| 60 | Quality floors from measured success rates | Needs Phase 0 gate data to be meaningful |
| 63 | Parallel branches + deterministic reducer | Latency, not tokens — listed honestly |

**Exit criteria**

* Every mechanism shadow-run for ≥ 500 turns before live traffic.
* Per-optimization gate passes individually: ≤ 1% success drop in every class.
* `advisor_roi` > 1 for every advisor enabled, per workload — advisors that do not pay are switched
  off per profile rather than tuned into profitability.
* Summarization: zero fabricated numbers or identifiers on the mutation corpus.
* Bypass: sandbox authorization inheritance verified; native fallback exercised.

**Two pairs must ship together.** 58 with 59, because tier routing without switching-cost awareness is
worse than no tier routing. And 51 with 52, because disclosure without churn pricing is the
prefix-thrashing anti-pattern.

**Per-profile enablement is expected here, not exceptional.** Progressive disclosure ships `off` for
conversational and `on` for analytics and coding. The bypass ships `on` for analytics, `shadow` for
coding, `off` for conversational. That is the mechanism inventory's per-deployment verdicts reaching
production.

---

## 6. Deferred register

Not in any phase. Each carries the threshold that would move it into one.

| # | Mechanism | Threshold to revisit |
|---|---|---|
| 22 | Instruction deduplication and compression | The always-loaded set approaches the 800-token ceiling |
| 33 | Per-modality budgets and downscaling | Any media workload appears — then it is **CORE immediately** |
| 64 | Sub-agent delegation | Analytics > 40% of mix, **or** sessions become predominantly cold |
| 65 | Sub-agent result contracts | Ships with 64 |
| 66 | Depth attenuation + spawn budgets | Ships with 64 |
| — | Provider-native capabilities not yet offered | Re-probe on every provider version change |
| — | Reordering `memory` before `tool_schemas` in the prefix | A/B in Stage 5 benchmarking (see `tool-router.md` §1) |

**Delegation is the significant deferral**, and it is deferred on arithmetic rather than taste: the
cold-prefix penalty makes it negative for conversational and roughly break-even for coding on the
reference deployment's warm sessions (`anti-patterns.md` §7). If sessions become short-lived, the
penalty disappears and the verdict flips.

---

## 7. Phase 4 — empty, and why

The specification's Phase 4 holds `EXPERIMENTAL` verdicts. **This deployment has none**, which is a
result rather than an oversight.

The candidates were speculative execution (68), semantic caching (17), a model-based tool reranker
(54) and inferred dependency edges (43). All four scored negative or negligible expected value against
the reference deployment, so each is `OMIT` with a stated revisit condition rather than `EXPERIMENTAL`
with a flag. Carrying a flag-isolated mechanism that the arithmetic already says will lose is how a
codebase accumulates permanently-off code paths that still have to be maintained and tested.

A deployment with different economics — latency-critical with bimodal branching, or a shared static
corpus — would populate Phase 4 from the same inventory with different numbers.

---

## 8. Sequencing and effort

Indicative, for a small team. The ratio matters more than the absolute numbers.

```text
Phase 0   Measure                     ~2 weeks     saves 0%        enables everything
Phase 1   Cheap wins                  ~4 weeks     saves 30–50%    ~82% of total value
Phase 2   Context management          ~8 weeks     saves 5–15%
Phase 3   Routing and bypass          ~8 weeks     saves 3–10%
                                      ─────────
                                      ~22 weeks
```

**Six weeks reach roughly four fifths of the value; the following sixteen chase the last fifth.** That
is not an argument against Phases 2 and 3 — context management becomes necessary the moment sessions
outgrow the window, regardless of savings, and the bypass is transformative on analytics. It is an
argument against starting there.

---

## 9. Stop conditions

A roadmap without stop conditions is a commitment to build everything on it.

**Stop after Phase 1 if:** the measured saving already meets the business target; sessions are
short (median under 4 turns), which collapses most of Phase 2's value; or context rarely exceeds 50%
of the window, which means the budget manager has nothing to manage.

**Stop after Phase 2 if:** `advisor_roi` for the routing advisors is below 1 in shadow across all
workloads; or the tool set is small and stable, which removes most of Phase 3's tool-side value.

**Reconsider the whole plan if:** Phase 0 shows a lean baseline — `economics.md` §5's Scenario B —
where the total available saving is 15% and Phases 2–3 would chase 4 points of it. In that case ship
Phase 1, keep the measurement, and revisit when the workload changes.

**Revert any phase if:** the per-optimization gate fails in any workload class after promotion. Flags
are per-mechanism precisely so a regression can be isolated to one and disabled without redeploying,
and the demotion path means turning one off degrades cost rather than correctness.

---

## 10. The one-line version

> Measure first. Order the prefix and stop rewriting it. Set `max_tokens` and reasoning effort per
> operation class. Push filters upstream and cap tool results. That is most of the money. Everything
> after it is real engineering for the remaining fifth, and half of what the specification proposes
> should not be built at all.

**Then we measured, and the first two sentences turned out to be already done.** See §11.

---

## 11. The revised plan, after Phase 0

Everything above is the plan for a deployment that has not measured itself. This is the plan for the
one that has. Source: [`phase0-findings.md`](phase0-findings.md) — a real multi-project corpus of tens of thousands of
Opus-tier turns.

### What Phase 0 retired

| Originally Phase 1 | Status | Action |
|---|---|---|
| Stable-prefix layer ordering | 95.4% hit rate | **Monitor, do not build.** Alarm if it degrades. |
| Append-only history | Satisfied by the harness | Monitor |
| Cache breakpoints | Satisfied by the harness | Monitor |
| Sub-agent delegation | Already 42% of spend at 93.3% hit | **Measure ROI**, do not build |

Four weeks of originally-planned Phase 1 work, deleted by two days of measurement.

### Phase 1′ — what is actually left, and it is small

| # | Mechanism | Expected | Why it survives |
|---|---|---|---|
| 25 | Per-tool result budgets | 1–15% | Bounds what enters context — the only thing that matters here |
| 27 | Ladder rungs 1–4 | 5–20% | Field removal is most of a typical payload |
| 24 | Upstream query optimization | 3–20% | Cheaper than reducing after the fact |
| 18–20 | `max_tokens`, stop, structured output | ~1% | Free; output is 13% of cost |
| 56 | Reasoning-effort routing | ~2.4% | Free; smaller than modelled but still positive |
| 26, 39 | Drop manifest, never-compress floor | 0% | Correctness. Non-negotiable. |
| ~~17b~~ | ~~Cache-write TTL selection~~ | **negative** | **Measured and dropped.** 1h earns its premium; see `phase0-findings.md` §4d |

### Phase 2′ — the actual work

**93% of cost is cache reads plus writes, and both scale with context volume, not cache quality.**

```text
cut median context 10%  →  save ~10% of the bill
cut it 25%              →  save ~24%
cut it 50%              →  save ~48%
```

Median prompt is 114,574 tokens; p90 is 315,999; the maximum reaches the full window. So:

| # | Mechanism | Why it is now first |
|---|---|---|
| 36 | Externalization + selective retrieval | Attacks volume directly |
| 30 | Ladder rungs 7–8 (paginate, externalize) | Same |
| 37, 38 | Five-way disposition + context scoring | Decides *what* to externalize |
| 34, 35 | Budget manager, task-sized reserved output | Bounds growth |
| 47 | Session resumption bundle | p90 session is very long; raw replay is the expensive path |
| 66 | Depth attenuation + spawn budgets | 42% of spend is delegated and currently unbounded |
| 40, 46, 48 | Checkpoint/rollback, provenance, schema versioning | Correctness, as before |

### Two measurements to take before building

**1. ~~Turn-to-turn gap distribution.~~ Done — and it killed the mechanism.** 17b was predicted at
+7.1%; measured, the 1-hour TTL is *earning* its premium, because the 2.5% of gaps it survives are
followed by ~235,000-token prefix rebuilds. The prediction was wrong by its whole magnitude plus its
sign. Recorded in `phase0-findings.md` §4d.

**2. Delegation ROI.** 42% of spend, across the majority of all turns, rests on an economic case nobody has
tested. That they run *efficiently* is measured; that they are *cheaper than native* is not. This is
the A/B in `test-strategy.md` §6 and it is the highest-value experiment available.

### Revised effort and value

```text
Phase 0    Measure                    done       —              reordered everything below
Phase 1'   What survives              ~2 weeks   5-15%          mostly free controls
Phase 2'   Context volume             ~8 weeks   20-45%         the actual money
Phase 3    Routing and bypass         ~8 weeks   low here       most of it is satisfied or small
```

**The original plan put ~82% of value in Phases 0–1 and four weeks of it was already built.** The
revised plan puts the value in Phase 2′, which the original ranked second. Both plans are correct
given their evidence; only one of them had any.

### Revised one-line version

> Your prefix is already ordered, your sub-agents are already warm, and your reasoning is already
> cheap. Your context is 114,000 tokens at the median and that is 93% of your bill. Externalize,
> retrieve on demand, and cap what tools return. Nothing else moves the number.
