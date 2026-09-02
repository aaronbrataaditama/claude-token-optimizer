# Deliverable 9 — Configuration

**Stage 2 of 5.** The configuration structure, its layering and precedence rules, provider awareness,
the load-time validation invariants, and what may change at runtime.

Files: [`config/optimizer.defaults.yaml`](../config/optimizer.defaults.yaml) ·
[`config/optimizer.schema.json`](../config/optimizer.schema.json) ·
[`config/profiles/`](../config/profiles)

Companion documents: [`architecture.md`](architecture.md) (D2) ·
[`decision-engine.md`](decision-engine.md) (§17) · [`../skill/SKILL.md`](../skill/SKILL.md) (D1).

---

## 1. The rule

**No component reads a literal that is not routed through this configuration.** Thresholds, budgets,
tiers, TTLs, limits, weights and policies are all values, not constants — and every one is
model- and provider-aware, because a number that is correct for one window size, one tokenizer and
one caching regime is wrong for the next.

There is exactly one exception, and it is deliberate: **the invariants themselves are not
configurable.** A deployment can set the compaction threshold to any value; it cannot configure away
the requirement that compaction be priced against cache invalidation. Section 6 lists what that means
in practice — twenty-seven configurations that the system refuses to start with.

---

## 2. Layering and precedence

Five layers, merged deeply, later layers winning:

```text
1. shipped defaults          config/optimizer.defaults.yaml
2. provider profile          resolved from the startup capability probe
3. deployment profile        config/profiles/<workload>.yaml
4. tenant override           per-tenant, supplied by the host
5. runtime flags             per-optimization on / off / shadow, for rollback
```

**Merge semantics.** Maps merge key by key. Scalars replace. Arrays replace wholesale *except* where
an invariant marks them append-only — `context.disposition.never_compress` and
`security.trust.propagate_through` are floors, and a later layer may add entries but never remove one
(V14). This distinction matters: array-replace is the right default because a profile redefining the
threshold bands wants to redefine all five, but array-replace on the never-compress list is how a
deployment quietly gains permission to summarize code.

**Direction of travel.** Later layers may tighten limits freely. Loosening is permitted for
performance knobs and refused for security ones (V2). A tenant override cannot widen tenant
isolation, disable sanitization, or raise a trust ceiling; it can lower a budget, narrow a threshold,
or turn an optimization off.

**Layer 5 exists for rollback, not tuning.** Flags carry three states — `on`, `off`, and `shadow`
(runs, logs, but does not affect the turn). Every mechanism is individually switchable so that a
regression can be isolated to one optimization and disabled without redeploying.

---

## 3. Provider awareness

The capability probe runs **at startup, not at first use**, so the first turn of a deployment is not
the one that discovers that prompt caching behaves differently from what the config claims.

```yaml
providers:
  entries:
    <provider-id>:
      prompt_cache_supported: true
      cached_input_discount: 0.90     # cached input costs 10%
      max_breakpoints: 4
      min_prefix_tokens: 1024
      cache_ttl_seconds: 300
      structured_output: true
      window: 200000
      tokenizer: anthropic
      native: [prompt_caching, structured_output]
```

Three configuration values resolve from this block rather than being written down:
`cache.prefix.max_breakpoints`, `min_prefix_tokens` and `ttl_seconds` all carry the sentinel
`provider`. Writing a literal there is how a config silently becomes wrong after a provider update.

### The native-capability rule

`native` lists optimizations the provider already performs. **If a capability is native, the
middleware equivalent is disabled** — the flag must be `off` or `shadow` — unless a recorded
benchmark shows the middleware version wins (V11). Reimplementing a native capability normally adds
overhead and can make things worse, and this rule turns that from advice into a load-time check.

### Tokenizer binding

Budgets are fractions of the *resolved* window, so the same file is valid across models. What is not
portable is a computed budget: it is stamped with a tokenizer family and is invalid against any
other (INV-8). `models.handoff.recompute_budgets_on_tokenizer_change` cannot be set false (V5).

