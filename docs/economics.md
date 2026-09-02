# Deliverable 8 — Token Economics Model

**Stage 5 of 5.** The cost model, expected savings per optimization, the overhead budget, and two
worked scenarios that produce very different answers.

Companion documents: [`observability.md`](observability.md) (D10) ·
[`anti-patterns.md`](anti-patterns.md) (§24) · [`mechanism-inventory.md`](mechanism-inventory.md)
(§24 right-sizing) · [`roadmap.md`](roadmap.md) (D12).

---

## 1. Total cost

```text
TotalCost =
      LLMInputCost + LLMOutputCost + ThinkingCost
    + CacheReadCost + CacheWriteCost
    + ToolAPICost + DatabaseCost + CodeExecutionCost
    + StorageCost + NetworkCost + SubAgentCost
    + OptimizerOverhead
```

Expanded to what a ledger entry actually multiplies:

```text
LLMInputCost   = uncached_input_tokens × price.input
CacheReadCost  = cached_input_tokens   × price.input × (1 − cached_input_discount)
CacheWriteCost = cacheable_tokens      × price.cache_write        # only on a cold prefix
LLMOutputCost  = output_tokens         × price.output
ThinkingCost   = thinking_tokens       × price.output             # billed as output

SubAgentCost   = Σ over sub-agents of ( TotalCost(sub) )          # recursive, including its overhead
                 + context_transfer + coordination + result_integration

OptimizerOverhead = Σ over advisors run of ( their token and wall-clock cost )
```

**Three terms are routinely omitted from cost models and each one inverts a decision.**

*`ThinkingCost` bills at the output rate.* On a turn producing 400 visible tokens with a 4,096-token
thinking budget, deliberation is roughly 90% of output spend and is invisible in the response. A
system that folds it into `LLMOutputCost` will tune answer length and never find the money.

*`CacheWriteCost` only applies on a cold prefix,* which makes it the tax on switching — model,
tool set, or anything that edits the prefix. It is what turns "the cheaper model" into the more
expensive turn (D6 §11).

*Non-token costs are first-class.* `DatabaseCost`, `ToolAPICost`, `CodeExecutionCost`,
`StorageCost` and `NetworkCost` participate in every value-of-information comparison. A database scan
returning 200 tokens can cost more than a tool call returning 8,000, and a cost model denominated
only in tokens will choose the scan every time.

---

## 2. The three decision formulas

Everything the optimizer decides reduces to one of these.

### 2.1 Value of information — should this operation run?

```text
NetInformationValue =
      P(result changes the decision) × ValueOfCorrectDecision
    − ( LLM tokens + API + database + search + execution + storage + transfer
        + rate-limit consumption + latency + coordination )

run only when NetInformationValue > 0
```

`ValueOfCorrectDecision` comes from `tools.necessity.value_of_correct_decision_usd`, keyed by task
risk. It is a crude per-deployment number ($0.02 / $0.20 / $2.00 by default) and deliberately so: the
point is not precision, it is that the engine answers *"is this retrieval worth 4,000 tokens plus its
API charge?"* rather than *"do I have 4,000 tokens left?"*.

### 2.2 Compaction benefit — should this prefix be edited?

```text
compaction_benefit =
      tokens_removed × input_price × expected_remaining_turns
    − cached_tokens_after_edit × input_price × cached_discount × expected_remaining_turns
    − summarization_cost

compact only when the pessimistic bound is positive
```

### 2.3 Delegation — should this be a sub-agent?

```text
subagent_cost = input + output + routing
              + context_transfer + cold_prefix_penalty
              + coordination + result_integration

delegate only when  subagent_cost.hi < native_cost.lo − min_expected_saving
```

`cold_prefix_penalty` is the term the specification names and most implementations drop. A sub-agent
starts with no cache, so it pays full price for context the primary was getting at a 90% discount.

---

## 3. The reference deployment

Every figure below is anchored to one concrete deployment, because savings claims without a stated
baseline are not claims.

```text
volume        50,000 turns/month
mix           conversational 55%  ·  analytics 25%  ·  coding 20%
sessions      median 9 turns; warm prefix typical
provider      prompt caching supported · 90% cached-input discount · 5-minute TTL

prices (USD per million, illustrative)
  Tier 4      input 3.00   output 15.00   cache read 0.30   cache write 3.75
  Tier 3      input 1.00   output  5.00
  Tier 2      input 0.25   output  1.25
```

**Baseline** is the pass-through path: full projection, all candidate schemas, default Tier 4 with
`reasoning: MEDIUM`, no reduction, and whatever cache hits happen accidentally.

---

## 4. Scenario A — a bloated baseline

The common starting point: default reasoning on every turn, tool schemas loaded wholesale, history
rewritten cosmetically so prefixes rarely match, raw tool payloads.

