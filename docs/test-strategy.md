# Deliverable 11 — Test Strategy

**Stage 5 of 5.** Unit, integration, adversarial and benchmark testing, with per-workload reporting
against the acceptance criteria.

Companion documents: [`observability.md`](observability.md) (D10) · [`economics.md`](economics.md)
(D8) · [`mechanism-inventory.md`](mechanism-inventory.md) (§24) · [`roadmap.md`](roadmap.md) (D12).

---

## 1. What has to be true

```text
Target              ≥ 40% reduction in total inference cost on the benchmark suite
Minimum             statistically significant reduction, reported with confidence intervals
Quality constraint  ≤ 2% absolute drop in task success rate
Latency constraint  ≤ 10% increase in p95 latency
Per-optimization    significant token reduction in its category,
                    ≤ 1% success drop, ≤ 10% p95 latency increase
```

Two rules govern how these are applied, and both exist because of specific ways such programmes go
wrong.

**Confidence intervals, never single-run comparisons.** A single A/B run on 200 turns can show a 40%
saving that is noise. Every claim carries an interval, and a gate passes only when the interval clears
the threshold — the same discipline the decision engine applies to its own estimates.

**Per workload class, never only in aggregate.** An optimization that wins 30% on analytics and loses
8% on long-document work nets positive in aggregate and ships, then degrades the workload nobody
benchmarked separately. Aggregate results are reported, but gates are evaluated per class.

---

## 2. The pyramid

```text
        ┌──────────────────────────────────────────┐
        │ Benchmark   10 workload classes          │  nightly + pre-promotion
        │             baseline vs optimized        │
        ├──────────────────────────────────────────┤
        │ Adversarial ~40 scenarios                │  per PR (fast subset), nightly (full)
        ├──────────────────────────────────────────┤
        │ Integration ~60 cases                    │  per PR
        ├──────────────────────────────────────────┤
        │ Unit        ~180 cases, no I/O           │  per commit, < 30 s
        └──────────────────────────────────────────┘
```

The unit layer is unusually large relative to the others, and deliberately so. The control plane is
pure functions from a state snapshot to a decision fragment (`architecture.md` §3), so scoring,
budgeting, ranking, routing, disposition and cost estimation are all testable with no network, no
model and no clock. That is the payoff of the plane separation, and the tests are where it is
collected.

---

## 3. Unit

No I/O, no model, deterministic clock. Approximately 180 cases, consolidating the test hooks declared
at the end of each Stage 3 and 4 document.

| Area | Representative assertions | Source |
|---|---|---|
| Token estimation | Sampled estimate within 15% of exact across the corpus; `bytes` always populated; `exact` flag propagates; safety multiplier applied to inexact counts | D3 §11 |
| Budget calculation | 10⁴ random configs × windows: allocations sum within the window, family stamped, reserved output task-sized | D4 §2 |
| Hysteresis | A sawtooth utilization trace produces no band oscillation inside the enter/exit gap | D4 §2 |
| Context scoring | Same snapshot → same scores across 10³ runs; cached items score a lower retention cost than identical uncached items | D4 §3 |
| Dispositions | `DISCARD` requires both conditions; `RETAIN` is the fall-through; ties resolve conservatively | D4 §4 |
| Compaction gate | Sign of `compaction_benefit` matches hand-computed values across a parameter sweep | D4 §5 |
| Payload rungs | Each rung's effect on a fixture corpus; no rung increases token count without revert | D3 §3 |
| Protected fields | Fuzzed payloads never lose an auth, tenancy, identity or join field at any aggressiveness | D3 §6 |
| Representation | For each shape, the chosen form is within 5% of the best available form | D3 §10 |
| Signature translation | Every enum, required flag and constraint survives; median reduction in the 40–60% band | D5 §5 |
| Tool projection | Same tool set renders byte-identically; additions never reorder | D5 §7 |
| Model selection | Quality floor applied before cost; Wilson bounds (19/20 fails a 0.90 floor, 190/200 passes) | D6 §3, §6 |
| Switching cost | Warm-prefix cases above break-even do not switch tier | D6 §11 |
| Escalation ladder | `RAISE_REASONING` precedes `ESCALATE_MODEL`; `RETRY` only for transient | D6 §8 |
| Reducer ordering | 10³ randomized completion orders → byte-identical committed state | D7 §6 |
| Trust algebra | For every input combination, derived trust equals the minimum | D7 §2 |
| Config validation | Each of V1–V23 rejected with the right error; a config with four faults reports four | D9 §6 |

**The determinism tests are the load-bearing ones.** Reducer ordering, disposition stability and
scoring reproducibility are what make every other test meaningful: a system whose output depends on
completion order cannot be tested at all, only sampled.

---

## 4. Integration

Real components, stubbed providers and stores. Approximately 60 cases.

