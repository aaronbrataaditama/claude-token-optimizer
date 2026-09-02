# Install

Two independent halves. The **measurement tools** need nothing but Python and tell you whether any of
this is worth doing. The **skill** changes how the agent behaves. Install the tools first — the whole
argument of this project is that you should measure before you change anything.

---

## 1. Measurement tools (start here)

Python 3.8+, no dependencies.

```bash
git clone https://github.com/aaronbrataaditama/claude-token-optimizer.git
cd claude-token-optimizer

python tools/baseline_profile.py          # where your tokens and money go
python tools/context_growth.py            # what is filling your context
python tools/ttl_analysis.py              # is your cache TTL earning its premium
```

They read `~/.claude/projects/**/*.jsonl` — your Claude Code transcripts.

**They are content-free by construction.** They read `message.usage`, `message.model`, `timestamp`,
`sessionId`, `type`, block types and tool names. They never open message content, tool results or
attachments, and nothing they print contains conversation text. Everything runs locally; nothing is
uploaded.

Useful flags:

```bash
python tools/baseline_profile.py --project MyApp     # one project
python tools/baseline_profile.py --json out.json     # machine-readable
python tools/context_growth.py --include-subagents   # count delegated work too
```

`out.json` will contain your project names and spend. Keep it out of version control — the shipped
`.gitignore` already excludes `baseline.json`.

### Reading the result

`baseline_profile.py` ends with a verdict:

| Verdict | Meaning |
|---|---|
| **Scenario A** — bloated on both axes | Low cache hit rate and high thinking share. Large wins available. |
| **Scenario B** — already lean | Caching and reasoning are in good shape. Expect 10–20%, concentrated in context volume. |

On a mature harness like Claude Code you will probably land in Scenario B, and the lever will be
**session length** rather than anything clever. See `docs/phase0-findings.md`.

---

## 2. The payload reducer

A standalone pipe. No installation, no configuration.

```bash
# bound a large command output
find . -name '*.log' -exec cat {} + | python tools/reduce.py --budget 4000 --query "timeout errors"

# reduce a JSON payload
python tools/reduce.py --file big-response.json --budget 2000 --query "sla breached"
```

It removes empty fields, drops irrelevant ones, deduplicates while preserving counts, ranks records
while preserving the total, truncates long values at semantic boundaries, picks the smallest
representation, and prints a manifest saying what it dropped and how to recover it. The full original
is archived first, so every manifest entry names a recovery that exists.

It **never summarises** — that is the only reduction that can fabricate, and it needs a model.

```
--budget N          target tokens (default 4000)
--query "..."       what you need; drives field and record relevance
--file PATH         read a file instead of stdin
--archive-dir DIR   where the original goes (default: system temp)
--no-archive        skip archiving
```

---

## 3. The skill (changes agent behaviour)

```bash
mkdir -p ~/.claude/skills
cp -r skill/. ~/.claude/skills/token-optimization/
rm ~/.claude/skills/token-optimization/CLAUDE.md \
   ~/.claude/skills/token-optimization/README.md
```

Loads on demand when a situation matches its description. Remove with
`rm -rf ~/.claude/skills/token-optimization`.

On Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force ~\.claude\skills\token-optimization\references
Copy-Item skill\SKILL.md ~\.claude\skills\token-optimization\
Copy-Item skill\references\*.md ~\.claude\skills\token-optimization\references\
```

---

## 4. The always-on rules (optional, recommended)

Skills load **on demand**. The three correctness rules — untrusted content stays data, never summarise
code or identifiers, resolve ties toward correctness — need to be present on turns where the skill
does *not* fire, which is exactly when they matter.

```bash
cp skill/CLAUDE.md ~/.claude/CLAUDE.md          # if you have none
cat skill/CLAUDE.md >> ~/.claude/CLAUDE.md      # if you do
```

**This applies to every project you work on.** Read it before installing — it is 281 tokens and takes
a minute. It costs ~281 tokens on every turn of every session (roughly 0.1% of a measured bill) and
buys those rules being unconditionally present.

If you install it, the skill and the CLAUDE.md are coupled: the skill assumes the CLAUDE.md carries
the always-on layer and does not repeat it. Removing one leaves a hole in the other.

---

## 5. Configuration

`config/` is a **design artifact, not a runtime dependency.** Nothing in this repository reads it.

It is the full parameter surface the specification calls for — thresholds, budgets, model tiers, cache
policy, security policy — with a JSON Schema and twenty-seven load-time validation invariants
documented in `docs/configuration.md`. It is there for anyone implementing the middleware described in
`docs/`, and as a worked example of what "nothing hard-coded" costs to actually deliver.

If you are only using the tools and the skill, ignore it.

---

## Uninstall

```bash
rm -rf ~/.claude/skills/token-optimization
# and remove the block you appended to ~/.claude/CLAUDE.md
```

Nothing else touches your system. The tools are read-only; the reducer writes only to its archive
directory.
