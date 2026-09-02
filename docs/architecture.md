# Deliverable 2 — Architecture

**Stage 1 of 5.** Component architecture, interfaces, responsibilities, data flow, state management,
lifecycle, failure modes, concurrency model.

Companion documents: [`decision-engine.md`](decision-engine.md) (§17), [`flow.md`](flow.md) (§21).

---

## 1. Scope and stance

### 1.1 What this system is

A **middleware layer** that sits between a primary agent and its providers, tools, memory and
sub-agents. It does not replace the agent loop; it decides, per turn, *what the next model request
should contain and how the work should be executed*, then accounts for what that decision cost and
what it saved.

### 1.2 What this system is not

* **Not a framework.** It is decoupled from any agent framework or provider. Provider-specific
  behaviour lives behind capability-probed adapters (C20, C6).
* **Not a dependency.** Per INV-2 it is always removable at runtime: any component may fail and the
  turn still executes, more expensively, on the pass-through path.
* **Not the source of truth.** Per INV-1 the conversation transcript is a rendered artifact. The
  system of record is the Canonical State Store (C17), memory (C18) and the archive (C19).

### 1.3 Terminology

Used exactly as defined in the source specification:

| Term | Meaning |
|---|---|
| **Token budget** | A hard limit on tokens for a category or operation. |
| **Threshold** | A utilization level that triggers a management action. |
| **Compaction** | Rewriting in-context content into a smaller in-context form. Potentially lossy; invalidates downstream cache. |
| **Externalization** | Moving content out of context to an external store, leaving a reference. Lossless; retrievable. |
| **Summarization** | A lossy form of compaction producing natural-language or structured digests. |

Three terms are added by this design and used consistently from here on:

| Term | Meaning |
|---|---|
| **Projection** | The concrete model request built for one turn from canonical state. Ephemeral. Rebuilt, never edited. |
| **Advisor** | A control-plane component the Decision Engine may consult. Each declares a cost estimate and is scheduled under the overhead budget. |
| **Demotion** | Automatic fallback of a failing component to a simpler, more expensive, always-correct behaviour. |

---

## 2. Invariants

Nine rules. Everything below is a consequence of one of them. A change that violates an invariant is
a redesign, not a tuning decision. Each is stated with its enforcement point and its adversarial test
hook, so Stage 5 (D11) has something concrete to test against.

| # | Invariant | Enforced by | Adversarial test (D11) |
|---|---|---|---|
| **INV-1** | **Projection.** The LLM context is a projection of agent state, never the source of truth. No component mutates canonical state by editing context; compaction rebuilds a projection. | C10 is the only writer of model requests; C17 is the only writer of canonical state. They share no mutable objects. | Corrupt a projection mid-turn; canonical state and the next turn must be unaffected. |
| **INV-2** | **Fail-open.** No failure in any optimization component may reduce agent availability. Every component has a demotion path to pass-through. | C0 wraps every advisor and executor in a demotion guard with a deadline. | Fault-inject each of the 24 components in turn; every turn must still complete correctly. |
| **INV-3** | **Metered overhead.** The optimizer's own cost is measured and capped. An optimization runs only when expected savings exceed its metered overhead. | C0 overhead meter plus the C8 advisor scheduler; C22 records `optimization_overhead` per turn. | Small-task suite: optimizer overhead must be near zero and `mode = NO_OPTIMIZATION`. |
| **INV-4** | **Single writer.** Shared state is written only at turn boundaries, only through the deterministic reducer, in dependency-then-dispatch order — never completion order. | C13 holds the sole write capability to C17/C18 during a turn. | Randomize branch completion order across 1,000 runs; final state must be byte-identical. |
| **INV-5** | **Trust monotonicity.** Trust never increases along a pipeline. A derived item's trust is the minimum trust of its inputs. Untrusted content never becomes an instruction by being summarized, extracted or stored. | C21 stamps every item; C12/C15/C18 propagate the minimum; C10 renders untrusted content inside data delimiters with its label. | Injection corpus through retrieval, summarization, memory and rehydration; no directive may be executed. |
| **INV-6** | **Accounted reduction.** Every reduction is recorded in a drop manifest naming what was removed, why, and how to recover it. Silently reduced data that looks complete is forbidden. | C12 emits `DropManifest`; C10 renders it; C22 logs it. | Reduce a payload, then ask a question answerable only by dropped data; the agent must retrieve, not confabulate. |
| **INV-7** | **Cache-aware mutation.** Any edit to a cached prefix is priced against cache-invalidation cost before it happens. | C8 computes `compaction_benefit`; C10 refuses non-append prefix edits without an approved `CompactionDecision`. | Warm-prefix session with an aggressive compaction policy; total cost must not exceed the unoptimized run. |
| **INV-8** | **Tokenizer coherence.** A budget is valid only for the tokenizer family it was computed with. Any model handoff forces recomputation; raw bytes travel alongside tokens as a tokenizer-independent bound. | C2 stamps `TokenCount.family`; C3 rejects a budget whose family differs from the target model's. | Route a near-full context across model families; no overflow, no silent truncation. |
| **INV-9** | **Correctness tie-break.** When a decision is a tie, or its estimate is uncertain beyond a configured band, resolve toward correctness: retain, validate, surface as disputed — never toward savings. | C8 `resolveTie()`; C9 returns confidence intervals, not point estimates. | Feed deliberately uncertain estimates; every resolution must take the conservative branch. |

---

## 3. Plane model

Components are grouped into four planes with a strict dependency direction. **Control depends on
State and Observability; Data depends on State; nothing depends on Control.** This is what makes
INV-2 achievable: deleting the entire control plane leaves a working, expensive agent.

```text
        +---------------------------------------------+
        |  CONTROL PLANE - decides, never executes    |
        |  pure functions over state snapshots        |
        +----------------------+----------------------+
                               | emits ExecutionPlan (immutable)
        +----------------------v----------------------+
        |  DATA PLANE - executes, never decides       |
        |  effectful; obeys the plan literally        |
        +----------------------+----------------------+
                               | reads / writes via the reducer
        +----------------------v----------------------+
        |  STATE PLANE - the source of truth          |
        +----------------------+----------------------+
                               | every plane emits events
        +----------------------v----------------------+
        |  OBSERVABILITY PLANE - measures, gates      |
        |  feeds estimates back into Control          |
        +---------------------------------------------+
```

Two properties follow:

1. **The control plane is testable without I/O.** Every advisor is a pure function from a state
   snapshot plus a cost table to a decision fragment. Unit tests (D11) need no network and no model.
2. **The feedback loop is explicit and single-directional.** Observability informs Control's
   estimates (realized savings per advisor, estimator bias); Control never reads the data plane
   directly. This prevents the classic failure where an optimizer's decisions depend on the results
   of its own optimizations through an unauditable loop.

### 3.1 Component map

| # | Component | Plane | Responsibility | Phase hint |
|---|---|---|---|---|
| C0 | Turn Orchestrator | Kernel | Owns the turn lifecycle, the overhead meter and every demotion. | 0 |
| C1 | Task Classifier | Control | Intent, complexity, risk class, workload class. | 3 |
| C2 | Token Estimator + Tokenizer Adapters | Control | Tokens and bytes per model family; exact where available. | 0 |
| C3 | Budget Manager | Control | Allocates and polices context, tool, modality and output budgets. | 2 |
| C4 | Context Scorer and Selector | Control | Scores items; assigns the five-way disposition. | 2 |
| C5 | Tool Registry and Router | Control | Candidate tools, disclosure level, cache-stable projections. | 3 |
| C6 | Model and Reasoning Router | Control | Model tier, reasoning effort, provider capability. | 3 |
| C7 | Delegation Planner | Control | Native vs router vs sub-agent vs parallel vs escalate. | 4 |
| C8 | Decision Engine | Control | Schedules advisors under budget; emits the `ExecutionPlan`. | 1 |
| C9 | Cost Model and VOI Evaluator | Control | Prices any candidate action; net information value. | 0 |
| C10 | Projection Builder | Data | Sole constructor of model requests; prefix order, breakpoints. | 1 |
| C11 | Execution Dispatcher | Data | Runs plan steps; enforces guards and idempotency. | 1 |
| C12 | Payload Middleware | Data | The reduction pipeline and its drop manifest. | 1 |
| C13 | Deterministic Reducer | Data | The only write path from parallel work to shared state. | 2 |
| C14 | Code Execution Adapter | Data | Sandboxed code-execution bypass. | 3 |
| C15 | Sub-Agent Manager | Data | Spawns, bounds and compresses sub-agent work. | 4 |
| C16 | Failure Controller | Data | Checkpoints, rollback, retries, circuit breakers, loop guards. | 1 |
| C17 | Canonical State Store | State | Task state, decisions, pending actions. Source of truth. | 2 |
| C18 | Memory Services | State | Working / episodic / semantic memory with temporal validity. | 2 |
| C19 | Raw Archive | State | Externalized raw logs, payloads, artifacts. | 2 |
| C20 | Cache Fabric | State | Schema, result, semantic, prefix and retrieval caches. | 1 |
| C21 | Trust and Security Envelope | Cross-cutting | Trust labels, tenant scoping, sanitization, erasure. | 1 |
| C22 | Ledger | Observability | Token and cost accounting; baseline counterfactuals. | 0 |
| C23 | Estimator Bias Monitor | Observability | Estimate-versus-actual; alarms on drift. | 0 |
| C24 | Gate Runner and Shadow Harness | Observability | Regression gates, shadow/A-B, per-workload reporting. | 0 |