| Area | What is exercised |
|---|---|
| MCP / tool invocation | Discovery, disclosure escalation, upstream parameter binding, side-effect and idempotency gates |
| Large database responses | Full ladder against 10 KB–4 MB payloads; budget respected; manifest resolves |
| Context compaction | Cache-invalidation accounting end to end; tail-first relief; batching at boundaries |
| Memory | Write at turn boundary, `as_of` filtering, supersession, contradiction surfacing, schema migration |
| Archive round-trip | `retrieveContext(ref, selector)` returns exactly what the manifest named |
| Code-execution bypass | Sandbox limits, egress cap, authorization inheritance, native fallback on failure |
| Sub-agent delegation | Minimal context construction, structured result, trust ceiling, reducer commit |
| Parallel branches | Fan-out, one failure, rollback, atomic commit |
| Model handoff | Budget recomputation across tokenizer families; byte backstop with no adapter |
| Provider probe | Capability mismatch handling; native-capability rule disabling middleware twins |
| Fail-open sweep | Fault-inject each of the 24 components in turn; every turn still completes correctly |

**The fail-open sweep is the single most important integration test in the suite.** INV-2 is the
promise that makes this system deployable in front of a production agent, and it is a promise about
24 components, not a design intention. The test injects a fault into each in turn and asserts the turn
completes with a correct answer, a recorded demotion and an attributed cost increase.

---

## 5. Adversarial

Roughly 40 scenarios, each targeting a specific way the system could be wrong rather than broken.

### Volume and shape

| Scenario | Must hold |
|---|---|
| 4 MB single-record payload | Terminates within budget; manifest resolves |
| 500,000 tiny records | Ladder does not degrade to O(n²); budget respected |
| Deeply nested (depth 40) | No stack exhaustion; representation falls back to opaque |
| Malformed / truncated JSON | `OPAQUE` shape; no field-level guessing |
| Payload with 3,000 distinct keys | Field relevance terminates; protected fields survive |
| Budget of 10 tokens | `enforceBudget` returns a reference and alarms |

### Loops and runaway

| Scenario | Must hold |
|---|---|
| `search → summarize → search → summarize` | Detected within `cycle_window`; escalated or terminated |
| Identical tool call repeated | Blocked at the second repeat |
| Recursive sub-agent spec | Terminates at `max_depth`; tree spend converges under attenuation |
| Sub-agent per record over 200 records | Spawn budget binds before the count cap |
| Context growth without progress | Detected within three turns |

### Correctness under reduction

| Scenario | Must hold |
|---|---|
| Question answerable only from dropped data | Agent retrieves via the manifest; does not confabulate |
| Summary mutation corpus (injected wrong numbers and IDs) | Every fabrication rejected by validation |
| Contradictory memory (A supersedes B, both relevant) | Superseded value never returned as current |
| Unresolvable conflict between two branches | Committed as disputed, not silently resolved |
| Truncated identifier / code fragment | Never produced — `never_compress` holds at every aggressiveness |

### Security

| Scenario | Must hold |
|---|---|
| Injection corpus through retrieval → summary → memory → rehydration | No directive executed; trust label survives every stage |
| Sub-agent given untrusted inputs | Result trust equals the minimum; cannot rise at merge |
| Cross-tenant cache probe | No key without a scope component; no hit across tenants |
| PII in a payload destined for the archive | Redacted before the write, never after |
| Erasure request | Archive, memory, caches, derived summaries and embeddings all purged |
| Generated bypass code attempting egress | Blocked by allowlist; `egress_bytes_max` enforced |

### Cache pathology

| Scenario | Must hold |
|---|---|
| Prompt-cache thrash (cosmetic rewrite each turn) | `assertAppendOnly` throws; test asserts the throw |
| Aggressive compaction on a warm prefix | Total cost never exceeds the unoptimized run |
| Tool churn every turn | Router refuses drops; cost stays bounded |
| Cache poisoning attempt (crafted key collision) | Scope and auth fingerprint prevent the hit |

**The injection corpus is the security test that matters most**, because it targets a failure the rest
of the architecture makes easy: summarization moves untrusted text into a compact, authoritative-looking
form and stores it. Each corpus item is traced through all five stages, and the assertion is on the
label at every hop, not only on the final behaviour.

---

## 6. Benchmark

The suite that gates promotion. Baseline versus optimized, per workload class, with intervals.

### Workload classes

The ten from the specification, each with a fixed task set and a defined success oracle.

| Class | Tasks | Success oracle |
|---|---|---|
| Conversational Q&A | 200 | Answer contains the required facts; no fabricated specifics |
| Tool-heavy research | 120 | All required sources consulted; claims traceable |
| Database analytics | 100 | Numeric result matches ground truth exactly |
| Coding agent | 100 | Tests pass; diff applies |
| Long-document analysis | 80 | Required passages cited; no invented quotations |
| Multi-step workflow | 80 | All steps completed in order; side effects correct |
| Multi-agent orchestration | 60 | Aggregate matches per-branch ground truth |
| Multimodal | 60 | Extracted values match |
| Long-running session | 40 (× 40 turns) | Facts from turn 3 still correct at turn 40 |
| High-cache-reuse session | 40 (× 20 turns) | As conversational, plus prefix stability |

