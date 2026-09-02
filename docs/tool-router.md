# Deliverable 5 — Tool Router

**Stage 4 of 5.** Production pseudocode for tool discovery, ranking, disclosure and selection (C5) —
cache-stability-aware throughout.

Companion documents: [`architecture.md`](architecture.md) (C5) ·
[`decision-engine.md`](decision-engine.md) (§17 S6) · [`model-router.md`](model-router.md) (D6) ·
[`configuration.md`](configuration.md) (D9) ·
[`../skill/references/tools.md`](../skill/references/tools.md) (the runtime instruction).

---

## 1. The tension this component exists inside

Progressive disclosure and prompt caching pull in opposite directions, and the tool layer is where
they collide hardest. The projection order is fixed (V4):

```text
system → skill → TOOL SCHEMAS → long-term memory → conversation history → current turn
             ↑
             any edit here invalidates everything to its right
```

The tool layer sits **early in the prefix**, so changing it invalidates memory, history and the
current turn — the three layers that grow. Which produces the governing asymmetry:

> **A tool-set change is cheap early in a session and expensive late.** At turn 2 there is almost
> nothing to its right. At turn 40 there may be 60,000 cached tokens, all of which re-bill at full
> price.

Everything below follows from that. The router does not ask "which tools are most relevant?" — it
asks "which tools are most relevant, and is now an affordable moment to change my mind?"

### A tension worth measuring

The fixed layer order is inherited from the specification (§9) and pinned by V4, and this design
implements it as given. It is worth recording that within a *session* the ordering is arguably
inverted: long-term memory is rehydrated once at session start and then static, while tool schemas
may change per task — which would argue for `memory → tool_schemas`. Reordering would make tool churn
strictly cheaper by moving it later in the prefix.

I have not changed it, because the order is a specification constraint and a load-time invariant, and
because the benefit depends on how often tool sets actually change in a given workload. It belongs in
the Stage 5 benchmark suite as an A/B, not in a design decision made without data. The mitigations
available *within* the fixed order are front-loading (§5) and change-at-boundary (§6), and they are
what this router implements.

---

## 2. The registry

Metadata only. Full schemas live behind it and are never loaded to decide whether to load them.

```ts
interface ToolRegistryEntry {
  id: ToolId;
  name: string;
  summary: string;                    // one line — this is Level 0
  capabilities: string[];             // domain tags, matched against the task
  side_effects: 'READ' | 'WRITE' | 'MUTATE';
  idempotent: boolean;

  // Contract facts the middleware and the planner both need
  upstream: {                         // what reduction this tool supports before the call
    field_selection: boolean; pagination: boolean; filtering: boolean;
    sorting: boolean; aggregation: boolean; verbosity_param?: string;
  };
  identity_fields: FieldPath[];       // primary keys, for dedupe (D3 §7)
  join_keys: FieldPath[];

  // Sizes at each disclosure level, measured not guessed
  level_tokens: { 0: number; 1: number; 2: number; 3: number };
  schema_hash: string;                // changes ⇒ cached signature invalid

  // Cost priors, refreshed from the ledger
  result_size: { p50: number; p95: number };
  non_token_cost_usd: number;
  latency: { p50_ms: number; p95_ms: number };
  success_rate: Estimate<number>;

  equivalents: ToolId[];              // fallbacks, for the escalation ladder rung 4
}
```

`level_tokens` being **measured** matters more than it looks. Disclosure decisions are arithmetic
comparing schema tokens against churn cost; doing that arithmetic against an estimate of a schema's
size, when the schema is sitting right there and could have been measured once at registry build
time, is a needless source of error in a decision that is already close.

---

## 3. `discoverTools`

