# Token Optimization & Context Management

Measurement tools for Claude Code token spend, a runtime skill, and a complete design specification
for the middleware neither of them is.

> **Use the minimum amount of model context and inference required to complete the task correctly.**

**→ [INSTALL.md](INSTALL.md)** to get started. Run the tools before installing anything else.

## What is actually built

Read this before the rest. The repository contains two very different things and conflating them
would waste your time.

| | State | Use it? |
|---|---|---|
| **`tools/`** — 4 measurement and reduction tools | **Working, tested** | Yes. Start here. |
| **`skill/`** — runtime skill + always-on rules | **Working, installed by copy** | Yes, after measuring |
| **`docs/`** — 17-document architecture specification | **Specification only** | Only if implementing it |
| **`config/`** — full parameter surface + JSON Schema | **Design artifact.** Nothing reads it | No |

The **middleware described in `docs/` does not exist as code** — roughly a third of one deliverable
(the reduction ladder, as `tools/reduce.py`) is implemented and the rest is pseudocode. That is not an
oversight: most of it has to run *inside* the request loop, and from outside a harness there is no
interception point. `docs/phase0-findings.md` explains where that boundary falls.

## What the tools will probably tell you

They were run against a real multi-project Claude Code corpus, and the result **contradicted
three of this project's own design assumptions**:

- **Cache hit rate was 95.4%, not the assumed 35%.** The mechanism credited with 60% of all modelled
  savings was already provided by the harness. Nothing to build.
- **A deferred mechanism was 42% of live spend.** Sub-agent delegation was ruled out on a cold-prefix
  argument; measured, sub-agents run at a 93.3% hit rate. The premise was false.
- **A predicted 7.1% saving was a $175 loss.** Cache-TTL selection, measured, went the other way —
  wrong by its whole magnitude *and its sign*.

What actually drove cost: **context volume**. Reads plus writes were 93% of spend, and both scale with
how many tokens are in the context rather than how well they are cached. **92% of prompt tokens sat in
turns past #100** — a turn late in a long session cost 6.4× an early one for identical work.

The transferable lesson is not any of those numbers. It is that all three errors came from reasoning
about a platform instead of observing it, and were only findable because the claims were written down
in a checkable form first. **Measure your own deployment.** Every number here is a property of one
setup and may invert on yours — that is exactly what happened to this project's.

## The short version, if you skip everything else

Measure first. Then: shorten your sessions, read files with offsets instead of whole, and bound
command output. On a mature harness the clever mechanisms are mostly already built for you, and
session length is the lever nobody is pulling.

## Layout

```
tools/                        WORKING CODE
  baseline_profile.py           where your tokens and money go
  context_growth.py             what is filling your context
  ttl_analysis.py               is your cache TTL earning its premium
  reduce.py                     the reduction ladder, as a pipe
skill/                        WORKING — installed by copying
  SKILL.md                      loaded on demand · ~695 tokens · ceiling 800
  references/                   8 files, loaded only when their situation arises
  CLAUDE.md                     the always-on rules · copy to ~/.claude/CLAUDE.md
  README.md                     the disclosure map and its token budget
config/                       DESIGN ARTIFACT — nothing reads it
  optimizer.defaults.yaml       shipped defaults, annotated
  optimizer.schema.json         structural validation
  profiles/                     per-workload deployment profiles
docs/                         SPECIFICATION — the design record
  architecture.md               D2   architecture, invariants, components
  decision-engine.md            §17  the per-turn decision procedure
  flow.md                       §21  six Mermaid diagrams
  configuration.md              D9   layering, 27 load-time invariants
  payload-middleware.md         D3   the reduction ladder
  context-manager.md            D4   budgeting, scoring, compaction
  tool-router.md                D5   discovery, disclosure, cache stability
  model-router.md               D6   model and reasoning routing
  subagent-manager.md           D7   delegation and the reducer
  economics.md                  D8   cost model and two scenarios
  observability.md              D10  metrics, events, traces, alerts
  test-strategy.md              D11  unit, integration, adversarial, benchmark
  anti-patterns.md              §24  ten optimizations that cost more
  mechanism-inventory.md        §24  verdicts: build, defer, omit
  roadmap.md                    D12  phases populated from the verdicts
```

## Documents