---

## 4. The configuration map

| Block | Controls | Spec |
|---|---|---|
| `fast_path` | The `NO_OPTIMIZATION` gate and the always-on free controls | §17 S0 |
| `overhead` | The optimizer's own cost cap and the advisor scheduler | §17 |
| `budgets` | Window split, per-tool, per-modality, `max_tokens` per operation class | §4, §13, §15 |
| `thresholds` | The five utilization bands and their hysteresis pairs | §4 |
| `context` | Scoring weights, dispositions, dependency closure, the never-compress floor | §5, §6 |
| `compression` | Aggressiveness levels, summary validation, the compaction policy | §6, §9 |
| `middleware` | The eight-rung reduction ladder and its per-rung policies | D3 |
| `estimation` | Exact-versus-sampled token counting and its safety multiplier | D3 §11 |
| `context_manager` | Scoring priors, summary-validation rules, checkpoint limits | D4 |
| `cache` | Prefix layer order, TTLs, breakpoints, result / semantic / schema / retrieval caches | §9 |
| `tools` | Registry, disclosure levels, churn tolerance, necessity test, the bypass and its sandbox | §2, §3, §7 |
| `models` | Tiers, bindings, routing by operation class, reasoning effort, handoff rules | §10 |
| `delegation` | Depth, breadth, sub-agent contract requirements, delegation economics | §11 |
| `parallelism` | Branch limits, the reducer contract, speculation | §12 |
| `retry`, `guards`, `circuit_breaker` | Retry budgets, the escalation ladder, loop detection, breakers | §16 |
| `memory` | Extraction timing, retention, temporal validity, provenance, resumption, migration | §5 |
| `security` | Trust propagation, sanitization, isolation, retention, erasure | §19 |
| `telemetry` | Categories, baseline method, decision traces, estimator-bias alarm | §18 |
| `skill` | The always-loaded instruction budget — the meta-bloat guard | D1 |
| `gates` | Acceptance criteria, per-optimization gates, workload classes, rollout, drift | §20 |
| `flags` | Per-optimization `on` / `off` / `shadow` | §20 |
| `providers` | Capability probe, native-capability declarations | §9, §10 |

---

## 5. What the schema checks, and what it cannot

`optimizer.schema.json` enforces structure: types, enums, ranges, required keys,
`additionalProperties: false` on every block so a typo is an error rather than a silently ignored
key. It also pins a set of values with `const` — the ones that exist to make an invariant
unrepresentable, such as `reducer.order`, `trust.derived_level` and
`sanitization.before_external_write`.

What JSON Schema **cannot** express is any relationship between two fields: that `enter` must exceed
`exit`, that budget fractions must sum to at most one, that a native capability must disable its
middleware twin. Those are the twenty-seven invariants below, enforced by a validator that runs after
schema validation and before anything else starts.

---

## 6. Load-time validation invariants

Twenty-seven configurations that cannot be expressed. Each is a **hard failure at startup**, not a
warning — a warning in a log nobody reads is how a misconfigured optimizer runs for a month.