```text
function discoverTools(task, ctx) -> ToolCandidate[]:

  # Index lookup only. No schema is read, at any level above 0.
  pool <- registry.index.query(
              terms        = task.intent ++ task.capabilities,
              scope        = ctx.scope,               # tenant-visible tools only
              limit        = cfg.tools.registry.candidate_pool)

  pool <- [ t for t in pool if authorized(t, ctx.auth) ]
  pool <- [ t for t in pool if not circuitBreaker(t).isOpen() ]
  pool <- [ t for t in pool if capabilityScore(t, task) >= cfg.tools.registry.min_capability_score ]

  # Tools already in the active projection are always candidates, even if they
  # score below the floor. Dropping a loaded tool costs cache; keeping it costs
  # only its Level-0 line.
  pool <- pool ++ ctx.active_projection.tools

  return dedupe(pool)
```

**The last step is the cache-aware part.** A tool that has fallen out of relevance is *not* removed
from the candidate pool, because removing it from the projection is the expensive operation. It is
carried forward and only dropped when §6 says a change is affordable.

**Config:** `tools.registry.candidate_pool`, `tools.registry.min_capability_score`.
**Demotion:** return every authorized tool. Maximum candidates, zero discovery risk.
**Complexity:** one index query, O(pool).

---

## 4. `rankTools`

```text
function rankTools(cands, task, ctx) -> RankedTool[]:

  for t in cands:
      capability <- capabilityScore(t, task)              # lexical + tag overlap, deterministic
      history    <- ledger.priors(task.workload).tool_success[t.id]   # EWMA, wide when cold
      upstream   <- upstreamBonus(t)                      # tools that filter server-side rank higher
      cost_pen   <- 1 / (1 + normalizedCost(estimateToolCost(t, task, ctx)))
      incumbency <- t in ctx.active_projection.tools
                      ? cfg.tools.router.incumbency_bonus : 1.0

      t.rank <- capability * history.lo * upstream * cost_pen * incumbency

  ranked <- sortDescending(cands, by rank)

  # A model-based reranker runs only if the advisor scheduler funded it AND the
  # deterministic ranking is ambiguous — the top two within a hair of each other.
  if scheduler.includes(TOOL_RERANK) and ambiguous(ranked, cfg.tools.router.rerank_margin):
      ranked <- modelRerank(ranked[0..cfg.tools.router.rerank_top_k], task,
                            model = tier(2), reasoning = NONE,
                            max_tokens = cfg.budgets.max_tokens_by_operation_class.routing)

  return take(ranked, cfg.tools.router.k_candidates)
```

**`upstreamBonus` is the highest-leverage ranking term and the least obvious.** A tool that accepts
`fields=` and `limit=` lets reduction happen before transfer; an equivalent tool that returns
everything forces the payload middleware to work after the cost has already been paid. Two tools with
identical capability are not equally good, and the difference can be an order of magnitude in tokens.

**`history.lo`, not `history.mid`.** A tool with no track record ranks on its pessimistic bound, so
untested tools do not displace proven ones on optimism. Same discipline as the advisor scheduler.

**The reranker is doubly gated:** funded by the scheduler *and* only when the deterministic ranking is
genuinely close. A router LLM call that costs more than the schema tokens it saves is one of the
named anti-patterns, and "only run it when it might change the answer" is what keeps it honest.

---

## 5. `translateSchemaToSignature`

```text
function translateSchemaToSignature(schema, style) -> CompactSignature:

  cached <- cache.schema.get(schema.hash, style)
  if cached: return cached

  params <- []
  for (name, prop) in schema.properties:
      required <- name in schema.required
      params.add(name + (required ? "" : "?") + ": " + typeOf(prop))

  sig <- schema.name + "(" + join(params, ", ") + "): " + typeOf(schema.returns)

  # Enums, constraints and defaults are carried only where they change what a
  # caller can legally pass. Descriptions are dropped at Level 1 and kept at 2.
  for (name, prop) in schema.properties where prop.enum or prop.default or prop.pattern:
      sig += "\n  " + name + ": " + constraintLine(prop)

  cache.schema.put(schema.hash, style, sig, ttl = cfg.cache.schema.ttl_seconds)
  return sig


function typeOf(prop) -> string:
  match prop:
    {type: "string", enum: E}      -> join(E, "|")
    {type: "string", format: F}    -> F                      # date-time, uri, uuid
    {type: "string"}               -> "string"
    {type: "integer"|"number"}     -> "number"
    {type: "boolean"}              -> "boolean"
    {type: "array", items: I}      -> typeOf(I) + "[]"
    {type: "object", properties:P} -> depth < cfg.tools.router.signature_max_depth
                                        ? "{" + join(P.map(inline), ", ") + "}"
                                        : "object"
    {oneOf|anyOf: A}               -> join(A.map(typeOf), " | ")
    _                              -> "unknown"
```