```text
                    turns/mo   input    cached   output   think   $/turn    $/month
conversational        27,500   12,000     35%       400     800   0.0427     1,174
analytics             12,500   48,000     35%       700   1,500   0.1356     1,695
coding                10,000   34,000     35%     1,800   1,200   0.1149     1,149
                                                                            ──────
baseline                                                                     4,018
```

Optimized, with the Phase 0–3 mechanisms enabled:

```text
                    input    cached   output   think   overhead   $/turn    $/month
conversational     10,400      78%       360     120    0.0003    0.0165       453
analytics          18,000      78%       650     500    0.0015    0.0398       498
coding             21,000      78%     1,700     900    0.0012    0.0590       590
                                                                              ─────
optimized                                                                     1,541

saving  $2,477/month  ·  62%
```

### Where the money actually came from

> **Superseded for the measured deployment.** This table is arithmetic on the *modelled* reference deployment in §3. A Phase 0 measurement over real transcripts ([`phase0-findings.md`](phase0-findings.md)) found a 95.4% baseline cache hit rate rather than 35%, which reduces the first row to approximately zero and moves the money to context volume. The arithmetic below is unchanged and still correct *given its assumptions* — read it as a worked method, not as a result.

Attributed by isolating each lever against the baseline:

| Lever | Monthly saving | Share |
|---|---:|---:|
| Cache-aware prefix ordering and append-only discipline | $1,475 | **60%** |
| Reasoning-effort routing | $514 | 21% |
| Payload reduction, externalization and the code bypass | $490 | 20% |
| Output controls (`max_tokens`, stop, structured) | $41 | 1.7% |
| Non-token delta (execution added, database scans reduced) | −$13 | −0.5% |
| Optimizer overhead | −$39 | −1.6% |

**This table is the most important output of the economics model, and it is not the result anyone
expects.** Sixty percent of the saving comes from the cheapest mechanism in the system — putting the
stable content first and never rewriting it — which costs nothing at runtime and needs no scoring, no
routing and no model call. A further 21% comes from a lookup table mapping operation class to
reasoning effort.

**Four fifths of the value is in Phase 0 and Phase 1.** The elaborate machinery — context scoring,
tool routing, delegation, dependency graphs — competes for the remaining fifth against its own
overhead. That is what makes the mechanism inventory's `OMIT` verdicts defensible rather than
squeamish.

---

## 5. Scenario B — an already-lean baseline

The same deployment, but the team has already done the obvious things: prefixes are stable (75% hit
rate), reasoning is already tuned per task, `max_tokens` is set, and tool schemas are curated.

```text
                    baseline $/turn   optimized $/turn   saving
conversational             0.0181            0.0166        8%
analytics                  0.0562            0.0421       25%
coding                     0.0714            0.0623       13%
                    ──────────────    ───────────────    ─────
monthly                     $2,057            $1,753       15%
```

**15%, not 62%** — and the gap is entirely a property of the baseline, not of the optimizer. The two
scenarios run identical code.

> **The measured deployment is Scenario B, and more extreme than written here** — 95.4% cache hit rate, reasoning already cheap. See [`phase0-findings.md`](phase0-findings.md).

This is why the specification's acceptance criteria say *"target ≥ 40%"* while also noting that an
already-lean system may yield 10–15%. Both are true simultaneously. It is also why **Phase 0 is
baseline profiling**: without knowing which scenario you are in, a 40% target is either trivially met
or unreachable, and no amount of engineering changes which.

The one lever that holds up in both scenarios is the **code-execution bypass on analytics** — 25%
even against a tuned baseline, because it does not compete with caching or reasoning tuning. It
attacks a category neither of them touches.

---

## 6. Expected savings per optimization

Ranges, per workload, for the reference deployment. Negative entries are not hypothetical.

| Mechanism | Conversational | Analytics | Coding | Notes |
|---|---|---|---|---|
| Stable prefix ordering | 30–45% | 35–55% | 30–45% | Collapses to ~0 if the baseline already caches |
| Append-only history | 5–15% | 3–8% | 5–12% | Prevents cosmetic-rewrite invalidation |
| Reasoning-effort routing | 20–30% | 10–18% | 5–12% | Largest where turns are short and answers simple |
| `max_tokens` per operation class | 2–5% | 1–3% | 1–2% | Free; also prevents runaway generations |
| Structured output | 1–4% | 2–5% | 1–3% | Plus the avoided-retry saving, which is larger |
| Per-tool result budgets | 1–3% | 8–15% | 5–10% | Bounds the tail rather than the median |
| Payload middleware ladder | 1–3% | 12–22% | 8–14% | Rung 2 (field removal) is most of it |
| Upstream query optimization | 0–2% | 10–20% | 3–8% | Cheaper than the middleware; do it first |
| Code-execution bypass | 0% | 25–45% | 5–15% | Order-of-magnitude on data-heavy turns, zero elsewhere |
| Externalization + selective retrieval | 2–5% | 6–12% | 10–18% | Coding wins: file reads are large and read once |
| Selective retention / context scoring | 3–8% | 2–5% | 4–9% | Modest, and the most expensive to build |
| Compact schema signatures | 1–3% | 2–4% | 3–6% | 40–60% off schema tokens, but schemas are a small share |
| Progressive tool disclosure | **−2 to +1%** | 2–5% | 2–6% | Negative where churn cost exceeds schema savings |
| Model-tier routing | 2–6% | 3–8% | 2–5% | Suppressed by switching cost on warm sessions |
| Result caching | 3–10% | 5–12% | 2–6% | Depends entirely on repeat-query rate |
| Semantic caching | **−1 to +2%** | ~0% | **negative** | Validation usually costs more than the call saved |
| Sub-agent delegation | **negative** | 0–8% | **−3 to +2%** | Cold-prefix penalty dominates on warm sessions |
| Parallel branches | 0% (latency only) | 0% (latency only) | 0% (latency only) | Saves wall-clock, not tokens |
| Speculative execution | negative | −5 to +2% | negative | Pays for branches it discards |
| Multimodal budgets | n/a | n/a | n/a | Large where media exists; zero where it does not |