| # | Invariant | Rejection message |
|---|---|---|
| **V1** | Every band has `enter > exit`, and bands are strictly ordered ascending by `enter`. | `thresholds.bands[COMPACT]: enter 0.60 must exceed exit 0.60 — equal values cause compaction thrash` |
| **V2** | A later layer may only tighten `security.*`. Any loosening is refused. | `profiles/x.yaml: security.isolation.cross_tenant_reuse cannot be loosened from "forbidden"` |
| **V3** | `security.sanitization.before_external_write` cannot be false. | `sanitization before external write cannot be disabled — externalization creates durable copies` |
| **V4** | `cache.prefix.stable_layer_order` must be exactly the six layers, most-static first. | `cache.prefix.stable_layer_order: conversation_history precedes tool_schemas — this invalidates the prefix every turn` |
| **V5** | `models.handoff.recompute_budgets_on_tokenizer_change` cannot be false. | `budgets computed for one tokenizer are invalid for another (INV-8)` |
| **V6** | `delegation.max_depth > sane_depth_limit` requires `explicit_deep_delegation: true`. | `delegation.max_depth 4 exceeds sane_depth_limit 2 — set explicit_deep_delegation to acknowledge` |
| **V7** | Sub-agent contract requirements cannot be disabled: input allowlist, structured output, no skill injection, `trust_ceiling: min_of_inputs`. | `delegation.require_explicit_input_allowlist cannot be false — a sub-agent must never receive the primary context` |
| **V8** | `cache.semantic.enabled` requires validation covering both authorization- and time-sensitivity, and `forbid_when_scope_includes_user`. | `cache.semantic enabled without authorization_sensitive validation — personalized answers would be served across users` |
| **V9** | `parallelism.reducer.order` must be `DEPENDENCY_THEN_DISPATCH` with `atomic_commit: true`. | `reducer ordering by completion produces non-reproducible state (INV-4)` |
| **V10** | `baseline_method_default: MEASURED` requires a configured shadow or A/B harness. | `telemetry.baseline_method_default MEASURED requires gates.rollout.shadow_before_cutover` |
| **V11** | A capability listed in `providers.entries.*.native` must have its middleware flag `off` or `shadow`, unless a passing benchmark is recorded. | `flags.prompt_prefix_management is "on" but provider declares prompt_caching native and no benchmark overrides it` |
| **V12** | `fast_path.always_on` must retain all eight zero-cost controls. | `fast_path.always_on is missing drop_manifest — removing it makes the fast path strictly worse for no saving` |
| **V13** | `meta.memory_schema_version` must match the code, or a migration must exist. | `memory schema version 1 on disk, 2 in code, no migration registered — refusing to start` |
| **V14** | `context.disposition.never_compress` is a floor. Entries may be added, never removed. | `profiles/x.yaml removes "identifiers" from never_compress — the shipped list is a floor` |
| **V15** | Measured always-loaded instruction tokens must not exceed `skill.max_tokens`. | `skill/SKILL.md measures 1,140 tokens against a ceiling of 800 — the optimizer would ship with the bloat it exists to remove` |
| **V16** | Budget fractions must sum to at most 1.0. | `budgets fractions sum to 1.07 — over-allocating the window guarantees overflow` |
| **V17** | `fast_path.max_utilization` must be below the `PRUNE` enter threshold. | `fast_path.max_utilization 0.55 sits inside the PRUNE band — the fast path would fire on turns already under budget pressure` |
| **V18** | `middleware.ladder.rungs` must be the eight rungs in the canonical order. | `middleware.ladder.rungs places summarize_repetitive before deduplicate — summarising records that dedupe would have removed pays for a lossy rung to do a lossless rung's work` |
| **V19** | `externalize` must remain in `middleware.ladder.always_available`. | `externalize removed from always_available — a conservative deployment would have no lossless way to fit an oversized payload` |
| **V20** | `middleware.relevance.protected_field_patterns` is a floor. Entries may be added, never removed. | `profiles/x.yaml removes "acl*" from protected_field_patterns — a relevance heuristic could then drop an authorization-bearing field` |
| **V21** | `models.routing.quality_floor_by_risk` must be non-decreasing with risk. | `quality_floor_by_risk: HIGH 0.85 is below MEDIUM 0.90 — a higher-risk task may never accept a lower quality bar` |
| **V22** | `delegation.depth_attenuation` must be in (0, 1.0]. | `depth_attenuation 1.4 — a delegation tree's cost diverges with depth instead of converging` |
| **V23** | `parallelism.reducer.conflict_precedence` must place `higher_trust` first. | `conflict_precedence begins with more_recent_valid_from — a newer untrusted claim would displace a trusted one` |
| **V24** | `middleware.rank.preserve_total_count` cannot be false. | `preserve_total_count disabled — a ranked result becomes indistinguishable from a complete one` |
| **V25** | `middleware.pagination.cursor_based` cannot be false. | `offset pagination over a live result silently skips or repeats records between calls` |
| **V26** | `fast_path` must set `max_prompt_tokens` alongside `max_utilization`. | `fast_path gates on utilisation alone — on a 1M window 35% is 350,000 tokens, so the fast path fires on turns that cost 6x a small one` |
| **V27** | `thresholds.absolute_enter_tokens` must be present and ascending. | `absolute_enter_tokens missing — fractional bands alone never trip on a large window until a turn is already expensive` |