Applied to a typical JSON Schema:

```json
{ "$schema": "…", "type": "object", "additionalProperties": false,
  "properties": {
    "query":  { "type": "string", "description": "The search query text…" },
    "limit":  { "type": "integer", "minimum": 1, "maximum": 100, "default": 20 },
    "fields": { "type": "array", "items": { "type": "string" } },
    "status": { "type": "string", "enum": ["open", "closed", "all"] } },
  "required": ["query"] }
```

becomes

```text
search(query: string, limit?: number, fields?: string[], status?: open|closed|all): Result[]
  limit: 1..100, default 20
```

**Where the savings come from.** The reduction is 40–60% and almost none of it is cleverness — it is
deleting `"$schema"`, `"type": "object"`, `"properties"`, `"additionalProperties"`, the punctuation of
nested JSON, and prose descriptions that restate the parameter name. What survives is what changes
what a caller can legally pass.

**Scope.** This is the *context* representation. The provider's tool-calling API may still require
full JSON Schema on the wire; the translation applies to Level 0–2 renderings, registry text, and the
importable stubs handed to the code-execution sandbox. Confusing the two produces a router that saves
tokens and breaks tool calls.

**Cache key is the schema hash, not the tool id.** A tool whose schema changes upstream gets a new
signature automatically; a tool that is merely renamed does not pay for a re-translation.

---

## 6. `loadSchema`, `unloadSchema`, and what those names actually mean

The deliverable names these functions, and Stage 1 committed to modelling the mechanism as
**projection, not load/unload** (spec §2). Both are true, and the honest implementation says so:
there is no persistent agent memory to load into. `loadSchema` produces a rendering for the *next*
projection; `unloadSchema` marks a tool absent from it.

```text
function loadSchema(tool, level, ctx) -> RenderedSchema:

  hit <- cache.schema.get(tool.id, level, tool.schema_hash)
  if hit: return hit

  raw <- registry.fetchSchema(tool.id)                    # the only place a full schema is read
  rendered <- match level:
      0 -> tool.name + " — " + tool.summary
      1 -> translateSchemaToSignature(requiredOnly(raw), cfg.tools.router.signature_style)
      2 -> translateSchemaToSignature(raw, cfg.tools.router.signature_style)
      3 -> translateSchemaToSignature(raw, ...) + examplesFor(tool)

  rendered.size <- estimateTokens(rendered, ctx.model.tokenizer)
  cache.schema.put(...)
  return rendered


function unloadSchema(tool, ctx) -> ProjectionDelta:
  # Nothing is unloaded. A delta is recorded and applied when §7 decides the
  # next projection, and its price is the tokens that re-bill downstream.
  return ProjectionDelta {
      op: DROP, tool: tool.id,
      invalidates_from: positionOf(tool, ctx.active_projection),
      cost: cachedTokensAfter(ctx.cache, positionOf(tool, ctx.active_projection))
            * inputPrice(ctx.model) * providerCapabilities(ctx.model).cached_input_discount
            * expectedRemainingTurns(ctx)
  }
```

**Dropping is more expensive than adding, and both are expensive late.** A drop invalidates from the
dropped tool's position — which includes every tool after it, plus memory, history and the turn. An
add appends to the end of the tool layer and invalidates from there. Neither is free, and the
`ProjectionDelta` carries the price so §7 can refuse.