**Three entries are worth reading as warnings rather than estimates.** Progressive disclosure,
semantic caching and sub-agent delegation all have negative expected value in at least one workload
of the reference deployment, and all three are the kind of mechanism that gets built because it
sounds sophisticated. The quantified case against each is in [`anti-patterns.md`](anti-patterns.md).

---

## 7. The optimizer's own budget

```text
overhead_cap = max( floor_usd , max_fraction × estimated_baseline_turn_cost )
             = max( $0.0005 , 0.10 × baseline )

enable an advisor only when  expected_savings > its metered overhead
```

Measured across the reference deployment, the optimizer costs **$39/month against $2,477 saved** — a
1.6% overhead ratio. Two design choices produce that:

* **The fast path fires on 70% of conversational turns**, where the full engine would cost more than
  the turn. On those turns overhead is seven comparisons.
* **The advisor scheduler runs advisors in descending expected savings per unit of overhead** and
  stops when the ratio reaches 1.0, so the marginal advisor never runs at a loss.

If the overhead ratio is not reported in your telemetry, you do not know whether your optimizer is
one. `optimization_overhead` is a first-class ledger category for that reason (D10 §3).

---

## 8. Sensitivity

Which assumptions the 62% actually rests on, and what happens when each is wrong.

| Assumption | If it is wrong | Effect on total saving |
|---|---|---|
| Baseline cache hit 35% | Already 75% | 62% → 15% |
| Cached-input discount 90% | 50% | 62% → 48% |
| Cache TTL 5 min, sessions warm | Sessions mostly cold | 62% → 30%; prefix ordering stops paying |
| Default reasoning MEDIUM everywhere | Already tuned | −21 points |
| Analytics 25% of mix | 5% | −11 points; the bypass has little to work on |
| Thinking bills at output rate | Billed at input rate | −17 points |
| Median session 9 turns | 2 turns | Prefix ordering and compaction both collapse |

**Session length is the hidden variable.** Nearly every mechanism here amortizes over
`expected_remaining_turns`: cache warmth, compaction benefit, the value of carrying a tool schema.
A deployment of one-shot requests gets almost none of this, and should implement Phase 0 and the
output controls and stop.

---

## 9. Non-token costs in practice

The reference deployment's non-LLM spend, which the LLM-token view would miss entirely:

```text
database compute      analytics turns, ~$0.003/turn baseline        $ 38/mo
                      reduced to ~$0.001 by upstream aggregation    $ 13/mo
code execution        bypass turns, ~$0.004 each                    $ 20/mo
archive storage       ~40 GB retained 30 days                       $  9/mo
retrieval reads       selective fetches against the archive         $  4/mo
                                                                    ───────
                      baseline $38  →  optimized $46
```

**The optimizer increases non-token cost by $8/month to save $2,477 in tokens.** That trade is
obviously correct here and obviously not universal: a deployment whose database charges per scan
rather than per byte, or whose execution sandbox is billed per second with a slow cold start, can
find the bypass more expensive than the payload it avoids. The model prices it either way; the answer
is a property of the price list, not of the architecture.

---

## 10. What this model does not do

* **It does not price quality.** Cost and task success are separate axes, and the acceptance criteria
  treat success as a constraint rather than a term. There is no exchange rate here between a
  percentage point of accuracy and a dollar, deliberately.
* **It does not price latency beyond p50/p95 tracking.** Latency is a constraint (≤10% p95 increase),
  not a cost term. A deployment where latency has a dollar value should add it as a term and re-derive
  the delegation and parallelism gates, which are the two decisions it would change.
* **It does not model rate limits as a cost.** They appear as a consumed resource in the VOI formula
  but have no price. In a rate-limited deployment they are frequently the binding constraint, and the
  model would need a shadow price to route correctly.
* **Every figure here is illustrative.** Real prices, real baselines and real workload mixes go in
  `models.bindings` and a measured Phase 0 profile. The arithmetic is the deliverable; the constants
  are placeholders.