### Validator

```text
function validate(merged, code, provider) -> void | ConfigError[]:

  errors <- schemaValidate(merged, optimizer.schema.json)     # structure first
  if errors: fail(errors)                                     # no point checking semantics

  # --- V1, V17: bands ---
  prev <- -1
  for b in merged.thresholds.bands where b.band != NORMAL:
      if b.enter <= b.exit:  err(V1, b)
      if b.enter <= prev:    err(V1, "bands must ascend", b)
      prev <- b.enter
  prune <- band(merged, PRUNE)
  if merged.fast_path.max_utilization >= prune.enter: err(V17)

  # --- V16: allocation ---
  b <- merged.budgets
  total <- b.reserved_output_fraction + b.reserved_system_skill_fraction
         + b.safety_margin_fraction + b.working_fraction
         + b.tool_results_fraction + b.memory_fraction
  if total > 1.0: err(V16, total)

  # --- V2, V14: monotone layers ---
  for layer in layersAfterDefaults():
      for path in SECURITY_PATHS:
          if loosens(layer[path], defaults[path]): err(V2, path, layer.name)
      for path in FLOOR_ARRAYS:                     # never_compress, propagate_through
          if not superset(layer[path], defaults[path]): err(V14, path, layer.name)

  # --- V3, V5, V7, V9: pinned values ---
  for (path, required) in PINNED:
      if merged[path] != required: err(pinnedInvariant(path), path)

  # --- V4: prefix order ---
  if merged.cache.prefix.stable_layer_order != CANONICAL_LAYER_ORDER: err(V4)

  # --- V6: delegation depth ---
  d <- merged.delegation
  if d.max_depth > d.sane_depth_limit and not d.explicit_deep_delegation: err(V6)

  # --- V8: semantic cache ---
  s <- merged.cache.semantic
  if s.enabled and (not covers(s.require_validation_when, [authorization_sensitive, time_sensitive])
                    or not s.forbid_when_scope_includes_user): err(V8)

  # --- V10: baseline honesty ---
  if merged.telemetry.baseline_method_default == MEASURED
     and not merged.gates.rollout.shadow_before_cutover: err(V10)

  # --- V11: do not reimplement what the provider does natively ---
  for cap in provider.native:
      flag <- middlewareFlagFor(cap)
      if merged.flags[flag] == "on" and not benchmarkOverrides(cap): err(V11, cap, flag)

  # --- V12: free controls ---
  if not superset(merged.fast_path.always_on, REQUIRED_FREE_CONTROLS): err(V12)

  # --- V18, V19, V20: the reduction ladder ---
  if merged.middleware.ladder.rungs != CANONICAL_LADDER: err(V18)
  if "externalize" not in merged.middleware.ladder.always_available: err(V19)
  for layer in layersAfterDefaults():
      if not superset(layer.middleware.relevance.protected_field_patterns,
                      defaults.middleware.relevance.protected_field_patterns): err(V20, layer.name)

  # --- V21, V22, V23: routing and delegation ---
  q <- merged.models.routing.quality_floor_by_risk
  if not (q.LOW <= q.MEDIUM <= q.HIGH): err(V21, q)
  if not (0 < merged.delegation.depth_attenuation <= 1.0): err(V22)
  cp <- merged.parallelism.reducer.conflict_precedence
  if "higher_trust" in cp and cp[0] != "higher_trust": err(V23, cp)

  # --- V24, V25: ranked and paginated results must describe themselves ---
  if not merged.middleware.rank.preserve_total_count: err(V24)
  if not merged.middleware.pagination.cursor_based:   err(V25)

  # --- V26, V27: absolute gates alongside fractional ones ---
  if merged.fast_path.max_prompt_tokens == null: err(V26)
  a <- merged.thresholds.absolute_enter_tokens
  if a == null or not (a.PRUNE < a.COMPACT < a.AGGRESSIVE < a.EMERGENCY): err(V27, a)

  # --- V13: memory schema ---
  if merged.meta.memory_schema_version != code.memory_schema_version
     and not migrationExists(...): err(V13)

  # --- V15: meta-bloat guard ---
  tokens <- tokenize(readAll(merged.skill.always_loaded), provider.tokenizer)
  if tokens > merged.skill.max_tokens: err(V15, tokens, merged.skill.max_tokens)
  for f in referenceFiles(merged.skill.references_dir):
      if tokenize(f) > merged.skill.reference_max_tokens: err(V15, f)

  if errors: fail(errors)     # all of them, not just the first
```

