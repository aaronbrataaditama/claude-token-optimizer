# Phase 0 — measured baseline

**Real data, not the reference deployment.** Produced by
[`tools/baseline_profile.py`](../tools/baseline_profile.py) over a multi-project Claude Code corpus
spanning tens of thousands of model turns.

This document exists because the measurement contradicted the design. `economics.md` assumed a
reference deployment; this is what one actual deployment looks like, and three of the design's
load-bearing assumptions are wrong for it.

---

## 1. The profile

```text
prompt token shares
  cache read      (0.1x)                      95.4%
  cache write 5m  (1.25x)                      2.9%
  cache write 1h  (2.0x)                       1.7%
  uncached input  (1.0x)                       0.1%

thinking as a share of output                18.1%

cost share (Opus-tier $5/$25 per MTok)
  cache reads                                 54.1%
  cache writes 5m                             20.2%
  cache writes 1h                             18.8%
  output incl. thinking                       13.0%
  uncached input                               0.3%
                                             ──────
  TOTAL                                      100.0%

prompt size per turn         p50 114,574   p90 315,999   max 996,642
model mix                    opus-5 71% · sonnet-5 15% · haiku-4.5 6% · opus-4.8 4%
```

**Absolute spend, corpus size and aggregate token volumes have been removed** from this write-up.
What remains is every share, ratio, per-turn and per-call figure — which is where the transferable
content is, and which does not reconstruct the totals. Cost shares were
computed from list API prices applied to measured token counts — under a subscription those are
notional rather than an invoice, but the shares hold either way, and the shares are what drive
every decision below.

---

## 2. Three assumptions the design got wrong

### 2.1 The cache hit rate is 95.4%, not 35%

`economics.md` §4 attributes **60% of all modelled savings** to cache-aware prefix ordering and
append-only history, on the assumption of a 35% accidental baseline hit rate. The measured rate is
**95.4%**.

The harness already does this. There is essentially nothing left to win on the mechanism the design
identified as its single largest lever, and the roadmap put it first in Phase 1.

This is Scenario B from `economics.md` §5, and more extreme than the version written there.

### 2.2 Cache *writes* are 39% of cost — the design barely modelled them

`CacheWriteCost` appears in the cost model as a term, and then only as the tax on switching model or
churning a prefix. Measured, it is **39% of cost** — nearly as large as reads, and the
second-biggest line by a wide margin.

Both TTLs are in use, split roughly 63/37 by volume between the 5-minute rate (1.25×) and the
1-hour rate (2.0×).
Writes are what you pay every time new content enters a cached prefix — which in an agent loop is
every turn. The design treated this as an edge case and it is a structural cost.

### 2.3 Sub-agents are not cold, so the delegation deferral was wrong

`mechanism-inventory.md` **deferred** the entire delegation cluster (mechanisms 64–66), and
`anti-patterns.md` §7 built a case against it, both resting on the cold-prefix penalty: a sub-agent
starts with no cache and pays full price for context the primary gets at a discount.

Measured:

```text
                turn share   cost share   hit rate   tokens/turn
main sessions        38.3%        58.0%      97.1%       235,813
sub-agents           61.7%        42.0%      93.3%       111,503
```

**Sub-agents run at a 93.3% hit rate.** They are not cold. They also receive less than half the
context per turn (111k vs 236k), which is exactly the "minimal context" property D7 §3 was designed
to enforce — the harness already provides it.

The premise was wrong for this harness, so the verdict derived from it is wrong. Delegation is
already 42% of spend and running efficiently.

---

## 3. Where the money actually is

```text
cache reads + cache writes           93.1% of cost
output (incl. thinking)              13.0%
uncached input                        0.3%
```

Reads and writes both scale with **one thing**: how many tokens are in the context. Not how well
they are cached — that is already near-optimal — but how many there are at all.

```text
p50 prompt      114,574 tokens
p90 prompt      315,999 tokens
max prompt      996,642 tokens   (the full window)
```

Even at 95% cached and 0.1× pricing, cache reads remain the single largest cost line at 54%.
**Halving the median context roughly halves the bill.**

Thinking, by contrast, is 18.1% of output but output is only 13% of cost — so reasoning-effort
routing is worth at most ~2.4% of total spend here, against the 21% the model attributed to it.

---

