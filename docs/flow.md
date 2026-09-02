# §21 — Architectural Flow

**Stage 1 of 5.** Six diagrams covering the master architecture, the turn state machine, the decision
engine, the concurrency and reducer model, projection and cache layering, and failure demotion.

Companion documents: [`architecture.md`](architecture.md) (Deliverable 2),
[`decision-engine.md`](decision-engine.md) (§17).

---

## 1. Master architecture

Every component of the system, the three execution paths, and the state and observability planes they
depend on. Solid arrows are the request path; dotted arrows are consultation or accounting.

```mermaid
flowchart TB
  U(["User request"]) --> PA["Primary agent"]
  PA --> K["C0 Turn Orchestrator<br/>overhead meter · demotion guard"]
  K --> S0{"S0 · optimization<br/>worthwhile?"}
  S0 -- "no · warm cache, small task" --> FP["Fast path<br/>free controls only<br/>zero optimizer overhead"]
  S0 -- "yes" --> DE

  subgraph CTRL["CONTROL PLANE · decides, never executes"]
    direction TB
    DE["C8 Decision Engine<br/>advisor scheduler · S1 to S16"]
    C1["C1 Task Classifier<br/>intent · workload · risk"]
    C2["C2 Token Estimator<br/>tokenizer adapters"]
    C3["C3 Budget Manager<br/>thresholds with hysteresis"]
    C4["C4 Context Scorer<br/>five-way disposition"]
    C5["C5 Tool Registry + Router<br/>disclosure · prefix stability"]
    C6["C6 Model + Reasoning Router"]
    C7["C7 Delegation Planner"]
    C9["C9 Cost Model + VOI"]
    DE -.-> C1
    DE -.-> C2
    DE -.-> C3
    DE -.-> C4
    DE -.-> C5
    DE -.-> C6
    DE -.-> C7
    DE -.-> C9
  end

  DE ==>|"ExecutionPlan<br/>immutable"| C10
  FP ==>|"pass-through plan"| C10

  subgraph DATA["DATA PLANE · executes, never decides"]
    direction TB
    C10["C10 Projection Builder<br/>SOLE writer of model requests"]
    C11["C11 Execution Dispatcher"]
    C14["C14 Code Execution Adapter"]
    C15["C15 Sub-Agent Manager"]
    C12["C12 Payload Middleware<br/>reduction ladder + drop manifest"]
    C13["C13 Deterministic Reducer<br/>SOLE write path to state"]
    C16["C16 Failure Controller<br/>checkpoint · rollback · breakers"]
  end

  C10 --> C11
  C11 --> NAT["Native execution"]
  C11 --> BYP["Code-execution bypass"]
  C11 --> SUB["Sub-agent delegation"]
  BYP --> C14
  SUB --> C15

  NAT --> TOOL["Tool / MCP / API / database"]
  C14 --> EXEC["Sandboxed execution environment<br/>tools imported on demand<br/>intermediate data never enters context"]
  EXEC -.-> TOOL
  C15 --> POOL["Sub-agent pool<br/>minimal context · structured results<br/>trust ceiling enforced"]
  POOL -.-> TOOL

  TOOL --> UP["Upstream reduction<br/>field selection · pagination · aggregation"]
  UP --> C12
  C14 --> C13
  POOL --> C13
  C12 --> C13
  C13 --> BC{"Context budget check<br/>C3 band"}
  BC -- "within budget" --> C10
  BC -- "over budget" --> C12
  C16 -.-> C11
  C16 -.-> C14
  C16 -.-> C15

  C10 --> GEN["Model request → provider"]
  GEN --> PA2["Primary agent"]
  PA2 --> RESP(["Final response"])

  subgraph STATE["STATE PLANE · the source of truth"]
    direction LR
    C17["C17 Canonical State<br/>task state · decisions · pending"]
    C18["C18 Memory<br/>working · episodic · semantic<br/>provenance + temporal validity"]
    C19["C19 Raw Archive<br/>selective retrieval by reference"]
    C20["C20 Cache Fabric<br/>schema · result · semantic<br/>prefix · retrieval"]
  end

  C13 ==>|"one atomic commit<br/>at the turn boundary"| C17
  C13 ==> C18
  C12 -- "full payload written<br/>BEFORE reduction" --> C19
  C10 -.-> C17
  C10 -.-> C18
  C10 -.-> C19
  C20 -.-> DE
  C20 -.-> C10
  C12 -.-> C20

  subgraph SEC["C21 Trust + Security Envelope · wraps every boundary · fails CLOSED"]
    direction LR
    SEC1["trust labelling<br/>min-trust propagation"]
    SEC2["PII/secret sanitization<br/>before every external write"]
    SEC3["tenant scoping<br/>erasure propagation"]
  end
  SEC -.-> C12
  SEC -.-> C15
  SEC -.-> C18
  SEC -.-> C19
  SEC -.-> C20

  subgraph OBS["OBSERVABILITY PLANE · measures, gates"]
    direction LR
    C22["C22 Ledger<br/>per-category accounting<br/>baseline counterfactual"]
    C23["C23 Estimator Bias Monitor"]
    C24["C24 Gate Runner + Shadow"]
  end

  C11 -.-> C22
  C10 -.-> C22
  C12 -.-> C22
  C15 -.-> C22
  C14 -.-> C22
  C22 -.-> C23
  C22 ==>|"EWMA savings priors<br/>per workload class"| DE
  C23 ==>|"bias alarm lowers<br/>estimate confidence"| C9
  C24 -.-> C22

  classDef ctrl fill:#e8f0fe,stroke:#3b6fd4,color:#12305e
  classDef data fill:#e9f7ef,stroke:#2e8b57,color:#14432a
  classDef st   fill:#fdf3e3,stroke:#c9852a,color:#5a3a06
  classDef obs  fill:#f3ecfa,stroke:#7a4fbf,color:#361a5e
  classDef sec  fill:#fdeaea,stroke:#c0392b,color:#5c1a13
  class DE,C1,C2,C3,C4,C5,C6,C7,C9 ctrl
  class C10,C11,C12,C13,C14,C15,C16,NAT,BYP,SUB data
  class C17,C18,C19,C20 st
  class C22,C23,C24 obs
  class SEC1,SEC2,SEC3 sec
```