Phase hints anticipate Deliverable 12 but do **not** constitute the mechanism inventory. Verdicts are
produced in Stage 5 and are per-deployment: a component that is Phase 1 for a database-analytics
agent may be `OMIT` for a conversational assistant.

---

## 4. Core data structures

These types are the contract between planes. Later stages implement functions over them; they do not
redefine them. Notation is TypeScript for readability, not a language commitment.

### 4.1 Identity, trust and scope

```ts
type TenantId = string; type UserId = string;
type TaskId = string;  type TurnId = string; type ItemId = string;

// Ascending order. A derived item's level is the minimum over its inputs (INV-5).
type TrustLevel = 'EXTERNAL' | 'TOOL' | 'USER' | 'SYSTEM';
const TRUST_ORDER: TrustLevel[] = ['EXTERNAL', 'TOOL', 'USER', 'SYSTEM'];

interface TrustLabel {
  level: TrustLevel;
  derived: boolean;          // produced by our own models rather than received verbatim
  derived_from: ItemId[];    // the inputs whose minimum set `level`
  instruction_like: boolean; // C21 flagged imperative text inside EXTERNAL content
}

interface Scope { tenant: TenantId; user?: UserId; task?: TaskId }

interface AuthContext {
  principal: string; roles: string[]; grants: string[];
  obtained_at: ISO8601; expires_at?: ISO8601;
}
```

`instruction_like` is deliberately a *flag*, not a filter. C21 detects imperative text inside
`EXTERNAL` content and marks it; C10 renders it inside data delimiters with its label. The agent is
told such text is data. Nothing is silently deleted, because deletion is itself a correctness risk
and because INV-6 forbids unaccounted reduction.

### 4.2 Provenance and temporal validity

```ts
interface Provenance {
  source: string;                  // 'tool:jira.search' | 'conv:turn_183' | 'memory:semantic'
  pointer: ArchiveRef | ConvRef;   // always resolvable back to the original
  trust: TrustLabel;
  confidence: number;              // 0..1
  scope: Scope;
  auth: AuthContext;
  created_at: ISO8601; updated_at: ISO8601;
  valid_from: ISO8601; valid_until?: ISO8601;  // semantic freshness, distinct from cache TTL
  supersedes?: ItemId[];           // this item replaces those; they are never returned as current
}
```

**Semantic freshness is not cache TTL.** A cached value may be byte-fresh and semantically stale.
`valid_until` and `supersedes` govern whether a fact may be *returned as current*; cache TTL governs
whether it must be *re-fetched*. C18 checks both; conflating them is the source of the classic
"preferred address = A" bug the specification calls out.

### 4.3 Tokens and cost

```ts
type TokenizerFamily = 'anthropic' | 'openai' | 'llama' | 'generic';

interface TokenCount {
  family: TokenizerFamily;
  tokens: number;
  bytes: number;     // tokenizer-independent sanity bound; survives model handoff (INV-8)
  exact: boolean;    // false when produced by an approximate estimator
}

interface TokenCost {
  llm_input: number; llm_output: number; llm_thinking: number;
  cache_read: number; cache_write: number;
  usd_llm: number;
  usd_non_token: number;   // API charges, database compute, search, execution, storage, transfer
  latency_p50_ms: number; latency_p95_ms: number;
}

// Estimates are intervals, never points (INV-9).
interface Estimate<T> { lo: T; mid: T; hi: T; confidence: number; basis: 'PRIOR'|'EWMA'|'MEASURED' }
```

`usd_non_token` is a first-class field, not an afterthought. It is what makes "the cheapest operation
is not the one with the fewest LLM tokens" mechanically true rather than merely stated: a database
scan that returns 200 tokens can cost more than a tool call returning 8,000.

### 4.4 Context items and scoring

```ts
type ItemKind =
  | 'SYSTEM' | 'SKILL' | 'TOOL_SCHEMA' | 'MEMORY'
  | 'MESSAGE' | 'TOOL_RESULT' | 'ARTIFACT' | 'MEDIA' | 'STATE';

// Stability drives prefix ordering; it is not a guess, it is declared at creation.
type Stability = 'STATIC' | 'SESSION' | 'TASK' | 'TURN';

interface ContextItem {
  id: ItemId;
  kind: ItemKind;
  stability: Stability;
  render: () => Renderable;    // lazy: large payloads never materialize inside the planner
  size: TokenCount;
  provenance: Provenance;
  deps: ItemId[];              // edges of the context dependency graph
  canonical_ref?: ArchiveRef;  // set whenever the in-context form is lossy
  score?: ContextScore;
  disposition?: Disposition;
}

type Disposition = 'RETAIN' | 'COMPRESS' | 'EXTERNALIZE' | 'DISCARD' | 'RETRIEVE_LATER';

interface ContextScore {
  relevance: number; importance: number; recency_factor: number;
  dependency_factor: number; future_value: number;
  score: number;                              // the product of the five
  retention_cost: Estimate<TokenCost>;        // keeping it for expected_remaining_turns
  reconstruction_cost: Estimate<TokenCost>;   // fetching it again after dropping it
}
```

`render` being a thunk is a load-bearing detail. The control plane reasons over sizes, scores and
provenance; it must never hold a 4 MB payload in memory to decide whether to keep 200 tokens of it.

### 4.5 The drop manifest

```ts
interface DropEntry {
  what: string;         // 'records 51..4312' | 'field: body_html' | 'attachments[]'
  reason: 'IRRELEVANT' | 'DUPLICATE' | 'LOW_RANK' | 'TRUNCATED' | 'BUDGET' | 'SUMMARIZED';
  recoverable: boolean;
  ref?: ArchiveRef;
  retrieve_with?: ToolCallSpec;   // the exact call that restores it
}

interface DropManifest {
  entries: DropEntry[];
  bytes_dropped: number;
  tokens_saved: number;
  lossy: boolean;                 // true if any entry is SUMMARIZED or TRUNCATED
}
```

The manifest is rendered into context in a compact, uniform form so the agent recognizes a gap
instead of reasoning as if the data were complete:

```text
[reduced 4,312 -> 50 records (low_rank); full set at result:9f3a1c;
 retrieve_with fetch_result("9f3a1c", range=51..4312)]
```

Roughly 30 tokens to prevent a class of confident-wrong answers. This is the cheapest correctness
mechanism in the system and it is Phase 1 for that reason.

### 4.6 The execution plan

The Decision Engine's sole output and the only thing the data plane obeys. It is immutable once
emitted; a change requires a new plan and a new ledger entry.

```ts
interface ExecutionPlan {
  turn: TurnId;
  mode: 'NO_OPTIMIZATION' | 'OPTIMIZED' | 'DEMOTED';
  projection: ProjectionSpec;
  steps: ExecutionStep[];
  model: ModelSpec;
  budgets: BudgetSpec;
  compaction: CompactionDecision;
  cache: CacheSpec;
  guards: GuardSpec;
  trace: DecisionTrace[];              // one entry per question answered, for telemetry
  overhead: TokenCost;                 // what producing this plan cost (INV-3)
  estimated: {
    baseline: Estimate<TokenCost>;     // counterfactual pass-through cost of this same turn
    optimized: Estimate<TokenCost>;
    savings: Estimate<TokenCost>;
  };
}

type ExecutionStep =
  | { k: 'NATIVE_TOOL'; tool: ToolId; args: unknown; budget: TokenBudget; idem: IdempotencyKey }
  | { k: 'CODE_BYPASS'; program: ProgramSpec; budget: TokenBudget; sandbox: SandboxSpec }
  | { k: 'SUBAGENT';    spec: SubAgentSpec }
  | { k: 'PARALLEL';    branches: ExecutionStep[]; reducer: ReducerSpec; speculative?: SpecSpec }
  | { k: 'ANSWER' };

interface ModelSpec {
  model: ModelId; tier: 1 | 2 | 3 | 4;
  reasoning: 'NONE' | 'LOW' | 'MEDIUM' | 'HIGH' | { thinking_tokens: number };
  max_tokens: number;                  // tuned per operation class, never the provider default
  stop?: string[];
  structured_output?: JSONSchema;
  stream_early_exit?: EarlyExitPredicate;
  tokenizer: TokenizerFamily;          // budgets are only valid against this (INV-8)
}

interface ProjectionSpec {
  layers: ProjectionLayer[];           // ordered most-static first
  breakpoints: CacheBreakpoint[];
  prefix_hash: string;                 // identity of the cacheable prefix
  total: TokenCount;
  manifest: DropManifest;
}

interface CompactionDecision {
  action: 'DEFER' | 'COMPACT_NOW';
  targets: ItemId[];
  net_benefit: Estimate<number>;       // USD; must be positive to compact (INV-7)
  reason: string;
  at_boundary: 'SESSION_START' | 'TASK_TRANSITION' | 'TTL_LAPSE' | 'EMERGENCY';
}

interface GuardSpec {
  max_iterations: number; max_tool_calls: number; max_subagents: number;
  max_delegation_depth: number; max_retries: number;
  max_context_growth_tokens: number; token_budget: number; wall_clock_ms: number;
}
```