---

## 7. `projectTools` — the cache-stability decision

```text
function projectTools(ranked, ctx) -> ToolProjection:

  current <- ctx.active_projection
  desired <- take(ranked, cfg.tools.router.max_active_tools)

  adds  <- desired \ current.tools
  drops <- current.tools \ desired

  # --- 1. No change: the cheapest outcome, and the common one --------------
  if adds.isEmpty() and drops.isEmpty():
      return current.unchanged()

  # --- 2. Is this an affordable moment? ------------------------------------
  at_boundary <- ctx.is_session_start or ctx.is_task_transition or not ctx.cache.prefix_warm
  within_window <- ctx.turns_since_last_tool_change < cfg.tools.router.stability_window_turns

  if not at_boundary and within_window and not forced(adds, ctx):
      # Too soon. Escalate disclosure on tools already present instead — that
      # is a change to an existing entry, not a set change, and costs the same
      # invalidation, so only do it if the step genuinely needs the schema.
      return current.withDisclosureFor(ctx.imminent_calls)

  # --- 3. Price the change --------------------------------------------------
  churn <- sum(unloadSchema(t, ctx).cost for t in drops)
         + appendCost(adds, ctx)

  saved <- sum(t.level_tokens[2] for t in drops)          # schema tokens no longer carried
           * inputPrice(ctx.model) * expectedRemainingTurns(ctx)

  if churn.hi > saved.lo / cfg.tools.router.churn_margin:
      # Keeping a slightly larger, stable set is cheaper than a precise, churning one.
      stable <- current.tools ++ adds                     # add only; never drop
      trace(TOOL_PROJECTION, chosen = STABLE, churn, saved)
      return build(stable, ctx, prefix_stable = adds.isEmpty())

  trace(TOOL_PROJECTION, chosen = CHURN, churn, saved)
  return build(desired, ctx, prefix_stable = false)


function build(tools, ctx, prefix_stable) -> ToolProjection:
  entries <- []
  for t in stableOrder(tools):                            # deterministic: insertion order, then id
      level <- disclosureLevel(t, ctx)                    # §8
      entries.add({ id: t.id, level, rendered: loadSchema(t, level, ctx) })
  return ToolProjection { tools: entries, prefix_stable,
                          churn_cost: churnOf(entries, ctx),
                          size: sum(e.rendered.size for e in entries) }
```

**`stableOrder` is load-bearing.** Two projections containing the same tools must render identically
byte for byte, or the prefix breaks for no reason at all. Sorting by insertion order and then by id
means a tool that stays gets the same position it had, and a new tool appends.

**The asymmetric fallback in branch 3 is the point.** When churn is too expensive the router does not
abandon the change — it takes the *additions* and refuses the *removals*. Additions are what unlock
new capability; removals only save schema tokens. Paying to carry an unused schema is usually cheaper
than paying to remove it.

**`forced(adds, ctx)`** overrides the stability window when a step cannot proceed without a tool: a
required capability that no active tool provides. Availability beats cache economics, and the event is
recorded so a workload that forces changes constantly shows up as one that needs a wider active set
rather than a tighter stability window.

**Config:** `tools.router.{max_active_tools, stability_window_turns, churn_margin, churn_tolerance_usd}`.
**Demotion:** project every candidate at Level 2 in id order and never change it again for the
session. Maximum schema tokens, perfect stability, zero routing risk.

---

## 8. `disclosureLevel`

```text
function disclosureLevel(tool, ctx) -> 0|1|2|3:

  if tool.id in ctx.failed_last_turn and ctx.failure_class == SCHEMA_VIOLATION:
      return 3                                            # examples, because the signature was not enough
  if tool.id in ctx.imminent_calls:      return 2         # about to be called: full signature
  if tool.id in ctx.plan_candidates:     return 1         # under consideration: required params
  return cfg.tools.router.default_disclosure_level        # 0: name and one line
```