The validator reports **every** violation, not the first. A config with four problems should take one
fix cycle, not four.

---

## 7. Runtime reconfiguration

| Change | When it takes effect | Why |
|---|---|---|
| Flags (`on` / `off` / `shadow`) | Next turn | The rollback path; must be immediate |
| Budgets, thresholds, per-tool limits | Next turn | Read fresh from the plan each turn |
| Retry, guard, breaker limits | Next turn | Per-turn `GuardSpec` |
| Compression aggressiveness | Next task boundary | Mid-task changes make retention decisions inconsistent within one task |
| Prefix layer order, skill content | Restart | Invalidates every cached prefix; batching this into a deploy is the point |
| Model bindings, tiers | Restart | Forces re-probe and budget recomputation |
| Memory schema, provenance fields | Restart with migration | Persisted format |
| Security policies | Restart | Auditable change, never a hot toggle |

Anything that edits the cached prefix is restart-only by design. A hot change to the skill text would
invalidate every session's cache at an arbitrary moment, which is exactly the kind of invisible cost
event the ledger would then have to explain.

---

## 8. Profiles

Three shipped examples. They exist to make the point that **verdicts are per-deployment**: the same
mechanism is core for one workload and disabled for another.

| | Conversational | Database analytics | Coding agent |
|---|---|---|---|
| `fast_path.max_utilization` | 0.45 | 0.25 | 0.30 |
| `overhead.max_fraction` | 0.06 | 0.12 | 0.08 |
| `overhead.max_model_advisor_calls` | 0 | 1 | 1 |
| `tool_results_fraction` | 0.12 | 0.34 | 0.30 |
| `memory_fraction` | 0.18 | 0.08 | 0.08 |
| `COMPACT` band enters at | 0.78 | 0.62 | 0.68 |
| `compression.aggressiveness` | conservative | balanced | conservative |
| `expected_remaining_turns` | 15 | 4 | 10 |
| `externalize_when_tokens_above` | 1500 | 800 | 1200 |
| Code-execution bypass | **off** | **on** | shadow |
| Progressive tool disclosure | **off** | on | on |
| Parallel branches | off | **on** (6) | on (4) |
| Sub-agent delegation | off | shadow | shadow |
| Semantic cache | off | off | off |

Four of these differences carry the reasoning worth stating:

* **Disclosure is off for the conversational profile.** Few tools, all stable, all cheap to keep
  loaded. Churning them to save a few hundred schema tokens would invalidate a prefix worth far more
  — the router would be a net cost. On the analytics and coding profiles, where schemas are large and
  the working set genuinely shifts between tasks, it pays.
* **The bypass is the analytics profile's whole reason for existing**, and is off entirely for the
  conversational one, which has no data-heavy work to bypass.