---

## 5. Component catalogue

Each entry gives responsibility, interface, and the demotion behaviour required by INV-2. What is
fixed here is the boundary; the bodies are in [`payload-middleware.md`](payload-middleware.md),
[`context-manager.md`](context-manager.md), [`tool-router.md`](tool-router.md),
[`model-router.md`](model-router.md) and [`subagent-manager.md`](subagent-manager.md).

### Kernel

#### C0 — Turn Orchestrator

Owns the turn. It is the only component that knows about *time* and the only one permitted to abandon
work. It runs the overhead meter, applies deadlines, and demotes.

```ts
interface TurnOrchestrator {
  runTurn(input: TurnInput): Promise<TurnResult>;
  meter(): OverheadMeter;                       // spent / cap / remaining, live
  guard<T>(component: ComponentId, deadline: Ms, f: () => Promise<T>, fallback: T): Promise<T>;
}

interface OverheadMeter {
  cap: TokenCost; spent: TokenCost;
  canAfford(estimate: Estimate<TokenCost>): boolean;
  charge(component: ComponentId, actual: TokenCost): void;
}
```

`guard()` is where INV-2 lives. Every call into an advisor or executor passes through it with an
explicit fallback value. An advisor that throws, times out, or exceeds its declared cost is skipped
and the plan is built without its contribution. The turn never fails because an optimizer failed.

**Demotion:** if C0 itself fails, the caller's own retry produces a plain pass-through turn. C0 holds
no state that a restart cannot rebuild from C17.

### Control plane

#### C1 — Task Classifier

Produces the labels every other advisor keys off. Deterministic rules first; a Tier-2 model only when
rules are inconclusive and the overhead budget allows.

```ts
interface TaskClassifier {
  classify(turn: TurnInput, state: StateSnapshot): Estimate<TaskClass>;
}

interface TaskClass {
  intent: string;
  workload: 'QA' | 'RESEARCH' | 'ANALYTICS' | 'CODING' | 'LONG_DOC'
          | 'WORKFLOW' | 'ORCHESTRATION' | 'MULTIMODAL';
  complexity: 'TRIVIAL' | 'SIMPLE' | 'MODERATE' | 'COMPLEX';
  risk: 'LOW' | 'MEDIUM' | 'HIGH';   // HIGH biases every later decision toward retention (INV-9)
  data_heavy: boolean;               // the primary trigger for the code-execution bypass
  independent_subtasks: number;
}
```

`workload` is not cosmetic: it is the partition key for every benchmark in D11 and for the EWMA
savings priors the advisor scheduler uses. An optimization that helps analytics and hurts long-doc
work must be visible as exactly that.

**Demotion:** returns a `MODERATE / MEDIUM / unknown-workload` label with `confidence: 0`, which the
tie-break rule (INV-9) turns into conservative downstream choices.

#### C2 — Token Estimator and Tokenizer Adapters

```ts
interface TokenEstimator {
  count(content: Renderable, family: TokenizerFamily): TokenCount;   // exact where available
  estimate(content: Renderable, family: TokenizerFamily): Estimate<TokenCount>;
  adapters(): Record<TokenizerFamily, TokenizerAdapter>;
  recomputeFor(budget: BudgetSpec, target: TokenizerFamily): BudgetSpec;  // INV-8
}
```

Every `TokenCount` carries `bytes` regardless of family. Bytes are the overflow backstop during a
model handoff: if the target family's adapter is unavailable, C3 falls back to a conservative
bytes-per-token bound rather than reusing the source family's token count.

**Demotion:** approximate character-class estimator with a configured safety multiplier
(default 1.25). Marked `exact: false`, which C23 tracks separately so approximation bias is visible.

#### C3 — Budget Manager

```ts
interface BudgetManager {
  allocate(model: ModelSpec, task: TaskClass, state: StateSnapshot): BudgetSpec;
  utilization(p: ProjectionSpec): number;
  band(u: number): 'NORMAL' | 'PRUNE' | 'COMPACT' | 'AGGRESSIVE' | 'EMERGENCY';
  budgetFor(tool: ToolId, task: TaskClass): TokenBudget;
  budgetForModality(m: Modality, task: TaskClass): TokenBudget;
  assertCoherent(b: BudgetSpec, m: ModelSpec): void;   // throws on tokenizer mismatch (INV-8)
}

interface BudgetSpec {
  family: TokenizerFamily;
  window: number;
  reserved_output: number; reserved_system_skill: number; safety_margin: number;
  working: number; tool_results: number; memory: number;
  per_tool: Record<ToolId, TokenBudget>;
  per_modality: Record<Modality, TokenBudget>;
  thresholds: Hysteresis[];   // enter > exit, validated at load time
}

interface Hysteresis { band: string; enter: number; exit: number }
```

**Hysteresis is mandatory, not optional.** A single threshold makes a session oscillate: compaction
drops utilization below the line, the next tool result pushes it back over, and the system compacts
again — repeatedly summarizing the same content, which the specification explicitly forbids, while
invalidating the prefix cache every time. Separate enter and exit levels (defaults: enter 0.70 /
exit 0.60) break the cycle. Load-time validation rejects any pair where `enter <= exit`.

**Demotion:** falls back to a single static conservative allocation derived from the model's window
with a doubled safety margin. Correct, wasteful, never overflowing.

#### C4 — Context Scorer and Selector

```ts
interface ContextSelector {
  score(item: ContextItem, ctx: ScoringContext): ContextScore;
  decide(item: ContextItem, budget: BudgetSpec): Disposition;
  select(items: ContextItem[], budget: BudgetSpec): SelectionResult;
  dependencyClosure(keep: ItemId[], graph: DepGraph): ItemId[];
}
```

Two rules govern `decide()` and both are consequences of the invariants:

1. **The choice is an economic comparison, not a threshold on `score`.** `RETRIEVE_LATER` wins when
   `reconstruction_cost < retention_cost` over the expected remaining turns; `EXTERNALIZE` wins when
   the item is large and its future need is uncertain; `DISCARD` requires *both* low future value and
   cheap reconstruction. A 20 KB response summarized to 2 KB is the wrong answer when one field is
   needed five turns later — the right answer is a reference plus on-demand field retrieval.
2. **Dependency closure runs after selection, before budgeting.** An item with a near-zero score that
   a retained decision depends on is promoted to `RETAIN`. This is what stops the deletion of the
   apparently irrelevant message that a later decision was built on.

**Demotion:** `RETAIN` everything that fits the working budget in recency order; `EXTERNALIZE` the
overflow with references. No scoring, no loss, larger context.

#### C5 — Tool Registry and Router

```ts
interface ToolRouter {
  candidates(task: TaskClass, k: number): ToolCandidate[];
  disclosureLevel(t: ToolId, task: TaskClass): 0 | 1 | 2 | 3;
  project(cands: ToolCandidate[], cache: CacheState): ToolProjection;
  signature(schema: JSONSchema): CompactSignature;   // 40-60% fewer tokens than raw JSON Schema
  estimateToolCost(t: ToolId, args: unknown): Estimate<TokenCost>;
}

interface ToolProjection {
  tools: Array<{ id: ToolId; level: 0|1|2|3; rendered: Renderable; size: TokenCount }>;
  prefix_stable: boolean;        // false means this projection invalidates the cached prefix
  churn_cost: Estimate<number>;  // USD cost of that invalidation over expected remaining turns
}
```

The registry holds metadata separately from full schemas, so candidate selection never loads what it
is deciding about. Disclosure escalates 0 to 3 only on demand.

**`prefix_stable` is the component's most important output.** Progressive disclosure and prompt
caching pull in opposite directions: per-task schema churn saves schema tokens and destroys the
cached prefix. C5 therefore returns both the projection and the price of adopting it, and C8 decides.
For short sessions or hot tool sets the correct answer is frequently a slightly larger but *stable*
tool set loaded once.

`signature()` applies only to the *context representation* of a schema. Provider-native tool calling
may still require full JSON Schema at the API layer; the translation is for registry text, Level 0-2
renderings, and code-execution imports.

**Demotion:** project all candidate tools at Level 2 in a fixed, stable order. Maximum schema tokens,
maximum cache stability, zero routing risk.

#### C6 — Model and Reasoning Router