## 4. What this means for the roadmap

The plan in `roadmap.md` was ordered by the reference deployment's attribution. Against this data
the ordering inverts:

| Phase 1 mechanism | Modelled | Measured here |
|---|---|---|
| Stable prefix ordering, append-only | 60% of savings | **~0%** — already at 95.4% |
| Reasoning-effort routing | 21% | **~2.4%** of total cost |
| Output controls | 1.7% | ~1% |
| **Phase 2 — externalization, selective retention** | 5–15% | **the whole game** |

**Phase 2 is where this deployment's savings are, and Phase 1 is largely already done for it.**
Specifically:

* **Externalization + selective retrieval** (mechanism 36) and **ladder rungs 7–8** (mechanism 30)
  attack context volume directly, which is 93% of cost.
* **Per-tool result budgets** (25) and **ladder rungs 1–4** (27) are the Phase 1 items that still
  apply, because they bound what enters context in the first place.
* **Delegation** (64–66) should move from `DEFER` to a real candidate, since its blocking assumption
  is empirically false here.
* **Cache-write economics** need a mechanism the design does not have: writes are 39% of cost and
  nothing in the inventory targets them.

---

## 4b. What is actually filling the 114k — measured

`tools/context_growth.py` recovers the intra-input split from the transcript records themselves:
it measures the size of every content block and attributes it, without reading any content.

### The growth curve is the whole answer

```text
turn    1     median prompt    41,549
turn   10     median prompt    53,529        ~1,000 tok/turn
turn   40     median prompt    96,933
turn   75     median prompt   138,025
turn  150     median prompt   207,837
turn  300     median prompt   334,215
turn  600     median prompt   325,924        plateau - harness compaction
turn 1200     median prompt   142,692        compaction has cut in hard
```

Two facts fall out, and they dominate everything else in this document.

**A fresh session starts at ~41,500 tokens.** That is the floor before any work happens: system
prompt, tool schemas, skill listings, agent listings, memory, environment. It is 36% of the median
prompt and it is paid on every turn of every session.

**After that, context grows ~1,000–1,200 tokens per turn, linearly, until compaction intervenes
around 330k.**

### Where the spend sits in a session

```text
turn range     turn share   prompt-token share   avg prompt
1-10                 3.1%                 0.6%       46,601
11-25                3.8%                 1.0%       61,622
26-50                5.3%                 1.9%       84,303
51-100               8.7%                 4.5%      122,735
101-200             12.9%                10.2%      187,437
201-400             16.5%                19.4%      278,568
401+                49.6%                62.3%      297,680
```

**Turns past #100 are 79% of turns but 92% of prompt tokens. Turns past #400 alone are 62%.**

A turn late in a long session costs **6.4× more** than an early one — 297,680 tokens versus 46,601 —
for identical work, purely from accumulated prefix.

### What enters per turn

```text
tool results                     251 tok/turn   28.9%
tool call parameters             173            20.0%
assistant thinking               168            19.3%
attachments / system reminders   110            12.7%
other blocks                      97            11.1%
user messages                     39             4.6%
assistant text                    29             3.4%
```

Among tool results, one tool dominates:

```text
tool      share of tool-result volume   call share   median   p95
Read                            55.0%        13.1%      708   10,205
Bash                            30.9%        57.4%      155    1,163
Edit                             2.9%        24.0%       69       90
Grep                             2.3%         3.2%      227    1,884
```

`Read` produces 55% of all tool-result volume from 13% of the calls. Its median is modest at 708
tokens; its
**p95 is 10,205**. It is the tail that fills context, not the typical call.

Harness-injected attachments are 12.7%, and are mostly outside application control — though three of
the largest scale with what is installed:

```text
                      share of attachment volume   tokens per injection
edited_text_file                           21.3%                  1,411
skill_listing                              13.9%                  1,789
deferred_tools_delta                       12.4%                  1,704
agent_listing_delta                        10.8%                  1,990
nested_memory                               9.1%                  2,353
```

---

## 4c. The lever, ranked

**1. Session length. Free, no code, and larger than everything else combined.**

Capping sessions at ~100 turns instead of running to 400+ would take the average prompt from 297,680
to roughly 91,500 (a 41,500 floor plus ~1,000/turn over 100 turns):