**Reading the diagram.** Three things are worth tracing explicitly:

* **The two heavy arrows into the state plane** are the only write paths. Everything else that
  touches state does so through a snapshot read.
* **The heavy arrow from the Ledger back into the Decision Engine** is the learning loop: realized
  savings per advisor, per workload class, are what the advisor scheduler ranks on. Without it the
  scheduler runs on hand-written priors forever.
* **`C12 → C19` is labelled "before reduction" deliberately.** The archive write happens first so
  that every drop-manifest entry has a real recovery path (INV-6).

---

## 2. Turn state machine

The twelve-phase turn, with the fast path, the early exits, and the demotion transitions.

```mermaid
stateDiagram-v2
  direction TB
  [*] --> Admit

  Admit: Admit · C0 + C21
  Admit: trust-label the input, open the ledger entry

  state fast_choice <<choice>>
  Admit --> fast_choice
  fast_choice --> FastPath: fast-path eligible
  fast_choice --> Classify: not eligible

  FastPath: Fast path
  FastPath: free controls only, zero metered overhead
  FastPath --> Project

  Classify: Classify · C1
  Classify --> Baseline

  Baseline: Baseline and cap · C9 + C0
  Baseline: counterfactual cost sets the overhead cap
  Baseline --> Advise

  Advise: Advise · C2 to C7 under the scheduler
  Advise --> Plan

  Plan: Plan · C8
  Plan: emit an immutable ExecutionPlan
  state exit_choice <<choice>>
  Plan --> exit_choice
  exit_choice --> Answer: answered from context, memory or cache
  exit_choice --> Project: work required

  Project: Project · C10
  Project: layer ordering, breakpoints, append-only assertion
  Project --> Execute

  Execute: Execute · C11 / C14 / C15
  Execute --> Reduce: results returned
  Execute --> Recover: step failed

  Recover: Recover · C16
  Recover: rollback to checkpoint, one normalized error line
  Recover --> Execute: retry, escalate reasoning, escalate model, fallback tool
  Recover --> Answer: abort with normalized error

  Reduce: Reduce payloads · C12
  Reduce: archive first, then the reduction ladder, always a manifest
  Reduce --> Aggregate

  Aggregate: Aggregate · C13
  Aggregate: dependency-then-dispatch order, one atomic commit
  Aggregate --> Generate

  Generate: Generate · C11
  Generate: max_tokens, stop sequences, structured output, early exit
  Generate --> Answer

  Answer: Answer
  Answer --> Persist

  Persist: Persist · C17 + C18 + C19
  Persist: turn-boundary writes only, never mid-loop
  Persist --> Account

  Account: Account · C22 + C23
  Account: per-category actuals, savings attribution, bias samples
  Account --> [*]

  Demoted: DEMOTED
  Demoted: pass-through projection, default model, no reduction
  Advise --> Demoted: advisor failure beyond guard
  Plan --> Demoted: validate failed
  Project --> Demoted: malformed request
  Demoted --> Execute
```