```ts
interface ModelRouter {
  select(task: TaskClass, budget: BudgetSpec, cache: CacheState): ModelSpec;
  reasoningEffort(task: TaskClass, step: ExecutionStep): ModelSpec['reasoning'];
  estimateModelCost(m: ModelSpec, p: ProjectionSpec): Estimate<TokenCost>;
  estimateQuality(m: ModelSpec, task: TaskClass): Estimate<number>;
  shouldEscalate(f: FailureRecord, current: ModelSpec): EscalationDecision;
  capabilities(m: ModelId): ProviderCapabilities;
}

interface ProviderCapabilities {
  prompt_cache: { supported: boolean; ttl_s: number; max_breakpoints: number; min_prefix_tokens: number };
  cached_input_discount: number;      // e.g. 0.9 means cached input costs 10%
  structured_output: boolean; function_calling: boolean; batch: boolean;
  window: number; tokenizer: TokenizerFamily;
  native: string[];                   // optimizations the provider already performs
}
```

`capabilities().native` is what enforces design principle 26. Before any middleware optimization runs,
C8 checks whether the provider already does it; if so the middleware equivalent is disabled unless a
benchmark in C24 shows the middleware wins. Load-time config validation makes this a hard rule.

Reasoning effort is a first-class routing axis, not a model attribute. Classification, extraction,
filtering and formatting get `NONE`; planning, ambiguity and high-risk actions get a budget.
Mechanical sub-agent work never gets extended thinking.

**Demotion:** the configured default tier and `reasoning: MEDIUM`. Expensive, always capable.

#### C7 — Delegation Planner

```ts
interface DelegationPlanner {
  plan(task: TaskClass, ctx: StateSnapshot, cache: CacheState): DelegationDecision;
}

type DelegationDecision =
  | { k: 'NATIVE' }
  | { k: 'LIGHTWEIGHT_ROUTER'; model: ModelSpec }
  | { k: 'SUBAGENT'; spec: SubAgentSpec }
  | { k: 'PARALLEL_SUBAGENTS'; specs: SubAgentSpec[]; reducer: ReducerSpec }
  | { k: 'ESCALATE'; model: ModelSpec };
```

The economic test includes cache state, which is the term most implementations omit: delegating to a
cold-cache sub-agent while the primary's prefix is warm can cost more than doing the work natively
even when the sub-agent's raw token count is lower.

**Demotion:** `NATIVE`. Always correct, sometimes expensive, never recursive.

#### C8 — Decision Engine

Full treatment in [`decision-engine.md`](decision-engine.md).

```ts
interface DecisionEngine {
  decide(turn: TurnInput, state: StateSnapshot, meter: OverheadMeter): ExecutionPlan;
  fastPathEligible(turn: TurnInput, state: StateSnapshot): boolean;
  scheduleAdvisors(cap: TokenCost, priors: SavingsPriors): AdvisorSchedule;
  resolveTie(a: Option, b: Option, conf: number): Option;   // INV-9
}
```

**Demotion:** emits `mode: 'DEMOTED'` with a pass-through projection, the default model, no reduction.

#### C9 — Cost Model and VOI Evaluator

```ts
interface CostModel {
  price(step: ExecutionStep, m: ModelSpec, p: ProjectionSpec): Estimate<TokenCost>;
  baseline(turn: TurnInput, state: StateSnapshot): Estimate<TokenCost>;   // the counterfactual
  compactionBenefit(targets: ItemId[], cache: CacheState, horizon: number): Estimate<number>;
  netInformationValue(op: CandidateOperation, ctx: StateSnapshot): Estimate<number>;
}
```

`netInformationValue` implements the specification's formula with explicitly crude, configurable
inputs — task-class priors, historical hit rates, result-size history. The point is not precision.
The point is that the engine answers *"is this retrieval worth 4,000 tokens plus its API charge?"*
rather than *"do I have 4,000 tokens left?"*, and that `usd_non_token` participates in the answer.

**Demotion:** returns a wide interval with `confidence: 0`, which INV-9 converts into the
conservative branch everywhere it is consulted.

### Data plane

#### C10 — Projection Builder

The **only** component that constructs a model request. This single-writer property is what makes
INV-1 and INV-7 enforceable rather than aspirational.

```ts
interface ProjectionBuilder {
  build(plan: ExecutionPlan, state: StateSnapshot): ModelRequest;
  layerOrder(): Stability[];             // STATIC, SESSION, TASK, TURN
  placeBreakpoints(caps: ProviderCapabilities, layers: ProjectionLayer[]): CacheBreakpoint[];
  assertAppendOnly(prev: ProjectionSpec, next: ProjectionSpec): void;   // throws without approval
}
```

Layer order is fixed, most-static first:

```text
System instructions -> Skill instructions -> Tool schemas
-> Long-term memory -> Conversation history (append-only) -> Current turn
```

`assertAppendOnly` throws when a new projection differs from the previous one anywhere before the
last breakpoint, unless the plan carries an approved `CompactionDecision`. Cosmetic rewrites of
earlier turns — reformatting, renumbering, tidying whitespace — are the most common accidental cause
of full-prefix invalidation, and this assertion is what catches them in development rather than in a
billing report.

**Demotion:** build the full pass-through projection with no reduction and a single breakpoint after
the system layer.

#### C11 — Execution Dispatcher

```ts
interface Dispatcher {
  run(step: ExecutionStep, guards: GuardSpec, ck: Checkpoint): Promise<StepResult>;
  fanOut(branches: ExecutionStep[], reducer: ReducerSpec): Promise<ReducedResult>;
}
```

Obeys the plan literally. It contains no policy: if the dispatcher would need to decide something,
that decision belongs in C8 and is a design bug. Every step carries an idempotency key so that a
retry or a cache reuse can never double-execute a mutation.

**Demotion:** sequential execution of every step, no parallelism, no speculation.

#### C12 — Payload Middleware

```ts
interface PayloadMiddleware {
  reduce(raw: RawPayload, budget: TokenBudget, task: TaskClass): ReducedPayload;
}

interface ReducedPayload {
  content: Renderable;
  representation: 'JSON' | 'YAML' | 'KV' | 'CSV' | 'LINES' | 'CUSTOM' | 'SUMMARY_REF';
  size: TokenCount;
  manifest: DropManifest;      // never optional (INV-6)
  trust: TrustLabel;           // carried through unchanged from the raw payload (INV-5)
  archive_ref: ArchiveRef;     // the full original, always written before reduction
}
```

Ordering of the reduction ladder is fixed and escalates only as far as the budget requires:

```text
remove nulls/empties -> remove irrelevant fields -> remove duplicates -> rank records
-> truncate low-value content -> summarize repetitive content -> project columns
-> paginate -> externalize and return summary + reference
```

Representation is chosen by measurement, not by rule. YAML is not assumed to beat JSON: on payloads
dominated by long string values YAML's block scalars and indentation frequently cost *more*, and the
choice depends on nesting depth, key repetition, field-name length and the target tokenizer. C12
estimates two or three candidate representations with C2 and picks the smallest that preserves the
required semantics. Bodies are Deliverable 3, Stage 3.

**Demotion:** pass the raw payload through, subject only to the hard tool budget with a truncation
entry in the manifest. More tokens, no information loss beyond the recorded truncation.

#### C13 — Deterministic Reducer

```ts
interface Reducer {
  reduce(branches: BranchResult[], spec: ReducerSpec): ReducedResult;
  commit(r: ReducedResult, tx: StateTransaction): void;   // the sole write path (INV-4)
}

interface ReducerSpec {
  order: 'DEPENDENCY_THEN_DISPATCH';   // never completion order
  dedupe: DedupeRule[];
  conflict: ConflictRule[];            // explicit precedence; ties surface as disputed (INV-9)
  atomic: true;
}
```

Parallel branches and sub-agents never touch working memory, semantic memory or task state. They
return structured results to the reducer, which reconciles in a defined order, deduplicates
overlapping facts, resolves conflicts by explicit precedence, and performs a single atomic write at
the aggregation point. Determinism here is what makes a multi-branch turn reproducible and therefore
debuggable; ordering by completion makes a system whose bugs cannot be replayed.

**Demotion:** sequential execution upstream means the reducer receives one branch, which it commits
directly. The write path is unchanged.

#### C14 — Code Execution Adapter

```ts
interface CodeExecutionAdapter {
  importable(tools: ToolId[]): ModuleManifest;   // schemas discovered on demand, not in context
  run(p: ProgramSpec, sandbox: SandboxSpec): Promise<ExecResult>;
}

interface SandboxSpec {
  cpu_ms: number; memory_mb: number; wall_ms: number;
  network: 'NONE' | 'ALLOWLIST'; allowlist?: string[];
  fs: 'NONE' | 'SCRATCH';
  scope: Scope; auth: AuthContext;    // identical boundaries to native execution (§19)
  egress_bytes_max: number;
}
```

The highest-leverage externalization pattern in the specification. Intermediate data lives in the
execution environment and never enters context; only final aggregates return. On data-heavy tasks
this beats any payload-filtering pipeline by an order of magnitude, because filtering reduces a
payload that still has to cross the context boundary while the bypass never lets it cross.

`egress_bytes_max` is the backstop: a bypass that returns 40,000 tokens has failed at its one job,
and the adapter enforces the return-size contract rather than trusting the generated program.