* **The coding profile is the most conservative on compression** despite having the largest payloads,
  because a summarized function signature that drops a parameter produces code that does not compile
  and the agent cannot tell that from context. Its savings come from externalization and retrieval,
  not compression. It extends `never_compress` with file paths, type definitions, test expectations,
  stack traces and diffs.
* **The semantic cache is off everywhere.** Analytics answers are tenant- and time-scoped; repository
  state changes underneath a coding agent's cache; conversational answers are personalized. It stays
  in the file as a supported, disabled mechanism rather than being removed, because a deployment with
  a genuinely shared, static corpus should be able to turn it on — under V8.

---

## 9. Worked rejections

**A plausible-looking threshold change that thrashes.**

```yaml
thresholds:
  bands:
    - { band: COMPACT, enter: 0.70, exit: 0.70 }
```
```text
FATAL config: thresholds.bands[COMPACT]: enter 0.70 must exceed exit 0.70 (V1)
  Equal enter and exit produce oscillation: compaction drops utilization below the
  line, the next tool result pushes it back over, and the same content is summarized
  repeatedly while the prefix is invalidated each time.
```

**A tenant trying to help.**

```yaml
security:
  sanitization:
    before_external_write: false     # "our archive is internal"
```
```text
FATAL config: security.sanitization.before_external_write cannot be disabled (V3)
  Externalization writes conversation and tool data to durable stores. An internal
  store is still a store, and erasure requests must be satisfiable against it.
```

**A profile quietly buying itself permission.**

```yaml
context:
  disposition:
    never_compress: [user_requirements, constraints, decisions]
```
```text
FATAL config: context.disposition.never_compress removes 10 entries from the shipped
  floor: identifiers, names, dates, numeric_values, configuration, code, api_contracts,
  unresolved_issues, commitments, state_transitions (V14)
  This list is append-only. Add entries; do not remove them.
```

**The one that catches the optimizer optimizing nothing.**

```text
FATAL config: skill/SKILL.md measures 1,140 tokens against skill.max_tokens 800 (V15)
  The always-loaded instruction set is paid on every request of every session. An
  optimizer that ships with a verbose skill prompt recreates the bloat it exists
  to remove. Move detail into skill/references/ and load it on its trigger.
```

---

## 10. Tuning: symptom to knob

The practical companion to the reference above. Every row assumes the symptom is confirmed in
telemetry, not suspected.

| Symptom | Look at | Likely change |
|---|---|---|
| Optimizer overhead is a visible share of spend | `telemetry` overhead per turn, `fast_path_blocked_by` | Widen `fast_path`, lower `overhead.max_fraction`, set `max_model_advisor_calls: 0` |
| Cache hit rate collapsed mid-session | `cache_invalidation_events` | Set `compaction.tail_first`, raise `COMPACT` enter, check for a hot config change to the prefix |
| Compaction runs constantly | Compaction count per session | Widen the hysteresis gap; raise `externalize_when_tokens_above` |
| The agent keeps re-fetching dropped data | `MANIFEST_RETRIEVAL` rate | Raise the offending `per_tool` budget; lower `compression.aggressiveness` |
| Task success dropped after enabling an optimization | Per-workload gate report | Set that flag to `shadow`; the per-optimization gate is 1% |
| `EMERGENCY` band firing regularly | Emergency rate alarm | The allocation is wrong, not the compaction policy — revisit `budgets` fractions |
| Sub-agents cost more than they save | `subagent_roi` | Raise `min_expected_saving_usd`, or set `subagent_delegation: off` |
| Savings reported but the invoice disagrees | `baseline_method` on the claims | Claims were `ESTIMATED`; run a measured A/B before believing them |
| Decisions drifting without visible errors | Estimator bias report | This is the silent failure — check C23 liveness first, then recalibrate |

---

## 11. Extensions by later stages

This file is versioned and grows as implementation reveals values that would otherwise be literals.
Each extension is listed here so the config's history is legible rather than archaeological.