The `Demoted` state is a **live** state, not an error state. Turns that pass through it complete
correctly and cost more, and the ledger records the demotion as a named cause so the cost increase is
attributable rather than mysterious.

---

## 3. Decision engine

Stages S0 to S16, with the short-circuit exits that make the cheap paths cheap.

```mermaid
flowchart TB
  IN(["Turn input + state snapshot"]) --> S0{"S0 · fast-path<br/>eligible?"}
  S0 -- "yes" --> FAST["NO_OPTIMIZATION<br/>free controls, overhead ≈ 0"] --> OUT
  S0 -- "no" --> SETUP["Baseline counterfactual · overhead cap<br/>classify · schedule advisors by<br/>expected savings ÷ overhead"]

  SETUP --> S1{"S1 · existing context<br/>sufficient?"}
  S1 -- "yes, confident" --> EXIT1["Answer from context"] --> OUT
  S1 -- "no" --> S2{"S2 · memory sufficient?<br/>VOI-gated, as_of freshness"}
  S2 -- "yes" --> EXIT2["Answer from memory"] --> OUT
  S2 -- "partial" --> ADD["Add hits to context"] --> S3
  S2 -- "no" --> S3{"S3 · reusable result<br/>or cache entry?"}
  S3 -- "hit, valid for scope+auth" --> EXIT3["Answer from cache"] --> OUT
  S3 -- "miss" --> S4{"S4 · tool required<br/>at all?"}

  S4 -- "no" --> S9
  S4 -- "yes" --> S5{"S5 · code-execution<br/>bypass wins?"}
  S5 -- "yes, conservatively" --> BYP["CODE_BYPASS step<br/>skip S6 to S8"] --> S9
  S5 -- "no" --> S6["S6 · tools + disclosure level<br/>compare churn_cost against<br/>schema tokens saved"]

  S6 --> S7["S7 · dependency graph<br/>independent groups parallelize<br/>uncertain ⇒ sequential"]
  S7 --> S8["S8 · delegation<br/>native · router · sub-agent<br/>parallel · escalate"]
  S8 --> S9["S9 · model + reasoning effort<br/>per step, not per turn"]

  S9 --> TK{"tokenizer<br/>family changed?"}
  TK -- "yes" --> RECOMP["Recompute every budget<br/>verify against bytes bound"] --> S10
  TK -- "no" --> S10["S10 · context sizing<br/>score · disposition · dependency closure"]

  S10 --> S11{"S11 · budget band?"}
  S11 -- "EMERGENCY" --> CNOW["COMPACT_NOW<br/>economics suspended, logged as emergency"]
  S11 -- "COMPACT / AGGRESSIVE" --> TAIL{"tail externalization<br/>sufficient?"}
  S11 -- "NORMAL / PRUNE" --> DEFER1["DEFER"]
  TAIL -- "yes" --> EXT["Externalize the uncached tail<br/>zero cache invalidation"]
  TAIL -- "no" --> BEN{"compaction_benefit > 0<br/>net of invalidation?"}
  BEN -- "yes" --> CNOW
  BEN -- "no" --> DEFER2["DEFER to task transition<br/>batch pending compactions"]

  CNOW --> S12
  EXT --> S12
  DEFER1 --> S12
  DEFER2 --> S12

  S12["S12 to S14 · result budgets, representation,<br/>cache policy, idempotency, output controls"] --> S15{"S15 · output budget<br/>sufficient?"}
  S15 -- "no, first attempt" --> S10
  S15 -- "no, second attempt" --> DEM["mode = DEMOTED"] --> OUT
  S15 -- "yes" --> VAL{"validate plan<br/>against invariants"}
  VAL -- "fail" --> DEM
  VAL -- "pass" --> OUT(["ExecutionPlan + trace + overhead"])

  classDef exit fill:#e9f7ef,stroke:#2e8b57,color:#14432a
  classDef warn fill:#fdeaea,stroke:#c0392b,color:#5c1a13
  classDef good fill:#e8f0fe,stroke:#3b6fd4,color:#12305e
  class FAST,EXIT1,EXIT2,EXIT3 exit
  class DEM,CNOW warn
  class BYP,EXT good
```

