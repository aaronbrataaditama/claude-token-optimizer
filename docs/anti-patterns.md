# §24 — Optimizations that increase token consumption

**Stage 5 of 5.** Ten techniques that are commonly recommended and frequently cost more than they
save, quantified against the reference deployment, with the conditions under which each does pay.

Companion documents: [`economics.md`](economics.md) (D8) ·
[`mechanism-inventory.md`](mechanism-inventory.md) (§24 right-sizing) ·
[`architecture.md`](architecture.md) §14 (the structural guards).

Every entry has the same shape: **the reasoning that makes it attractive**, **the arithmetic that
makes it wrong**, **when it is right**, and **the guard** that stops it being committed by accident.

---

## 1. Aggressive summarization that invalidates a warm cache

**Why it appeals.** Context is 78% full; the conversation is the biggest thing in it; summarizing
18,000 tokens of history to 3,000 is an obvious 15,000-token win.

**The arithmetic.** The summary edits the prefix, so everything after the edit point re-bills at full
price for the rest of the session.

```text
gain  18,000 removed × $3/M × 6 remaining turns              =  $0.324
loss  44,000 cached after the edit × $3/M × 0.90 × 6 turns   = −$0.713
      the summarization call itself                          = −$0.020
                                                                ───────
net                                                            −$0.409
```

A 15,000-token reduction that costs money. The tokens removed are counted once per turn; the discount
destroyed is counted on every downstream token, every turn.

**When it is right.** At a cache boundary — session start, task transition, TTL lapse — where there is
no warm prefix to lose. In the emergency band, where the alternative is a turn that does not fit. And
when `expected_remaining_turns` is small enough that the loss term shrinks faster than the gain.

**The guard.** `compaction_benefit` gated on its pessimistic bound, plus tail-first relief: freeing
21,000 tokens from the *uncached* tail delivers more headroom at zero cache cost (D4 §5).

---

## 2. Dynamic tool loading and unloading that churns the prefix

**Why it appeals.** Only three tools are relevant to this task; carrying twelve wastes 9,000 schema
tokens every turn.

**The arithmetic.** The tool layer sits early in the prefix, so a set change invalidates memory,
history and the turn.

```text
drop one tool at turn 12    38,400 cached tok downstream × $3/M × 0.90 × 5   = −$0.518
schema tokens saved          980 × $3/M × 5                                   =  $0.015
```

**Carrying a useless schema is 80× cheaper than deleting one.** The saving is linear in schema size;
the cost is linear in everything downstream, which grows all session.

**When it is right.** At session start and task transitions, where little sits to the right of the
tool layer. In deployments with large schemas and short sessions. And *additions* are always cheaper
than removals, because they append rather than reorder.

**The guard.** `projectTools` prices the churn and, when it fails, takes the additions and refuses the
removals (D5 §7).

---

## 3. A router LLM call that costs more than the schemas it avoids loading

**Why it appeals.** A small model picking three tools from thirty is obviously cheaper than loading
thirty schemas.

**The arithmetic.** On a conversational deployment with six candidate tools:

```text
router call    900 input + 40 output on Tier 2   = $0.000275
schemas avoided  3 tools × 320 tok × $3/M × 0.10 (cached) = $0.000288
net                                                = +$0.000013 per turn
```

Break-even, before counting that the router's own decision has to be made every turn while the
schemas it avoids would have been cached. On the analytics profile with four large schemas the router
clearly pays; on the conversational profile it does not.

**When it is right.** Large schemas, many candidates, cold prefixes, and a genuinely ambiguous choice.

**The guard.** The advisor scheduler ranks by realized savings per unit of overhead and stops at a
ratio of 1.0 — so on workloads where the router does not pay, it simply stops being scheduled. The
tool reranker is additionally gated on the top two candidates being within `rerank_margin`, so it runs
only when it might change the answer (D5 §4).

---

## 4. JSON → YAML on payloads dominated by long strings

**Why it appeals.** YAML has no braces, no quotes around keys, and no commas. It looks smaller.

**The arithmetic.** It is smaller when structure dominates and larger when content does. On a payload
of 40 records with three short keys and a 400-character body per record:

```text
compact JSON   structural overhead ~11 tok/record   ×40 =   440 tok
YAML           block scalar markers, indentation on
               every wrapped line, key repetition    ×40 =   610 tok
```

YAML costs 39% more here. On the same 40 records with twelve short fields and no long strings, YAML
wins by about 18%.

**When it is right.** High key repetition, short values, shallow nesting. The implementation encodes
exactly that as a gate: YAML is only a candidate when key repetition ≥ 0.5, mean string length ≤ 40,
and depth ≤ 3.

**The guard.** `selectRepresentation` measures candidates against the target tokenizer rather than
applying a rule, and the YAML gate removes it from candidacy where it structurally cannot win
(D3 §10). CSV or line-oriented forms usually beat both on uniform records.

---

## 5. Semantic caching on personalized or permission-scoped queries

**Why it appeals.** Many users ask the same question. Cache the answer.

**The arithmetic.** Once the key includes tenant, user and an authorization fingerprint — which it
must — the hit rate collapses toward the per-user repeat rate. And a hit still needs validating when
the answer is authorization- or time-sensitive:

```text
hit rate after correct scoping                 8%
validation call on a hit (freshness + authz)   $0.0009
call avoided on a valid hit                    $0.0140
expected value  0.08 × (0.0140 − 0.0009) − 0.92 × 0.0002 (lookup)  = +$0.00086/turn
```

Marginally positive — and that is *before* the risk. The failure mode is not a wasted lookup; it is
serving one user's answer to another. A cache whose correctness depends on getting the key right,
across every future code path, for a saving under a tenth of a cent per turn, is a poor trade.

**When it is right.** A shared, static, non-personalized corpus — documentation search, public
reference data — where scope is genuinely uniform and answers do not expire.

**The guard.** Disabled by default in all three shipped profiles. V8 refuses to enable it without
authorization- and time-sensitivity validation configured, and cache keys include scope by
construction, so the dangerous version is unrepresentable rather than merely discouraged.

---

## 6. Summarizing code, identifiers or schemas

**Why it appeals.** A 2,000-line OpenAPI schema is 24,000 tokens. A summary is 300.

**The arithmetic.** There isn't one, because the failure is not measured in tokens. A summarized
function signature that drops a parameter produces code that does not compile. A summarized
identifier is a wrong identifier. A summarized API contract sends a malformed request. Each costs a
retry — full input cost, again — and some cost a wrong action that is never detected.

**When it is right.** Never as a *replacement*. Always fine as a *pointer*: `POST /users — required:
name, email — returns: User` alongside a reference to the canonical schema is legitimate and useful.
The distinction is whether the exact version remains reachable.

**The guard.** `never_compress` is a load-time floor a profile may extend but not shrink (V14), and
any lossy in-context form requires a `canonical_ref` to the exact version in the archive. The coding
profile extends the list with file paths, type definitions, test expectations, stack traces and diffs.

---

## 7. A summarizer sub-agent that receives the full context it is meant to shrink

**Why it appeals.** Delegate the summarization so the primary does not spend its own budget on it.

**The arithmetic.**

```text
transfer 40,000 tokens to the sub-agent, cold      40,000 × $3/M     = $0.120
sub-agent generates a 600-token summary            600 × $15/M       = $0.009
result integration                                                    = $0.002
                                                                       ───────
                                                                       $0.131

primary summarizes in place: the 40,000 are already cached
                                            40,000 × $3/M × 0.10     = $0.012
                                            600 × $15/M              = $0.009
                                                                       ───────
                                                                       $0.021
```

Delegating costs **6× more** than doing it in place, entirely because the primary's copy is cached and
the sub-agent's is not. The delegation saves nothing and adds a failure mode.

**When it is right.** When the sub-agent needs materially *less* than the primary has — a genuinely
narrow subtask over a named subset — and when the primary's context is cold anyway.

**The guard.** `SubAgentSpec.inputs` is an item-ID allowlist. There is no API that accepts a context,
so the expensive version is not expressible; and `prepareMinimalContext` asserts the sub-projection
shares nothing with the primary's history layer (D7 §3).

---

## 8. Per-record tool calls in a loop

**Why it appeals.** It is the obvious way to write the agent loop, and each individual call is small.

**The arithmetic.** 200 records, one enrichment call each:

```text
per-record loop   200 × (900 tok result + 350 tok call overhead) = 250,000 tok  = $0.75
                  plus 200 round trips of latency
one batched call  1 × 14,000 tok reduced to 2,200 in context                    = $0.007
code bypass       1 script, joins and aggregates in-sandbox, 210 tok back       = $0.004
```

Two orders of magnitude, and the loop is also the version most likely to hit a rate limit and trigger
the retry ladder.

**When it is right.** When the records genuinely require independent reasoning per record and the
result of one informs the next. That is rarer than the pattern's frequency suggests.

**The guard.** The dependency graph identifies homogeneous independent operations; the bypass check
fires at `min_homogeneous_calls` (default 4); the loop detector terminates a repeating
call-with-varying-argument cycle (D5, D3, C16).

---

## 9. Summarizing a large payload the agent will need one field from later

**Why it appeals.** A 20 KB response compressed to 2 KB looks like a 90% win.

**The arithmetic.**

```text
summarize          20 KB → 2 KB, carried 6 turns    ≈ 500 tok × 6 × $3/M   = $0.009
                   turn 5 needs one field the summary dropped:
                   re-fetch + re-reason round trip                          = $0.021
                                                                             ───────
                                                                              $0.030

externalize        30-token reference carried 6 turns                        = $0.0005
                   turn 5 fetches that one field, 40 tokens                  = $0.0002
                                                                             ───────
                                                                              $0.0007
```

The summary is 40× worse, and it is worse in the specific case where it looked most attractive:
a large payload the agent mostly does not need.

**When it is right.** When the *aggregate* is the answer — counts, ranges, distributions — and the
records themselves will never be consulted.

**The guard.** The `RETRIEVE_LATER` and `EXTERNALIZE` dispositions compare reconstruction cost against
retention cost directly, and the archive supports selective retrieval so fetching one field does not
mean re-ingesting the blob (D4 §4, §7).

---

## 10. An optimizer stack whose overhead exceeds its savings on small tasks

**Why it appeals.** Every optimization is individually justified, so running all of them must be
better.

**The arithmetic.** A conversational turn with an 11,400-token warm context and two tools:

```text
turn cost, unoptimized                                            $0.0040

full engine: classify + score + rank + route + delegate-check     $0.0026
savings found on an 11k-token warm turn                           $0.0000
                                                                  ───────
optimized turn                                                    $0.0066
```

**65% more expensive.** And the failure compounds: the classic shape is
`Optimizer → Router → Analyzer → Scorer → Estimator → Sub-Agent`, where each layer is individually
defensible and the stack consumes more than the system it optimizes.

**When it is right.** On turns whose baseline cost is large enough that a 10% overhead cap funds real
analysis. That is exactly what the cap encodes.

**The guard.** Three layers. `NO_OPTIMIZATION` is a first-class plan mode with a zero-overhead fast
path (§17 S0). The overhead budget is metered and capped at 10% of the estimated baseline turn cost.
The advisor scheduler runs advisors in descending savings-per-overhead order and stops at a ratio of
1.0. And `optimization_overhead` is a reported metric, so a stack that stops paying is visible rather
than assumed.

---

## 11. The pattern behind the pattern

Nine of the ten share one structure:

> **The saving is local and immediate; the cost is distributed and deferred.**

Tokens removed are visible in this turn's request. Cache invalidated, information lost, retries
provoked, and context that must be re-fetched all land later, in a different metric, often in a
different dashboard. A token counter shows the win. The invoice shows the loss.

Two design consequences follow, and they are why the architecture looks the way it does:

* **Every optimization decision is priced across `expected_remaining_turns`, not this turn.** The
  compaction gate, the churn comparison, the retention-versus-reconstruction test and the delegation
  economics all carry that horizon term. Without it, all nine of these look profitable.
* **The baseline must be a counterfactual, not a memory.** Savings are claimed against the estimated
  cost of the same turn on the pass-through path, and gate decisions require a measured A/B. Claiming
  savings against "what we used to spend" attributes to the optimizer every unrelated change in
  traffic, model pricing and workload mix — and hides exactly these failures.

The tenth — the optimizer stack — is the specification's own warning turned on itself, and the reason
`NO_OPTIMIZATION` is a first-class outcome rather than an absence of one.