| Stage | Added | Why |
|---|---|---|
| 3 | `middleware.*` | The reduction ladder's per-rung policies — null semantics, protected fields, dedupe identity, truncation boundary, representation gates (D3) |
| 3 | `estimation.*` | Token counting is itself a cost; exact below a threshold, sampled above it, always with a byte bound (D3 §11) |
| 3 | `context_manager.*` | Scoring priors, summary-validation rules, checkpoint limits (D4) |
| 3 | `budgets.output_headroom`, `budgets.min_reserved_output` | Reserved output sized from the operation class rather than a flat fraction (D4 §2) |
| 3 | V18–V20 | Ladder order, externalization availability, and the protected-field floor |
| 4 | `tools.router.{max_active_tools, stability_window_turns, incumbency_bonus, signature_*, rerank_*}` | Cache-stability policy for the tool projection (D5 §7) |
| 4 | `tools.necessity.value_of_correct_decision_usd` | The value-of-information test needs a value for a correct decision (D5 §9) |
| 4 | `models.routing.{quality_floor_by_risk, switch_margin, switching_cost_aware, tier_quality_prior, min_quality_samples}` | Quality as a hard filter, and the cost of abandoning a warm prefix (D6 §1, §3) |
| 4 | `models.reasoning.thinking_tokens` | Thinking budgets per tier — billed as output, tracked separately (D6 §4) |
| 4 | `delegation.{spawn_budget_usd_*, depth_attenuation, max_context_transfer_tokens, inline_threshold_tokens, result_max_tokens, strict_inputs}` | Cost bounds, not just count bounds; transfer discipline (D7 §2, §3) |
| 4 | `parallelism.reducer.{conflict_precedence, corroboration}` | Explicit precedence, and agreement treated as evidence (D7 §6) |
| 4 | V21–V23 | Quality-floor monotonicity, attenuation convergence, trust-first precedence |
| 5 | `middleware.rank.*`, `middleware.pagination.*` | Ladder rungs 4 and 7 given bodies; ranked and paginated results must carry their total and a stable cursor (D3 §7.1, §9.1) |
| 5 | V24–V25 | Total-count preservation and cursor-based pagination |
| 5 | `fast_path.max_prompt_tokens`, `thresholds.absolute_enter_tokens`, `skill.measured_session_floor_tokens` | **Measured recalibration.** A fraction of a 1M window is a useless budget signal: at 35% the fast path fires on a 350,000-token turn. Cost tracks absolute tokens ([`phase0-findings.md`](phase0-findings.md) §4b) |
| 5 | V26–V27 | Absolute gates required alongside fractional ones |

The rule this follows: **a value discovered during implementation becomes a config key, not a
constant.** The alternative — a literal in the middleware with a comment explaining it — is how a
system ends up with tuning knobs nobody can find.

---

## 12. What is still left to later stages

* **Actual price tables.** `models.bindings` ships empty by design — no provider assumption is
  hard-coded. Every cost figure in the docs is illustrative; the cost model itself is in
  [`economics.md`](economics.md) (D8), and the constants come from your own price list.
* **Advisor cost declarations.** The scheduler's `cost_est` per advisor is a property of the
  implementations ([`tool-router.md`](tool-router.md), [`model-router.md`](model-router.md)), refined
  at runtime from the ledger's realized-savings EWMA.
* **Benchmark records for V11.** The native-capability override reads from the benchmark store the
  gate runner populates ([`test-strategy.md`](test-strategy.md) §6). Until a benchmark exists for a
  capability the provider declares native, the middleware equivalent stays `off` or `shadow`.
* **Which flags should actually be on.** The shipped `flags` defaults now reflect the verdicts in
  [`mechanism-inventory.md`](mechanism-inventory.md) and the phasing in [`roadmap.md`](roadmap.md) —
  and those verdicts are per-deployment. A profile that differs from the reference deployment
  (analytics-dominant, cold sessions, media workloads) should expect several to flip.