| File | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | **D2.** Nine invariants, the plane model, a 24-component catalogue with interfaces, core data structures, data flow, state management, lifecycle, concurrency, the failure/demotion matrix, ADRs, trade-off register. |
| [`docs/decision-engine.md`](docs/decision-engine.md) | **§17.** The `NO_OPTIMIZATION` fast path, the overhead budget and advisor scheduler, the resolved 16-question evaluation order, `decide()` pseudocode, tie-breaking, escalation, three worked examples. |
| [`docs/flow.md`](docs/flow.md) | **§21.** Six Mermaid diagrams: master architecture, turn state machine, decision engine, concurrency and reducer, projection and cache layering, failure demotion. |
| [`docs/configuration.md`](docs/configuration.md) | **D9.** Layering and precedence, provider awareness, twenty-seven load-time validation invariants with a validator, runtime-reconfiguration rules, profile comparison, worked rejections, a symptom-to-knob tuning table. |
| [`docs/payload-middleware.md`](docs/payload-middleware.md) | **D3.** The eight-rung reduction ladder as pseudocode, with a per-operation correctness table and a worked trace. |
| [`docs/context-manager.md`](docs/context-manager.md) | **D4.** Budgeting, scoring, the five-way disposition, compaction with cache-invalidation cost, archival, retrieval, summary validation, checkpoint and rollback. |
| [`docs/tool-router.md`](docs/tool-router.md) | **D5.** Discovery, ranking, disclosure, signature translation, cache-stability-aware projection, tool selection and cost estimation. |
| [`docs/model-router.md`](docs/model-router.md) | **D6.** Model and reasoning selection, cost and quality estimation, escalation, and the delegation gate — including the switching cost of abandoning a warm prefix. |
| [`docs/subagent-manager.md`](docs/subagent-manager.md) | **D7.** Spawning, minimal context, execution, result compression, the deterministic reducer, termination, and twelve named protections. |
| [`docs/economics.md`](docs/economics.md) | **D8.** The cost model, the three decision formulas, savings per optimization per workload, and two scenarios producing 62% and 15% from identical code. |
| [`docs/observability.md`](docs/observability.md) | **D10.** The turn ledger schema, metric definitions, traces, alerts, dashboards, cardinality discipline, and how attribution is computed. |
| [`docs/test-strategy.md`](docs/test-strategy.md) | **D11.** ~180 unit, ~60 integration and ~40 adversarial cases, plus a ten-class benchmark with per-workload gates. |
| [`docs/anti-patterns.md`](docs/anti-patterns.md) | **§24.** Ten commonly recommended optimizations that increase consumption, quantified, with the conditions under which each does pay. |
| [`docs/mechanism-inventory.md`](docs/mechanism-inventory.md) | **§24 right-sizing.** 78 mechanisms scored and given verdicts — 29 build, 7 defer, 12 omit — each omission with its reason and revisit condition. |
| [`docs/roadmap.md`](docs/roadmap.md) | **D12.** Phases 0–3 populated from the verdicts, with exit criteria, effort ratios and explicit stop conditions. |
| [`skill/README.md`](skill/README.md) | **D1.** The progressive-disclosure map, measured token budget, and the rules `SKILL.md` obeys. |

## Reading order

**If you have ten minutes:** `docs/roadmap.md` §10, then `docs/economics.md` §4 (where the money
actually is), then `docs/mechanism-inventory.md` §11 (what not to build).

**If you are implementing it:**

1. `skill/SKILL.md` — the whole system in 777 tokens, from the agent's point of view.
2. `docs/architecture.md` §2 — the nine invariants everything derives from.
3. `docs/flow.md` diagram 1 — the shape of the system on one page.
4. `docs/decision-engine.md` — how a single turn is decided.
5. `docs/roadmap.md` — what to build, in what order, and when to stop.

## Measured, not just modelled

Phase 0 has been run against a real multi-project Claude Code corpus. It contradicted three
of the design's load-bearing assumptions — see [`docs/phase0-findings.md`](docs/phase0-findings.md).

**The biggest modelled lever was already pulled.** Stable prefix ordering was credited with 60% of
savings on an assumed 35% baseline cache hit rate. The measured rate is **95.4%** — the harness
already does it. Four weeks of planned Phase 1 work, deleted by two days of measurement.

**A cost category worth 39% of the bill was missing from a 78-mechanism inventory.** Cache *writes*
were modelled as a switching tax; they are the second-largest line after reads.

**A deferred mechanism turned out to be 42% of live spend.** Sub-agent delegation was deferred on a
cold-prefix argument. Measured, sub-agents run at a **93.3%** hit rate — they are not cold, and the
verdict built on that premise was wrong.

**What actually drives cost:** reads plus writes are **93% of spend**, and both scale with context
volume rather than cache quality. Median prompt is 114,574 tokens. Cutting median context 25% saves
~24% of the bill. That is Phase 2 work, which the original roadmap ranked second.

The design was not careless — every verdict followed from assumptions written down where they could
be checked, which is the only reason the errors were findable. Run
[`tools/baseline_profile.py`](tools/baseline_profile.py) against your own transcripts before
believing any number in `docs/economics.md`.