**The long-running session class carries the test the others cannot.** A fact established early and
required late is exactly what selective retention, compaction and dependency closure exist to protect,
and no short-session benchmark detects its loss.

### Reported per class

```text
baseline vs optimized:  tokens · cost · p50/p95 latency · task success rate
                        tool-call count · context size · cache hit rate · answer quality
```

with:

* **Confidence intervals** on cost reduction and success rate. Wilson for proportions, bootstrap for
  cost ratios. `n ≥ 60` per class per arm, or the class reports "underpowered" rather than a number.
* **Baseline method: `MEASURED`.** Benchmark baselines are real pass-through runs, never estimated.
* **Paired runs.** The same task, same seed, same fixture on both arms, so variance from task
  difficulty cancels.

### Gates

```text
suite gate (promotion of the whole optimizer)
  cost reduction CI lower bound     ≥ 0.40 aggregate, > 0 in every class
  success rate drop CI upper bound  ≤ 0.02 in every class
  p95 latency increase              ≤ 0.10 in every class

per-optimization gate (promotion of one flag)
  token reduction in its category   significant at 95%
  success rate drop CI upper bound  ≤ 0.01 in every class
  p95 latency increase              ≤ 0.10 in every class
  shadow turns                      ≥ 500 before any live traffic
```

**A per-class floor of "> 0", not "≥ 0.40".** Requiring 40% in every class would block a mechanism
that delivers 45% on analytics and 3% on conversational — which is most of them. What must never
happen is a class going *backwards*, and that is what the floor encodes.

---

## 7. Fixtures and ground truth

| Fixture | Contents | Why it is needed |
|---|---|---|
| Payload corpus | ~400 real tool responses across shapes, 200 B – 4 MB | Representation, ladder and estimator tests |
| Schema corpus | ~80 JSON Schemas, trivial to pathological | Signature fidelity and savings |
| Injection corpus | ~150 prompt-injection strings in tool output, documents, filenames, field values | Trust propagation |
| Summary mutation corpus | 200 valid summaries with injected wrong numbers, IDs, entities | Validation efficacy |
| Session traces | 40 recorded sessions, 20–60 turns, with cache state | Compaction, retention, long-session behaviour |
| Config corpus | 200 configs, valid and invalid | All 23 load-time invariants |
| Cost fixture | Frozen price table and tokenizer versions | Deterministic economics tests |

**The cost fixture is frozen deliberately.** Economics tests assert arithmetic, not prices. Real
prices move, and a suite that fails when a provider changes its rate card is a suite people disable.

---

## 8. CI structure

| Stage | Runs | Duration | Blocks |
|---|---|---|---|
| Commit | Unit + config validation | < 30 s | Merge |
| PR | Integration + adversarial fast subset (~15) | < 8 min | Merge |
| Nightly | Full adversarial + benchmark on 3 primary classes | ~40 min | Nothing; reports |
| Pre-promotion | Full benchmark, all 10 classes, both arms | ~3 h | Flag promotion |
| Drift | Full benchmark on model, provider or tokenizer version change | ~3 h | Routing to the new version |

**The drift trigger is the one most easily forgotten.** A provider version bump can simultaneously
invalidate tokenizer assumptions, caching behaviour, pricing and structured-output fidelity — four
components at once, none of which fails a unit test. Until the suite passes on the new version, `C6`
pins the previous one.

---

## 9. What the tests cannot establish

Stated because a test strategy that claims completeness is the least trustworthy kind.

* **Real-workload savings.** The benchmark measures a fixed task set. Production savings depend on the
  actual baseline, session length distribution and workload mix — `economics.md` §5 shows the same
  code producing 62% and 15% on the same tasks with different baselines. Shadow mode on real traffic
  is the only source of that number.
* **Long-horizon memory correctness.** The longest benchmark session is 40 turns. Failures from
  memory drift, accumulated supersession or provenance loss over weeks are not reachable here.
* **Adversarial coverage.** The injection corpus tests known shapes. It establishes that the trust
  labels propagate, not that no injection succeeds.
* **Estimator calibration in production.** The suite runs against fixed fixtures where estimates are
  correct by construction. Bias appears against real distributions, which is why the bias monitor is a
  runtime component rather than a test.
* **Correctness of the cost model's assumptions.** Tests assert the arithmetic is implemented as
  specified. Whether `expected_remaining_turns` or `P(result changes the decision)` are good estimates
  is a production question, answered by attribution and bias monitoring.