Four levels, escalated on evidence rather than on a schedule. The Level-3 rule is the one that earns
its place: a tool call that failed on a schema violation is the one case where prose examples reliably
fix what a signature could not convey, and it is bounded — the escalation lasts one turn.

---

## 9. `selectTool`

```text
function selectTool(step, projection, ctx) -> ToolChoice | Skip:

  cands <- [ t for t in projection.tools if satisfies(t, step.capability) ]
  if cands.isEmpty(): return Skip { reason: NO_CAPABLE_TOOL }

  # --- Value of information: is this call worth making at all? -------------
  for t in cands:
      cost <- estimateToolCost(t, step, ctx)
      p    <- P(result changes the decision | task class, prior hit rate, step)
      value <- p * cfg.tools.necessity.value_of_correct_decision_usd[ctx.task.risk]
      t.niv <- value - (cost.usd_llm + cost.usd_non_token)

  best <- argmax(cands, t => t.niv)

  if cfg.tools.necessity.require_positive_information_value and best.niv.hi <= 0:
      return Skip { reason: NEGATIVE_INFORMATION_VALUE, best_was: best.id }

  # --- Safety gates ---------------------------------------------------------
  if best.side_effects != READ:
      require(step.idem != null, "mutation without an idempotency key")
      require(not ctx.speculative, "no side effects on a speculative branch")
  if circuitBreaker(best).isOpen():
      alt <- firstAvailable(best.equivalents, projection)
      if alt == null: return Skip { reason: CIRCUIT_OPEN }
      best <- alt

  return ToolChoice { tool: best, level: 2, args: bindArgs(step, best),
                      upstream: upstreamParamsFor(best, step),   # fields=, limit=, filter=
                      budget: cfg.budgets.per_tool[best.id] ?? cfg.budgets.per_tool.default }
```

**`niv.hi <= 0`, not `niv.mid`.** A call is skipped only when it is unprofitable on its *optimistic*
bound — the reverse of the compaction gate's pessimism, and deliberately so. Skipping a call that
would have helped costs correctness; making a call that was not needed costs tokens. INV-9 resolves
toward correctness, so uncertainty here buys the call.

**`upstreamParamsFor` runs here, not in the middleware.** Field selection, limits and filters are
bound at selection time from the tool's declared `upstream` capabilities. This is the difference
between reducing a payload and never generating it, and it is worth an order of magnitude more than
everything the payload middleware can do afterwards.

---

## 10. `estimateToolCost`

```text
function estimateToolCost(tool, step, ctx) -> Estimate<TokenCost>:

  # Result size: ledger history for this tool AND this argument shape, because
  # `search(limit=10)` and `search(limit=1000)` are not the same tool call.
  shape <- argShapeKey(step.args)
  hist  <- ledger.resultSizes(tool.id, shape) ?? tool.result_size

  raw_tokens <- Estimate { lo: hist.p50, mid: hist.p50 * 1.4, hi: hist.p95 }
  budget     <- cfg.budgets.per_tool[tool.id] ?? cfg.budgets.per_tool.default
  in_tokens  <- min(raw_tokens, budget)                   # the middleware caps it
  overflow   <- max(0, raw_tokens - budget)               # reduction work, not free

  return Estimate {
      llm_input:  in_tokens,
      llm_output: tool.side_effects == READ ? 0 : callArgumentTokens(step),
      usd_llm:    in_tokens * inputPrice(ctx.model),
      usd_non_token: tool.non_token_cost_usd
                   + reductionCost(overflow, ctx),        # summarization calls, if any
      latency_p50_ms: tool.latency.p50_ms,
      latency_p95_ms: tool.latency.p95_ms
  }
```

**Two terms most cost models omit.** `usd_non_token` carries the API charge, database compute or
search fee — the reason the cheapest operation is not the one with the fewest tokens. And `overflow`
prices the *reduction work* a large result forces: a tool that reliably returns 40,000 tokens against
a 4,000-token budget costs a summarization call every time it is used, and a model that ignores that
will keep choosing it over a tool that returns 3,000 tokens directly.