**Demotion:** the plan step is rewritten as a sequence of native tool calls with C12 reduction. This
is the expensive path the bypass exists to avoid, and it is always available.

#### C15 — Sub-Agent Manager

```ts
interface SubAgentManager {
  spawn(spec: SubAgentSpec, depth: number): Promise<SubAgentHandle>;
  prepareMinimalContext(spec: SubAgentSpec, state: StateSnapshot): ProjectionSpec;
  compressResult(r: unknown, schema: JSONSchema): StructuredResult;
  terminate(h: SubAgentHandle, reason: string): void;
}

interface SubAgentSpec {
  role: string;                  // purpose-scoped; never the primary's own instructions
  tools: ToolId[];               // only what the subtask needs
  model: ModelSpec;              // reasoning: NONE for mechanical work
  inputs: ItemId[];              // explicit allowlist, never "the conversation"
  result_schema: JSONSchema;     // structured output enforced, not requested
  trust_ceiling: TrustLevel;     // min over inputs (INV-5)
  max_depth: number; max_tokens: number; wall_ms: number;
}
```

Three prohibitions are structural rather than advisory, because each is a way the delegation saves
nothing:

* **`inputs` is an allowlist of item IDs.** There is no API by which a sub-agent receives the
  primary's context. A summarizer that receives the full context it was spawned to shrink is a
  net loss, and the interface makes it unexpressible.
* **The optimization skill is never loaded into a sub-agent.** The primary optimizes *for* the
  sub-agent. An optimizer inside every worker recreates the overhead delegation was meant to avoid.
* **`trust_ceiling` is computed, not declared.** Result trust is the minimum trust of the inputs.
  Delegation cannot launder untrusted content into trusted memory.

**Demotion:** execute natively in the primary. Always available, sometimes more expensive.

#### C16 — Failure Controller

```ts
interface FailureController {
  checkpoint(state: WorkingState): Checkpoint;
  rollback(ck: Checkpoint): WorkingState;
  classify(e: unknown): ErrorClass;     // DETERMINISTIC | TRANSIENT | CAPABILITY | FATAL
  normalize(e: unknown): OneLineError;  // class, cause, suggested fix - no stack traces
  next(f: FailureRecord, guards: GuardSpec): 'FIX' | 'RETRY' | 'ESCALATE_REASONING'
                                           | 'ESCALATE_MODEL' | 'FALLBACK_TOOL' | 'ABORT';
  loopDetector(): LoopDetector;
  breaker(t: ToolId): CircuitBreaker;
}
```

Checkpoints are taken before every speculative or failure-prone step. On failure, working context
rolls back to the pre-execution checkpoint and receives a single normalized line; raw stack traces
and verbose error payloads go to C19 with a reference, retrievable only if diagnosis needs them.
**Failed branches leave no residue in active context** — this is both a token measure and a
correctness measure, since a failed branch's partial output is exactly the kind of misleading
material that produces confidently wrong reasoning three turns later.

`next()` never returns `RETRY` for the same operation with unchanged inputs. The escalation ladder is
ordered by cost: fix deterministically without an LLM, then retry with changed input, then raise
reasoning effort on the same model, then escalate model, then fall back to another tool, then abort.

**Demotion:** fixed retry count with exponential backoff and no classification. Correct, blunter.

### State plane

#### C17 — Canonical State Store

```ts
interface CanonicalState {
  snapshot(task: TaskId): StateSnapshot;              // immutable read for the control plane
  transaction(task: TaskId): StateTransaction;        // write path, reducer-only
  resumptionBundle(task: TaskId): ResumptionBundle;   // compact rehydration, not raw replay
  schema_version: number;
}

interface ResumptionBundle {
  objective: string;
  decisions: Decision[];
  pending_actions: Action[];
  key_facts: Fact[];
  open_refs: ArchiveRef[];
  version: number;
}
```

Session resumption rehydrates from this bundle. Replaying raw history is the single most expensive
resumption strategy available and it is never used.

**Demotion:** read-only mode. Turns still execute; nothing new persists; the session degrades to
stateless behaviour rather than failing.

#### C18 — Memory Services

```ts
interface MemoryServices {
  working(task: TaskId): WorkingMemory;
  episodic(scope: Scope): EpisodicMemory;
  semantic(scope: Scope): SemanticMemory;
  retrieve(q: MemoryQuery): MemoryHit[];   // filters superseded and expired items
  extract(turn: TurnRecord): MemoryWrite[];  // runs at turn boundaries only
  migrate(from: number, to: number): MigrationReport;
}

interface MemoryQuery {
  text?: string; scope: Scope; auth: AuthContext;
  as_of: ISO8601;                 // semantic freshness, not cache freshness
  min_trust?: TrustLevel;
  budget: TokenBudget;
}
```

`as_of` is mandatory on every query. A superseded fact is never returned as current merely because it
is compact and looks relevant. Contradictions resolve by provenance and recency; unresolvable ones
surface as disputed rather than silently picking one (INV-9).

Memory schema is versioned because persistent memory outlives deployments. `migrate()` is part of the
interface, not an operational afterthought.

**Demotion:** retrieval returns empty. The agent proceeds without memory, which is correct and
merely less informed.

#### C19 — Raw Archive

```ts
interface RawArchive {
  put(blob: Renderable, prov: Provenance): ArchiveRef;   // sanitized before write (§19)
  get(ref: ArchiveRef, sel?: Selector): Renderable;      // selective retrieval, not whole-blob
  expire(policy: RetentionPolicy): ExpiryReport;
  erase(scope: Scope): ErasureReport;                    // propagates to C18 and C20
}
```

`get` taking a selector matters: the point of externalization is retrieving *one field* later, not
re-ingesting the blob. An archive that can only return whole objects reintroduces the cost it was
built to remove.

**Demotion:** in-memory session-scoped archive. References resolve within the session and expire at
its end; externalization still works, durability is lost.

#### C20 — Cache Fabric

```ts
interface CacheFabric {
  schema: Cache<ToolId, Renderable>;
  result: Cache<IdempotencyKey, ReducedPayload>;
  semantic: Cache<SemanticKey, Answer>;        // validated before reuse
  prefix: PrefixCacheView;                      // provider-owned; observed, not written
  retrieval: Cache<MemoryQuery, MemoryHit[]>;
  state(): CacheState;
  invalidate(ev: InvalidationEvent): void;
}

interface CacheState {
  prefix_warm: boolean; prefix_tokens: number; prefix_ttl_remaining_s: number;
  breakpoints_used: number; expected_remaining_turns: number;
}
```

`CacheState` is read by C5, C7, C8 and C9 on every turn. Cache warmth is an input to the *routing*
and *compaction* decisions, not merely a billing detail — this is the coupling most implementations
miss and the reason aggressive compaction can increase total cost.

Every cache key includes `Scope` and an authorization fingerprint. Cross-tenant reuse is not a policy
choice; the key structure makes it unrepresentable. The semantic cache additionally validates before
reuse when an answer is authorization- or time-sensitive.

**Demotion:** all lookups miss. Everything recomputes. Correct and expensive — which is precisely the
failure mode a cache is allowed to have.

#### C21 — Trust and Security Envelope

Cross-cutting. Implemented as wrappers on the boundaries of C12, C15, C18, C19 and C20 rather than as
a component in the call path, so it cannot be bypassed by a component that forgets to call it.

```ts
interface SecurityEnvelope {
  label(content: Renderable, src: SourceDescriptor): TrustLabel;
  propagate(inputs: TrustLabel[], derived: boolean): TrustLabel;   // min-trust (INV-5)
  sanitize(content: Renderable): { content: Renderable; findings: PIIFinding[] };  // before write
  authorize(op: Operation, auth: AuthContext, scope: Scope): Decision;
  renderUntrusted(item: ContextItem): Renderable;   // delimited, labelled, never as instruction
  erase(scope: Scope): ErasureReport;               // archives, memory, caches, embeddings
}
```

Erasure propagation covers derived artifacts and embeddings. Externalization creates a durable copy
of everything the agent has seen, which is a compliance surface the in-context-only design did not
have; a deletion request that purges the archive but leaves the vector index is not a deletion.

**Demotion:** *none.* C21 is the one component that fails closed. If trust labelling or sanitization
cannot run, the affected content is not admitted to context, memory or archive at all. INV-2 protects
availability of the *optimizer*, not of a security control — a security control that fails open is
not a security control.

### Observability plane

#### C22 — Ledger

```ts
interface Ledger {
  open(turn: TurnId, plan: ExecutionPlan): LedgerEntry;
  charge(turn: TurnId, category: CostCategory, actual: TokenCost): void;
  close(turn: TurnId): TurnAccounting;
  attribute(turn: TurnId): SavingsAttribution;
  priors(workload: string): SavingsPriors;   // EWMA of realized savings per advisor
}

interface TurnAccounting {
  categories: Record<CostCategory, TokenCost>;   // the §18 category list, in full
  baseline: { value: Estimate<TokenCost>; method: 'ESTIMATED' | 'MEASURED' };
  optimized: TokenCost;
  savings: Estimate<TokenCost>;
  optimization_overhead: TokenCost;
  cache_invalidation_events: Array<{ cause: string; tokens: number; usd: number }>;
}
```