```text
current       avg prompt 237,000 tokens per turn
100-turn cap  avg prompt  91,500 tokens per turn   (41,500 floor + ~1,000/turn over 100)
                                                    ──────────────────────────────────
                                                    ~61% fewer prompt tokens
```

Since 93% of cost scales with prompt volume, that is roughly a **55–60% cost reduction on main
sessions, available today, by starting fresh more often.**

The honest cost: you lose context and pay to re-establish it — re-reading files, restating intent.
That is exactly what mechanism 47 (session resumption bundle) exists to make cheap, and it is the
single mechanism in the inventory with the clearest case on this data.

**2. `Read` discipline.** 55% of tool-result volume, p95 of 10,205 tokens. Targeted reads with
offsets, or `Grep` to locate before `Read` to retrieve, attack the tail directly. This is what
`skill/references/payload.md` already says; it is now known to be the right target.

**3. Shrink the 41,500-token floor.** Fewer installed skills, agents and MCP servers; a smaller
`CLAUDE.md`. Every token here is multiplied by every turn of every session. Worth noting: installing
the `token-optimization` skill added to this floor and to every `skill_listing` re-injection — the
optimizer pays its own meta-bloat tax, which is precisely what invariant V15 was written to bound.

**4. Everything else in Phase 2′.** Externalization, dispositions, budget managers. Real, but
strictly smaller than item 1 and requiring code that cannot be written from outside the harness.

---

## 4d. The TTL question, answered — and my prediction was wrong

§4c ranked "cache-write TTL selection" as worth up to 7.1% and gated it: *a measurement to take
before it is a change to make.* `tools/ttl_analysis.py` took it.

```text
gap between consecutive turns      p50 5s   p90 46s   p99 1,829s
  under 1 min                                        91.5%
  1-5 min                                             5.3%
  5-60 min   <- the only window 1h pays               2.5%
  over 1 hour                                         0.7%

relative to the 1-hour write bill (= 100):
  all writes at 5m instead of 1h                       62.5
  + rebuilding the prefixes a 5m entry would lose      53.4
                                                     ------
  5m total                                            115.9

the 1-hour TTL is EARNING its premium: 5m would cost ~16% more
```

**The predicted 7.1% saving is actually a ~16% increase in write cost.** Wrong by its whole
magnitude, and by its sign.

The reason is the same one that governs everything else here: **contexts are enormous.** Only 2.5% of
gaps land in the 5–60 minute window where the longer TTL helps — but the turns *following* those gaps
carry an average prefix of ~235,000 tokens. Rebuilding those exceeds what is saved by dropping
every write from 2.0× to 1.25×.

At a 235k median prefix, surviving even a rare gap is worth paying double on every write. A deployment
with small contexts would get the opposite answer, which is exactly why this had to be measured rather
than reasoned about.

Verdict on mechanism 17b: **OMIT**. *Revisit if:* the median prefix falls below ~60,000 tokens.

**This is the third of my own claims that measurement has overturned**, after the 60%-of-savings cache
mechanism and the delegation deferral. The pattern is consistent enough to be worth naming: every
error came from reasoning about a platform's behaviour instead of observing it, and every one was
caught because the claim was written down in a checkable form before being acted on.

---

## 5. What the measurement cannot see

* **The intra-input split.** Claude Code reports prompt tokens only as cached / uncached / written.
  System vs skill vs tool schemas vs history vs tool results is not recoverable from the transcript,
  and that split is what would say *which* content to externalize. It needs instrumentation at the
  request layer.
* **Counterfactuals.** Sub-agent efficiency is measured; sub-agent *ROI* is not. Whether those
  delegated turns were cheaper than doing the work natively needs an A/B, not a transcript.
* **Session boundaries.** Sub-agent transcripts inherit the parent `sessionId`, so the
  turns-per-session figures conflate a session with its delegated work. Treat them as an upper bound.
* **Whether any of this is billed.** See the caveat in §1.

---

## 6. Reproducing

```bash
python tools/baseline_profile.py                    # all projects
python tools/baseline_profile.py --project MyApp    # one project
python tools/baseline_profile.py --json out.json    # machine-readable
```

The profiler is content-free by construction: it reads `message.usage`, `message.model`,
`timestamp`, `sessionId` and `type`, and never touches message content, tool results or attachments.
Nothing it prints contains conversation text.