The four green terminals on the left are the turns that cost almost nothing. A deployment where they
fire rarely is one where either the thresholds are miscalibrated or the workload genuinely requires
tools every turn — and `fast_path_blocked_by` in the decision telemetry distinguishes the two.

---

## 4. Concurrency and the deterministic reducer

Why parallel branches never touch shared state, and why the result does not depend on which branch
finishes first.

```mermaid
sequenceDiagram
  autonumber
  participant DE as C8 Decision Engine
  participant DI as C11 Dispatcher
  participant FC as C16 Failure Controller
  participant B1 as Branch 1
  participant B2 as Branch 2
  participant B3 as Branch 3 (sub-agent)
  participant RD as C13 Reducer
  participant ST as C17 / C18 State

  DE->>DI: PARALLEL step + ReducerSpec<br/>order = DEPENDENCY_THEN_DISPATCH
  DI->>FC: checkpoint(working state)
  FC-->>DI: ck0

  par independent branches, isolated working state
    DI->>B1: dispatch idx=0, no write capability
  and
    DI->>B2: dispatch idx=1, no write capability
  and
    DI->>B3: dispatch idx=2, minimal context, result_schema, trust_ceiling
  end

  B2-->>DI: structured result (finishes first)
  B3-->>DI: structured result
  B1--xDI: failure
  DI->>FC: classify + normalize
  FC-->>DI: one-line error, details to archive ref
  FC->>DI: rollback branch 1 to ck0 — no residue in context

  DI->>RD: results [idx 0 = error, idx 1, idx 2]
  Note over RD: reorder by dependency, then by<br/>dispatch index — never completion order
  RD->>RD: deduplicate overlapping facts
  RD->>RD: resolve conflicts by explicit precedence<br/>unresolved ⇒ surface as disputed
  RD->>ST: ONE atomic commit at the aggregation point
  ST-->>RD: committed
  RD-->>DE: ReducedResult + accounting
```

Step 12 is the property that makes multi-branch turns debuggable: rerun the same turn a thousand
times with branches completing in a thousand different orders and the committed state is identical.
Ordering by completion produces a system whose bugs cannot be reproduced, which on a stateful agent
is indistinguishable from a system that is simply wrong sometimes.

Note also that branch 1's failure leaves **nothing** behind. Its partial output is not summarized,
not mentioned, not retained "for context" — it is rolled back, and a single normalized line records
what happened. Partial output from a failed branch is one of the more effective ways to produce a
confidently wrong answer three turns later.

---

## 5. Projection layering and the cache boundary

What the model request is made of, where the cacheable prefix ends, and why reduction targets the
tail before the head.