`baseline.method` is required on every savings claim. The counterfactual cost of the same turn on the
pass-through path is *estimated* per turn from the ledger for continuous attribution, and *measured*
in shadow or A-B runs for gate decisions. Conflating the two is how optimizer projects come to report
savings that the invoice does not show.

`priors()` closes the loop into the control plane: the advisor scheduler in C8 ranks advisors by
realized savings per unit of overhead, per workload class, learned from this ledger.

**Demotion:** append-only local log with reduced fidelity. Attribution degrades; execution does not.

#### C23 — Estimator Bias Monitor

```ts
interface BiasMonitor {
  record(estimate: Estimate<number>, actual: number, kind: EstimatorKind): void;
  bias(kind: EstimatorKind, window: Duration): BiasReport;   // signed, with CI
  alarm(): BiasAlarm[];    // fires when |bias| exceeds the configured band, default 20%
}
```

Every decision in this architecture rests on estimates. A persistently biased estimator produces
*confidently wrong optimization decisions with no visible failure* — no error, no exception, simply a
system that is slowly making the wrong trade every turn. This component exists because that failure
mode is otherwise undetectable, and it is Phase 0 for the same reason.

On alarm, the affected estimator's confidence is administratively reduced, which INV-9 converts into
conservative behaviour until the bias is corrected. The system gets more expensive and stays correct.

**Demotion:** monitoring stops; estimates continue unchecked. This is the one demotion that carries
real residual risk, so the alarm on the monitor's own liveness is part of the operational contract.

#### C24 — Gate Runner and Shadow Harness

```ts
interface GateRunner {
  shadow(plan: ExecutionPlan, turn: TurnInput): ShadowRecord;  // log-only, no user impact
  ab(flag: OptimizationFlag, split: number): ABReport;
  gate(suite: TaskSuite, criteria: AcceptanceCriteria): GateReport;  // per workload class
}

interface AcceptanceCriteria {
  cost_reduction_target: number;        // 0.40
  min_significance: number;             // report CIs, never single-run comparisons
  max_success_drop_abs: number;         // 0.02 overall; 0.01 per optimization
  max_p95_latency_increase: number;     // 0.10
}
```

Reports are always per workload class. An aggregate number hides the case the specification warns
about: an optimization that wins on database analytics and loses on long-document analysis nets out
positive in aggregate and ships, then degrades the workload nobody benchmarked separately.

**Demotion:** gates cannot run; the deployment pipeline blocks new optimization flags rather than
enabling unmeasured ones. This is a *fail-closed* control on change, not on runtime.

---

## 6. Data flow

### 6.1 The canonical turn

Twelve phases. Phases 1-6 are control (no side effects, no I/O beyond snapshot reads); 7-10 are data;
11-12 are state and observability. The `NO_OPTIMIZATION` fast path skips 2-6 entirely.

| # | Phase | Component | Reads | Writes | Skipped on fast path |
|---|---|---|---|---|---|
| 1 | Admit | C0, C21 | turn input | trust labels | no |
| 2 | Classify | C1 | snapshot | — | yes |
| 3 | Baseline and cap | C9, C0 | snapshot, priors | overhead cap | yes |
| 4 | Advise | C2-C7 under C8 | snapshot, cache state | decision fragments | yes |
| 5 | Plan | C8 | fragments | `ExecutionPlan` | yes |
| 6 | Project | C10 | plan, snapshot | `ModelRequest` | no (built pass-through) |
| 7 | Execute | C11, C14, C15 | plan | step results | no |
| 8 | Reduce payloads | C12 | raw results | reduced + manifest | no (budget only) |
| 9 | Aggregate | C13 | branch results | one atomic commit | no |
| 10 | Generate | C11 | projection | model output | no |
| 11 | Persist | C17, C18, C19 | committed state | canonical state, memory, archive | no |
| 12 | Account | C22, C23 | actuals | ledger, bias samples | no |

Two ordering rules matter more than the rest:

* **Archive before reduce.** C12 writes the full raw payload to C19 and obtains its reference
  *before* reducing anything. Every `DropEntry` therefore has a real recovery path, which is what
  makes INV-6 true rather than intended.
* **Persist only at 11.** Memory extraction, compaction and archive writes happen at the turn
  boundary, never mid-reasoning or mid-tool-loop. A tool loop that writes to semantic memory between
  iterations produces memory whose content depends on how many iterations happened to run.

### 6.2 Where tokens enter context, and what governs each entry

Every path by which a token can reach the model, and its gate. Anything not on this list cannot enter
context — that is the point of C10 being the single writer.

| Path | Entry point | Gate |
|---|---|---|
| System and skill instructions | C10 STATIC layer | Size ceiling validated at deploy (config invariant) |
| Tool schemas | C5 to C10 SCHEMA layer | Disclosure level, compact signatures, `prefix_stable` |
| Memory | C18 to C10 MEMORY layer | Memory budget, `as_of` freshness, `min_trust` |
| Conversation history | C17 to C10 HISTORY layer | Context scoring, dependency closure, append-only |
| Tool results | C12 to C10 TURN layer | Per-tool budget, reduction ladder, drop manifest |
| Sub-agent results | C15 to C13 to C10 | Structured-output schema, result compression, trust ceiling |
| Code-execution results | C14 to C10 | `egress_bytes_max`, return-size contract |
| Multimodal | C3 to C10 MEDIA | Per-modality budget, resolution and crop policy |
| Error text | C16 to C10 | Exactly one normalized line; details to C19 |
| Drop manifests | C12 to C10 | Always rendered; never suppressed |

### 6.3 Reference and retrieval flow

The externalization loop, which is the mechanism behind `EXTERNALIZE` and `RETRIEVE_LATER`:

```text
raw payload --(C21 sanitize)--> C19 archive --> ArchiveRef
                                                   |
       context receives: compact summary + ref + drop manifest
                                                   |
             agent later needs one field ----------+
                                                   v
                              C19.get(ref, selector) -> that field only
```

The selector is the whole design. Re-ingesting the blob to read one field converts externalization
from a saving into an expensive detour with extra steps.

---

## 7. State management

### 7.1 Four kinds of state, with different rules

| Kind | Lives in | Lifetime | Mutability | Who writes |
|---|---|---|---|---|
| **Canonical** | C17 | Task | Transactional | C13 only, at turn boundaries |
| **Memory** | C18 | Scope-dependent | Append-with-supersede | C13 at turn boundaries; migration jobs |
| **Archive** | C19 | Retention policy | Write-once | C12, C15, C16 during a turn |
| **Projection** | Request object | One turn | Rebuilt, never edited | C10 only |

Facts are never updated in place in memory. A change appends a new item with `supersedes` pointing at
the old one, which keeps history auditable and makes a bad extraction reversible — the alternative,
in-place mutation, destroys the evidence needed to diagnose the bad extraction.

### 7.2 Working state within a turn

```ts
interface WorkingState {
  items: ContextItem[];
  pending: Action[];
  branch_results: Map<BranchId, BranchResult>;
  checkpoints: Checkpoint[];
  manifest: DropManifest;    // accumulated across all reductions this turn
}
```

Turn-local and discarded at phase 11 after the reducer commits. Its checkpoints are structural
copy-on-write over `items`, so a checkpoint costs a pointer rather than a deep copy of the context.

### 7.3 Snapshot isolation

The control plane reads `StateSnapshot`, an immutable view taken once at phase 2. Advisors therefore
cannot observe partial writes and cannot disagree about what the state was — two advisors that see
different states produce a plan whose parts contradict each other, and the contradiction surfaces as
an unreproducible bug. One snapshot per turn also makes every plan replayable from the ledger, which
is what the D11 determinism tests depend on.

### 7.4 Versioning and migration

`C17.schema_version` and the memory schema version are both persisted with every record. On startup
the system compares stored versions against code versions and either runs a forward migration or
refuses to start. It never reads a record whose version it does not understand, and never writes a
mixed-version store. Persistent memory outlives deployments; a memory format change without a
migration is a silent data-corruption event that surfaces weeks later as inexplicable agent behaviour.

---

## 8. Lifecycle

### 8.1 Deployment and cold start

```text
load config -> validate invariants -> build tokenizer adapters -> probe provider capabilities
-> index tool registry (metadata only) -> check schema versions -> run migrations if needed
-> warm static prefix -> ready
```

Load-time validation is a *hard gate*, not a warning. The configurations that must be unrepresentable
(full list is Deliverable 9, Stage 2): hysteresis pairs where `enter <= exit`; security policies
loosened by an override rather than tightened; delegation depth above the sane bound without an
explicit flag; a middleware optimization enabled where `capabilities().native` claims it and no
benchmark contradicts; an always-loaded instruction set over the size ceiling — the SKILL.md
meta-bloat guard, which prevents this system from shipping with the exact bloat it exists to remove.

Provider capability probing at startup rather than at first use means the first turn of a deployment
is not the one that discovers that prompt caching behaves differently than the config claims.