**Keyed by argument shape.** Bucketing by tool alone makes the p95 of a paginated search meaningless.
`argShapeKey` buckets on the parameters that drive size — limit, depth, date range, field count.

---

## 11. Failure and demotion

| Function | Failure | Demotion | Cost |
|---|---|---|---|
| `discoverTools` | Index unavailable | Every authorized tool becomes a candidate | Larger projection |
| `rankTools` | Priors or reranker unavailable | Capability score alone, incumbents first | Suboptimal selection |
| `translateSchemaToSignature` | Unparseable schema | Emit raw JSON Schema for that tool | 2–3× the tokens for one tool |
| `loadSchema` | Registry fetch fails | Level 0 only; the tool cannot be called this turn | Capability lost for the turn |
| `projectTools` | Cache state unavailable | Return the current projection unchanged | Stale tool set, warm cache |
| `disclosureLevel` | — | Level 2 for everything | More schema tokens |
| `selectTool` | Cost model unavailable | Highest-ranked capable tool, no VOI gate | Unnecessary calls |
| `estimateToolCost` | No history | Registry priors with a wide interval | Conservative choices |

Every row keeps the turn executable. The most expensive demotion — everything at Level 2, never
changed — is also the most cache-stable, which is a pleasant accident of the design rather than a
coincidence: the fallback for a cache-aware router is the maximally cache-stable behaviour.

---

## 12. Worked example

Turn 12 of a research session. The task shifts from *search* to *summarise and file*, so the ranking
now wants `docs.fetch` and `notes.create` and no longer wants `web.search`.

```text
active projection      web.search(L2) · docs.search(L2) · docs.fetch(L1)      2,340 tok
ranked for this task   docs.fetch · notes.create · docs.search · web.search

adds   notes.create (L2, 410 tok)
drops  web.search   (L2, 980 tok)

turns_since_last_tool_change = 3   (stability_window_turns = 8)
is_task_transition           = true   → the window does not apply

price the change:
  drop web.search    invalidates from its position: 38,400 cached tok downstream
                     38,400 × $3/M × 0.90 × 5 remaining turns          = $0.518
  append notes.create invalidates from end of tool layer: 34,900 tok
                     34,900 × $3/M × 0.90 × 5                          = $0.471
  churn total                                                          = $0.989

  saved: 980 schema tok × $3/M × 5                                     = $0.015

  churn.hi ($0.989) > saved.lo ($0.015) / 1.5   → STABLE branch
  → add notes.create, keep web.search
```

The router adds the capability it needs and refuses the removal. It pays 410 tokens per turn to carry
a tool it will not call, which over five turns costs $0.006 — against $0.518 to remove it. **Carrying
a useless schema is 80× cheaper than deleting one.**

This is the anti-pattern from §24 of the specification, caught by arithmetic rather than by a rule:
dynamic tool loading and unloading that churns the cached prefix every turn. Note also what the
example does *not* show — a model call. The whole decision is four multiplications against numbers
already in the registry and the cache state.

---

## 13. Test hooks (for D11)

| Test | Asserts |
|---|---|
| Byte-identical rendering | Two projections with the same tool set render identically |
| Append-only additions | Adding a tool never reorders existing entries |
| Churn refusal | On a warm prefix with cheap schemas, drops are refused and adds accepted |
| Forced override | A required capability with no active provider changes the set regardless of window |
| Signature fidelity | Every enum, required flag and constraint survives translation, across a schema corpus |
| Signature savings | Median reduction against raw JSON Schema is within the 40–60% band |
| Schema cache keying | A schema change upstream invalidates the cached signature; a rename does not |
| VOI asymmetry | Calls are skipped only when unprofitable on the optimistic bound |
| Upstream binding | Every selected tool supporting `fields`/`limit` has them bound |
| Mutation safety | No non-idempotent tool is selected without a key, or on a speculative branch |
| Cost keying | Result-size priors differ across argument shapes for the same tool |