```mermaid
flowchart TB
  subgraph PROJ["Model request · built by C10, most-static first"]
    direction TB
    L1["STATIC · system instructions<br/>changes on deploy only"]
    L2["STATIC · skill instructions<br/>hard size ceiling, deploy-gated"]
    L3["SESSION · tool schemas<br/>compact typed signatures, not raw JSON Schema"]
    L4["SESSION · long-term memory<br/>rehydrated with trust labels"]
    L5["TASK · conversation history<br/>APPEND-ONLY, never cosmetically rewritten"]
    L6["TURN · current input, tool results,<br/>drop manifests, normalized errors"]
    L1 --> L2 --> L3 --> L4 --> L5 --> L6
  end

  BP1["breakpoint 1"] -.-> L2
  BP2["breakpoint 2"] -.-> L4
  BP3["breakpoint 3 · last"] -.-> L5

  subgraph HEAD["CACHED HEAD · editing anything here invalidates everything after it"]
    H1["cost of removing 1 token here =<br/>token saved MINUS discount lost on<br/>every downstream token, every turn"]
  end

  subgraph TAILZ["UNCACHED TAIL · editing here invalidates nothing"]
    T1["cost of removing 1 token here =<br/>token saved. Full stop."]
  end

  L4 -.-> HEAD
  L6 -.-> TAILZ

  PRESSURE(["budget pressure"]) --> ORDER
  ORDER["Reduction order · §5.1 of the decision engine"]
  ORDER --> O1["1 · externalize large TURN items in the tail"]
  O1 --> O2["2 · externalize TASK items after the last breakpoint"]
  O2 --> O3["3 · convert cheap-to-reconstruct items to RETRIEVE_LATER"]
  O3 --> O4["4 · compact the cached head<br/>ONLY if compaction_benefit > 0<br/>or the band is EMERGENCY"]

  classDef head fill:#fdeaea,stroke:#c0392b,color:#5c1a13
  classDef tail fill:#e9f7ef,stroke:#2e8b57,color:#14432a
  class H1 head
  class T1 tail
```

The asymmetry between the two boxes is the whole point. A token in the tail and a token in the head
look identical in a token count and have completely different economics, and no amount of careful
scoring will find that difference if position relative to the cache breakpoint is not an input.

---

## 6. Failure and demotion

INV-2 as a control-flow diagram. Every optimizer component has a path back to a correct, more
expensive turn; two components deliberately fail closed instead.

```mermaid
flowchart TB
  T(["Turn begins"]) --> G["C0 guard: deadline + declared cost + fallback value"]
  G --> RUN{"component<br/>outcome?"}
  RUN -- "ok" --> OK["contribution merged into the plan"]
  RUN -- "throws / times out / overspends" --> CLASS{"which component?"}

  CLASS -- "C21 Security" --> CLOSED["FAIL CLOSED<br/>content not admitted to context,<br/>memory or archive"]
  CLASS -- "C24 Gate Runner" --> BLOCK["FAIL CLOSED on change<br/>block promotion of unmeasured flags<br/>runtime unaffected"]
  CLASS -- "any other component" --> OPEN["FAIL OPEN · demote"]

  OPEN --> D["neutral fragment or pass-through behaviour<br/>recorded in the plan trace"]
  D --> COST["turn completes correctly, costs more"]
  COST --> LEDGER["C22 records DEMOTED with a named cause<br/>cost increase is attributable"]
  LEDGER --> BRK{"repeated failures<br/>past the breaker threshold?"}
  BRK -- "no" --> HEALTH["health check → re-ENABLE"]
  BRK -- "yes" --> DIS["DISABLED + alarm<br/>component stays out until fixed"]

  OK --> DONE(["Turn completes"])
  COST --> DONE
  CLOSED --> REFUSE(["that content path refused,<br/>deliberately"])
  BLOCK --> DONE

  SILENT["C23 Estimator Bias Monitor"] -.-> NOTE["the one demotion nothing downstream notices:<br/>estimates continue unvalidated.<br/>Its own liveness alarm is part of<br/>the operational contract."]

  classDef bad fill:#fdeaea,stroke:#c0392b,color:#5c1a13
  classDef ok fill:#e9f7ef,stroke:#2e8b57,color:#14432a
  classDef warn fill:#fdf3e3,stroke:#c9852a,color:#5a3a06
  class CLOSED,BLOCK,DIS bad
  class OK,DONE,HEALTH ok
  class D,COST,LEDGER,NOTE warn
```

Read the diagram as an availability argument: there is no path from an optimizer fault to a failed
turn. There are paths to an *expensive* turn, and every one of them is named in the ledger. The two
red terminals are the deliberate exceptions — a security control that fails open is not a control,
and a gate that fails open is not a gate.

---

## 7. Diagram index

| # | Diagram | Answers |
|---|---|---|
| 1 | Master architecture | What are the parts, and what talks to what? |
| 2 | Turn state machine | What happens, in what order, within one turn? |
| 3 | Decision engine | How is a single turn's plan decided, and where does it exit early? |
| 4 | Concurrency and reducer | Why is a multi-branch turn deterministic? |
| 5 | Projection and cache | Why does *where* a token sits matter more than how many there are? |
| 6 | Failure and demotion | What happens when the optimizer breaks? |