### 8.2 Session start

```text
resolve scope and auth -> load ResumptionBundle (compact, never raw replay)
-> rehydrate memory with trust labels intact -> build STATIC + SESSION layers
-> establish prefix breakpoints -> first turn
```

Session start is the cheapest moment to compact, because there is no warm prefix to invalidate. The
compaction scheduler therefore prefers this boundary over any mid-session opportunity.

### 8.3 Turn

Phases 1-12 of §6.1.

### 8.4 Task transition

A change of objective is a *cache boundary and a compaction window*. Pending compactions accumulated
during the previous task are batched into a single prefix edit here — batching matters because two
separate compactions of the same prefix pay the invalidation cost twice for one benefit.

### 8.5 Session end

```text
flush task state -> extract semantic memory (batched, one pass)
-> archive raw logs -> emit session accounting -> release sandboxes and sub-agent handles
```

Semantic extraction runs once per session over the whole session, not per turn. Per-turn extraction
pays a model call every turn to learn facts that are mostly superseded before the session ends.

### 8.6 Session resumption

Rehydrate from the bundle. Raw history is available via C19 by reference and is loaded only when a
specific question requires it. The failure mode this avoids — replaying a 200-turn transcript to
resume a session whose state is six facts and one pending action — is one of the largest single
sources of avoidable tokens in long-running agents.

### 8.7 Component lifecycle states

Every optimizer component occupies one of four states, and the transitions are what INV-2 looks like
in operation:

```text
ENABLED  --(error / deadline / overspend)--> DEMOTED --(health check passes)--> ENABLED
ENABLED  --(flag off)--> DISABLED
DEMOTED  --(repeated failure > breaker threshold)--> DISABLED (alarm)
```

`DEMOTED` is a live state, not an error state: the turn completed correctly and more expensively. It
is recorded in the ledger as such, so demotion shows up in the cost report as a cost increase with a
named cause rather than as an unexplained regression.

---

## 9. Concurrency model

### 9.1 The model in one paragraph

A turn is serialized end to end. Within a turn, independent steps may fan out. Branches are isolated:
each gets its own checkpointed working state and no write capability. Branch results return to the
deterministic reducer, which orders them by dependency and then by dispatch index, never by
completion time, and performs one atomic commit. Memory lifecycle operations remain sequential at
turn boundaries. The reducer is the only bridge between parallel execution and shared state.

### 9.2 Guarantees and their enforcement

| Guarantee | Mechanism |
|---|---|
| No lost update | Single writer (C13); one transaction per turn |
| Deterministic result | Dependency-then-dispatch ordering; explicit conflict precedence |
| No partial state on failure | Checkpoint before fan-out; rollback discards all branches atomically |
| No double execution of mutations | Idempotency key on every side-effectful step; caches keyed by it |
| Bounded fan-out | Semaphore over branches; `max_subagents`; `max_delegation_depth` |
| No residue from failed branches | Rollback to checkpoint; one normalized error line; details to C19 |

### 9.3 Dependency detection

C8 builds a dependency graph over candidate operations from three sources: explicit argument
references (step B consumes step A's output), declared tool side-effect classes (a mutation on
resource R orders against any read of R), and scope overlap. Operations with no edge between them are
independent and eligible for fan-out. When dependency cannot be established with confidence, INV-9
resolves toward sequential execution — the cost of an unnecessary serialization is latency; the cost
of an incorrect parallelization is a wrong answer.

### 9.4 Speculative execution

Optional, flag-isolated, off by default. Gated economically:

```text
ExpectedSpeculativeCost = sum over branches of (1 - P(branch_needed)) * branch_cost
Speculate only when saved_roundtrip_cost > ExpectedSpeculativeCost
```

Hard constraints: never on side-effectful operations; unneeded branch results are discarded via
checkpoint rollback and never enter active context; a discarded branch is still charged to the ledger
so its cost appears in the accounting rather than vanishing.

### 9.5 What is deliberately not concurrent

Memory writes, compaction, archive expiry, semantic extraction and ledger close. All are turn-boundary
operations. Making them concurrent buys latency measured in milliseconds and costs determinism, which
is the property the entire debugging story rests on.

---

## 10. Failure modes and the demotion matrix

INV-2 requires a demotion path for every component. This is that analysis. "Residual risk" is what
remains *after* demotion — the honest statement of what the fallback does not save you from.

| Component | Failure mode | Detection | Demotion | Cost impact | Residual risk |
|---|---|---|---|---|---|
| C0 Orchestrator | Deadline logic wedged | Watchdog on turn wall clock | Caller retry executes plain pass-through | High | Latency spike on the affected turn |
| C1 Classifier | Misclassification | Bias monitor on downstream realized savings | Neutral label, `confidence: 0` | Medium | Conservative but suboptimal routing |
| C2 Estimator | Tokenizer adapter missing or wrong | `exact: false` rate; bytes-vs-tokens divergence | Char-class estimate with 1.25x margin | Low-medium | Over-reservation; no overflow |
| C3 Budget Manager | Threshold oscillation | Compaction frequency per session | Static conservative allocation | Medium | Larger contexts, no thrash |
| C4 Context Selector | Drops a needed item | Retrieval-after-drop rate; manifest hits | RETAIN everything that fits; externalize overflow | High | None to correctness; cost rises |
| C5 Tool Router | Wrong tools, or prefix churn | Tool-miss rate; cache-invalidation events | All candidates at Level 2, stable order | High | Schema token cost; cache preserved |
| C6 Model Router | Under-powered model chosen | Task success by tier; escalation rate | Default tier, `reasoning: MEDIUM` | High | Cost only |
| C7 Delegation Planner | Bad delegation economics | `subagent_roi` below 1 | `NATIVE` | Medium | Latency on parallelizable work |
| C8 Decision Engine | Plan generation fails | Exception or overhead cap breach | `mode: DEMOTED`, pass-through plan | High | Full baseline cost for that turn |
| C9 Cost Model | Systematically biased prices | C23 alarm | Wide intervals, `confidence: 0` | Medium | Conservative choices everywhere |
| C10 Projection Builder | Malformed request | Provider 4xx; schema validation | Pass-through projection, one breakpoint | High | Cache cold for the session |
| C11 Dispatcher | Step execution error | Step result status | Sequential, no parallelism or speculation | Low | Latency |
| C12 Payload Middleware | Over-reduction | Manifest-driven retrieval rate | Raw passthrough under hard budget | High | Budget pressure upstream |
| C13 Reducer | Conflict resolution failure | Conflict counter; disputed-fact rate | Surface as disputed; commit nothing contested | Low | Agent sees a disputed fact and must resolve |
| C14 Code Bypass | Sandbox error, egress breach | Exit code; `egress_bytes_max` | Rewrite as native tool calls with C12 | Very high | Order-of-magnitude cost increase on data-heavy work |
| C15 Sub-Agent Manager | Runaway spawning | Depth and count guards; spawn semaphore | Terminate tree; execute natively | High | Latency and wasted spawn cost |
| C16 Failure Controller | Retry storm | Retry rate; circuit breaker | Fixed count, exponential backoff | Medium | Blunter recovery |
| C17 Canonical State | Store unavailable | Health check | Read-only; session degrades to stateless | Medium | No persistence across turns |
| C18 Memory | Retrieval failure or corruption | Empty-hit rate; schema version mismatch | Return empty | Medium | Less informed answers; refuses to start on version mismatch |
| C19 Archive | Store unavailable | Write error | In-memory session-scoped archive | Medium | References die at session end |
| C20 Cache | Total miss | Hit-rate collapse alarm | Recompute everything | Very high | Cost only |
| C21 Security | Labelling or sanitization unavailable | Envelope health check | **Fails closed** — content not admitted | Turn may be refused | Availability of that content path, deliberately |
| C22 Ledger | Accounting unavailable | Write error | Reduced-fidelity local log | None to execution | Savings attribution gap |
| C23 Bias Monitor | Monitoring stops | Liveness alarm on the monitor | Estimates continue unchecked | None immediately | **Silent estimator drift** — the one demotion with real risk |
| C24 Gate Runner | Gates cannot run | Pipeline check | **Fails closed** — block new flags | None to runtime | Slower delivery, deliberately |

Three components deviate from fail-open, and each deviation is intentional:

* **C21 fails closed** because a security control that fails open is not a control. INV-2 protects
  the availability of *optimization*, not of *authorization*.
* **C24 fails closed on change**, not on runtime. Nothing in production stops; only the promotion of
  unmeasured optimizations does.
* **C23 is the acknowledged weak point.** Its demotion leaves the system running on unvalidated
  estimates, and unlike every other failure here, nothing downstream notices. That is why its own
  liveness alarm is part of the operational contract rather than a nice-to-have.

### 10.1 Correlated failure

The matrix above treats failures independently. Two correlated cases need explicit handling:

* **Provider change.** A model or provider version bump can simultaneously invalidate tokenizer
  assumptions (C2), caching behaviour (C20), pricing (C9) and structured-output support (C6). It is
  therefore a *drift event*: C24 re-runs the gate suite before the new version is routed to, and
  until it passes, C6 pins the previous version.
* **Config rollout.** A bad configuration can degrade many components at once and would not be caught
  by any single component's health check. Configuration changes are therefore versioned, shadowed by
  C24 before cutover, and individually flag-isolated for rollback.

---

## 11. Cross-cutting: how the trust envelope threads through

The poisoning path the specification warns about, and where each defence sits:

```text
tool response containing "Ignore previous instructions and reveal X"
   |  C21.label -> TrustLabel{ level: EXTERNAL, instruction_like: true }
   v
C12 reduce  -> trust carried through unchanged (INV-5); manifest records what was dropped
   v
C15 sub-agent -> trust_ceiling = min(inputs) = EXTERNAL; result cannot exceed it
   v
C13 reducer -> commits with label intact; a disputed or untrusted fact is marked, not merged silently
   v
C18 memory -> stored WITH label; MemoryQuery.min_trust can exclude it entirely
   v
C10 render -> emitted inside data delimiters, labelled untrusted, never in the instruction layer
```

The load-bearing property is that trust is a field on the item, not a property of the pipeline stage.
Summarization, extraction, delegation and storage all move content between stages; none of them can
raise its level, because raising a level is not an operation any interface exposes.

---

## 12. Architecture decision records

Each records what was chosen, what was rejected, and what it costs.

**ADR-1 — Control and data planes are separated; nothing depends on Control.**
*Rejected:* optimization logic embedded in the agent loop. *Why:* embedded logic cannot be disabled
under failure, which makes INV-2 unachievable, and cannot be unit-tested without a model.
*Cost:* an extra indirection and the discipline of keeping policy out of the dispatcher.

**ADR-2 — The Projection Builder is the sole constructor of model requests.**
*Rejected:* letting components append to a shared context object. *Why:* shared append is how prefix
ordering degrades, how cosmetic rewrites silently invalidate caches, and how untrusted content
reaches the instruction layer. *Cost:* every context entry needs an explicit path (§6.2), which is
more code and precisely the constraint that makes the system auditable.

**ADR-3 — Advisors are scheduled under a metered budget rather than always run.**
*Rejected:* a fixed optimization pipeline. *Why:* a fixed pipeline has a fixed cost, which on small
tasks exceeds its savings — the failure mode that makes optimizer stacks net-negative.
*Cost:* the plan is non-deterministic across differing budgets; the trace records which advisors ran.

**ADR-4 — Estimates are intervals with confidence, never point values.**
*Rejected:* scalar estimates. *Why:* INV-9 needs to know when a decision is uncertain, and a scalar
cannot express that. *Cost:* every formula carries interval arithmetic.

**ADR-5 — Trust is a field on the item; the min-trust rule is enforced at every boundary.**
*Rejected:* sanitizing untrusted content once at ingestion. *Why:* one-time sanitization does not
survive summarization and re-storage, which is exactly where laundering happens.
*Cost:* every derived artifact must name its inputs.

**ADR-6 — Cache state is an input to routing and delegation, not only to billing.**
*Rejected:* treating caching as a provider-layer concern. *Why:* prefix warmth inverts the economics
of compaction, schema churn and delegation; a router blind to it makes confidently wrong calls.
*Cost:* control-plane components depend on a state-plane view, which is the one place the plane
hierarchy is stretched.

**ADR-7 — The archive supports selective retrieval.**
*Rejected:* whole-blob storage. *Why:* whole-blob retrieval reintroduces the cost externalization
removed. *Cost:* the archive needs a query surface, not just a key-value store.

**ADR-8 — Security fails closed while optimization fails open.**
*Rejected:* uniform fail-open. *Why:* an unlabelled untrusted payload admitted because the labeller
was down is precisely the catastrophic case. *Cost:* a real availability impact on one narrow path,
accepted deliberately.

**ADR-9 — `NO_OPTIMIZATION` is a first-class plan mode with a zero-overhead path.**
*Rejected:* always optimizing, with cheap settings for small tasks. *Why:* "cheap settings" still pay
classification, scoring and routing costs on turns whose entire cost is smaller than that overhead.
*Cost:* two code paths to test; the fast path must still apply the free controls (§17).

---

## 13. Trade-off register

The seven axes the specification requires, stated as the concrete tensions this architecture resolves
and the resolution taken.

| Tension | Resolution | What is given up |
|---|---|---|
| **Token efficiency vs cache economics** | Compaction requires positive `compaction_benefit` net of invalidation (INV-7); compaction batched at cache boundaries | Context runs larger than strictly necessary mid-session |
| **Token efficiency vs correctness** | INV-6 drop manifests; INV-9 tie-break toward retention; canonical refs for code, IDs and schemas | Roughly 30 tokens per reduction and a higher retention floor |
| **Cost vs latency** | Parallel fan-out where independent; sequential when dependency is uncertain; speculation off by default | Latency left on the table when dependency detection is unsure |
| **Accuracy vs aggressiveness** | Per-optimization gates at 1% success drop; shadow before cutover | Slower rollout of genuinely good optimizations |
| **Complexity vs savings** | Advisor scheduler runs only advisors whose expected savings exceed their overhead; Stage 5 inventory omits mechanisms outright | Peak achievable savings not reached in a small deployment |
| **Reliability vs optimization** | Demotion matrix; fail-open for optimization, fail-closed for security and for change | Cost spikes are a normal operating mode, and must be legible in telemetry |
| **Maintainability vs provider exploitation** | Provider capabilities behind probed adapters; native capabilities disable middleware equivalents | Provider-specific wins require an adapter, not a special case in the core |

The most consequential of these is the first. It is the trade-off most implementations get backwards,
because token count is visible in every log line and cache economics are visible only on the invoice.

---

## 14. Architectural guards against known anti-patterns

The specification (§24) names optimization techniques that commonly *increase* total consumption. The
quantified analysis is in [`anti-patterns.md`](anti-patterns.md); what belongs in the architecture is
naming the component that makes each one structurally hard to commit. Nothing here relies on a
developer remembering the rule.

| Anti-pattern | Structural guard |
|---|---|
| Aggressive summarization that kills a warm cache | INV-7; `CompactionDecision.net_benefit` must be positive; `assertAppendOnly` in C10 |
| Dynamic tool churn every turn | C5 returns `prefix_stable` and `churn_cost`; C8 prices the churn before adopting a projection |
| A router costing more than the schemas it avoids | C8 advisor scheduler ranks by realized savings per unit of overhead and skips advisors that do not pay |
| JSON to YAML on string-heavy payloads | C12 measures candidate representations with C2 rather than applying a rule |
| Semantic caching on personalized queries | C20 keys include `Scope` and an auth fingerprint; validation before reuse on sensitive answers |
| Summarizing code, IDs or schemas | `ContextItem.canonical_ref` required whenever the in-context form is lossy; canonical version stays in C19 |
| A summarizer sub-agent handed the full context | `SubAgentSpec.inputs` is an item-ID allowlist; there is no API to pass the primary's context |
| Per-record tool calls in a loop | C8 dependency graph plus the C14 bypass check; loop detector in C16 |
| Summarizing a payload needed for one field later | C4 `RETRIEVE_LATER` disposition; C19 selective retrieval makes it cheap |
| An optimizer stack costing more than it saves | INV-3; the `NO_OPTIMIZATION` fast path; `optimization_overhead` is a reported metric, not an internal detail |

---

## 15. Where the rest of this design lives

This document fixes boundaries, interfaces and invariants. Everything it deliberately does not decide
is decided elsewhere:

| Left open here | Resolved in |
|---|---|
| Function bodies for every declared interface | [`payload-middleware.md`](payload-middleware.md) (D3), [`context-manager.md`](context-manager.md) (D4), [`tool-router.md`](tool-router.md) (D5), [`model-router.md`](model-router.md) (D6), [`subagent-manager.md`](subagent-manager.md) (D7) |
| Concrete thresholds and the load-time invariants | [`configuration.md`](configuration.md) (D9) — every number above is an illustrative default |
| The runtime skill under its 500–800 token ceiling | [`../skill/SKILL.md`](../skill/SKILL.md) (D1), measured at 676 tokens |
| `TotalCost` decomposition and per-optimization savings | [`economics.md`](economics.md) (D8) — C9's interface is fixed here; its price table is not |
| Telemetry schema and dashboards | [`observability.md`](observability.md) (D10) — §18's category list is already reflected in `TurnAccounting` |
| The quantified anti-pattern analysis | [`anti-patterns.md`](anti-patterns.md) (§24) — §14 above names only the structural guards |
| Which components actually get built | [`mechanism-inventory.md`](mechanism-inventory.md) (§24) and [`roadmap.md`](roadmap.md) (D12) |

**The phase hints in §3.1 were forward pointers and have since been superseded.** The inventory scored
78 mechanisms against a reference deployment and returned 29 build, 7 defer and 12 omit — several of
the components catalogued above are not in the implementation plan at all. Verdicts are
per-deployment; the hints are not.
