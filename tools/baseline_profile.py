"""
Phase 0 - baseline token profiler for Claude Code transcripts.

Reads ~/.claude/projects/*/*.jsonl and produces the per-category token profile
that Deliverable 12 Phase 0 requires, plus the Scenario A / Scenario B verdict
from docs/economics.md section 5.

CONTENT-FREE BY CONSTRUCTION. This script reads exactly five fields per record:
    message.usage, message.model, timestamp, sessionId, type
It never touches message.content, toolUseResult, or attachment bodies. Nothing
it prints contains conversation text.

Usage:
    python baseline_profile.py                 # all projects
    python baseline_profile.py --project NAME  # one project (substring match)
    python baseline_profile.py --json out.json # also write machine-readable output
"""

import argparse, collections, glob, io, json, os, sys

# Prices in USD per million tokens, verified against the claude-api skill.
# cache_read = 0.1x input; cache write = 2.0x at 1h TTL, 1.25x at 5m TTL.
PRICES = {
    "claude-opus-5":     {"in": 5.00, "out": 25.00},
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00},
    "claude-opus-4-7":   {"in": 5.00, "out": 25.00},
    "claude-opus-4-6":   {"in": 5.00, "out": 25.00},
    "claude-fable-5":    {"in": 10.00, "out": 50.00},
    "claude-sonnet-5":   {"in": 2.00, "out": 10.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00},
}
CACHE_READ_MULT = 0.10
WRITE_MULT = {"1h": 2.00, "5m": 1.25}
UNKNOWN = {"in": 5.00, "out": 25.00}   # assume Opus-tier if unrecognised


def price(model):
    if not model:
        return UNKNOWN
    for k, v in PRICES.items():
        if model.startswith(k):
            return v
    return UNKNOWN


class Acc(object):
    def __init__(self):
        self.input = self.read = self.write_1h = self.write_5m = 0
        self.out = self.think = self.turns = 0
        self.cost = 0.0
        self.sessions = collections.Counter()
        self.models = collections.Counter()
        self.prompt_sizes = []

    def add(self, u, model, session):
        p = price(model)
        cc = u.get("cache_creation") or {}
        w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
        w5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        flat = u.get("cache_creation_input_tokens", 0) or 0
        if w1 + w5 == 0 and flat:
            w1 = flat          # TTL split absent; assume the 1h default seen in Claude Code

        i = u.get("input_tokens", 0) or 0
        r = u.get("cache_read_input_tokens", 0) or 0
        o = u.get("output_tokens", 0) or 0
        t = (u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0

        self.input += i
        self.read += r
        self.write_1h += w1
        self.write_5m += w5
        self.out += o
        self.think += t
        self.turns += 1
        self.sessions[session] += 1
        self.models[model] += 1
        self.prompt_sizes.append(i + r + w1 + w5)

        self.cost += (i * p["in"]
                      + r * p["in"] * CACHE_READ_MULT
                      + w1 * p["in"] * WRITE_MULT["1h"]
                      + w5 * p["in"] * WRITE_MULT["5m"]
                      + o * p["out"]) / 1000000.0

    @property
    def prompt_total(self):
        return self.input + self.read + self.write_1h + self.write_5m

    @property
    def hit_rate(self):
        return self.read / float(self.prompt_total) if self.prompt_total else 0.0

    @property
    def think_share(self):
        return self.think / float(self.out) if self.out else 0.0


def scan(root, project_filter=None):
    """Walks recursively. Transcripts under a <session-id>/subagents/ path are
    delegated work and are accounted separately - that split is the whole point
    of measuring delegation ROI (D7 section 9)."""
    overall, by_project = Acc(), {}
    main_acc, sub_acc = Acc(), Acc()
    files = 0
    for proj_dir in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(proj_dir):
            continue
        name = os.path.basename(proj_dir)
        if project_filter and project_filter.lower() not in name.lower():
            continue
        acc = by_project.setdefault(name, Acc())
        for dirpath, _dirnames, filenames in os.walk(proj_dir):
            is_sub = "subagents" in dirpath.replace("\\", "/").split("/")
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                f = os.path.join(dirpath, fn)
                files += 1
                try:
                    fh = io.open(f, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with fh:
                    for line in fh:
                        if '"usage"' not in line:      # cheap prefilter
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if d.get("type") != "assistant":
                            continue
                        m = d.get("message")
                        if not isinstance(m, dict):
                            continue
                        u = m.get("usage")
                        if not isinstance(u, dict):
                            continue
                        sid = d.get("sessionId") or os.path.basename(f)
                        model = m.get("model")
                        acc.add(u, model, sid)
                        overall.add(u, model, sid)
                        (sub_acc if is_sub else main_acc).add(u, model, sid)
    return overall, by_project, files, main_acc, sub_acc


def pct(x):
    return "%5.1f%%" % (100 * x)


def report(overall, by_project, files, main_acc=None, sub_acc=None):
    W = 78
    print("=" * W)
    print("PHASE 0 - BASELINE TOKEN PROFILE")
    print("=" * W)
    print("transcripts scanned : %d files, %d projects" % (files, len(by_project)))
    print("model turns         : {:,}".format(overall.turns))
    print("sessions            : {:,}".format(len(overall.sessions)))
    if not overall.turns:
        print("\nNo usage records found.")
        return

    tp = overall.prompt_total
    print("")
    print("-" * W)
    print("TOKEN PROFILE  (the category split Phase 0 requires)")
    print("-" * W)
    rows = [
        ("cache read   (billed 0.1x)", overall.read,     overall.read / float(tp)),
        ("cache write  1h (2.0x)",     overall.write_1h, overall.write_1h / float(tp)),
        ("cache write  5m (1.25x)",    overall.write_5m, overall.write_5m / float(tp)),
        ("uncached input (1.0x)",      overall.input,    overall.input / float(tp)),
    ]
    for label, v, share in rows:
        print("  %-28s %15s  %s" % (label, "{:,}".format(v), pct(share)))
    print("  %-28s %15s" % ("prompt tokens total", "{:,}".format(tp)))
    print("")
    print("  %-28s %15s" % ("output tokens", "{:,}".format(overall.out)))
    print("  %-28s %15s  %s of output"
          % ("  of which thinking", "{:,}".format(overall.think), pct(overall.think_share)))

    print("")
    print("-" * W)
    print("COST  (Opus-tier $5/$25 per MTok; read 0.1x, 1h write 2.0x)")
    print("-" * W)
    c_read = overall.read * 5.00 * CACHE_READ_MULT / 1e6
    c_w1 = overall.write_1h * 5.00 * WRITE_MULT["1h"] / 1e6
    c_w5 = overall.write_5m * 5.00 * WRITE_MULT["5m"] / 1e6
    c_in = overall.input * 5.00 / 1e6
    c_out = overall.out * 25.00 / 1e6
    tot = overall.cost
    for label, v in [("cache reads", c_read), ("cache writes 1h", c_w1),
                     ("cache writes 5m", c_w5), ("uncached input", c_in),
                     ("output (incl. thinking)", c_out)]:
        print("  %-28s $%10.2f  %s" % (label, v, pct(v / tot if tot else 0)))
    print("  %-28s $%10.2f" % ("TOTAL", tot))
    print("  %-28s $%10.4f" % ("per turn", tot / overall.turns))

    print("")
    print("-" * W)
    print("SESSION SHAPE")
    print("-" * W)
    lens = sorted(overall.sessions.values())
    print("  turns per session   p50 %d   p90 %d   max %d"
          % (lens[len(lens) // 2], lens[int(len(lens) * 0.9)], lens[-1]))
    ps = sorted(overall.prompt_sizes)
    print("  prompt size (tok)   p50 {:,}   p90 {:,}   max {:,}".format(
        ps[len(ps) // 2], ps[int(len(ps) * 0.9)], ps[-1]))
    print("  models              %s" % ", ".join(
        "%s x%d" % (m or "?", c) for m, c in overall.models.most_common(4)))

    print("")
    print("-" * W)
    print("VERDICT")
    print("-" * W)
    hr, ts = overall.hit_rate, overall.think_share
    print("  cache hit rate      %s" % pct(hr))
    print("  thinking share      %s of output tokens" % pct(ts))
    print("")
    if hr >= 0.70 and ts <= 0.20:
        v = "SCENARIO B - already lean"
        note = ("Caching and reasoning are both in good shape. Expect 10-20% headroom,\n"
                "  concentrated in payload reduction.")
    elif hr >= 0.70:
        v = "SCENARIO B(ish) - caching lean, reasoning is the lever"
        note = "Cache is working. The remaining lever is reasoning-effort routing."
    elif ts <= 0.20:
        v = "SCENARIO A(ish) - reasoning lean, caching is the lever"
        note = "Reasoning is controlled. Prefix stability is where the money is."
    else:
        v = "SCENARIO A - bloated on both axes"
        note = "Both major levers are available. Phase 1 should deliver 30-50%."
    print("  %s" % v)
    print("  %s" % note)

    if main_acc is not None and sub_acc is not None and sub_acc.turns:
        print("")
        print("-" * W)
        print("DELEGATION  (main sessions vs sub-agent transcripts)")
        print("-" * W)
        tot2 = main_acc.cost + sub_acc.cost
        print("  %-20s %9s %11s %8s %8s %9s"
              % ("", "turns", "cost", "share", "hit", "tok/turn"))
        for label, a in [("main sessions", main_acc), ("sub-agents", sub_acc)]:
            print("  %-20s %9s $%10.2f %7s %7s %9s"
                  % (label, "{:,}".format(a.turns), a.cost,
                     pct(a.cost / tot2 if tot2 else 0), pct(a.hit_rate),
                     "{:,}".format(a.prompt_total // a.turns)))
        print("")
        print("  Sub-agents run %s of turns for %s of spend."
              % (pct(sub_acc.turns / float(main_acc.turns + sub_acc.turns)),
                 pct(sub_acc.cost / tot2 if tot2 else 0)))

    print("")
    print("-" * W)
    print("BY PROJECT  (top 10 by cost)")
    print("-" * W)
    print("  %-42s %9s %7s %7s %8s" % ("project", "cost", "hit", "think", "turns"))
    ranked = sorted(by_project.items(), key=lambda kv: -kv[1].cost)[:10]
    for name, a in ranked:
        if not a.turns:
            continue
        print("  %-42s $%8.2f %6s %6s %8s"
              % (name[:42], a.cost, pct(a.hit_rate), pct(a.think_share),
                 "{:,}".format(a.turns)))

    print("")
    print("-" * W)
    print("WHAT THIS CANNOT SEE")
    print("-" * W)
    print("  The intra-input split - system vs skill vs tool schemas vs history vs")
    print("  tool results - is not in the transcript. Claude Code reports prompt")
    print("  tokens only as cached / uncached / written. Measuring that split needs")
    print("  instrumentation at the request layer, which is Phase 1 work.")
    print("")
    print("  What IS measurable here covers the two largest levers in economics.md:")
    print("  cache behaviour (60% of modelled savings) and thinking tokens (21%).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--project", default=None, help="substring filter")
    ap.add_argument("--json", default=None, help="write machine-readable output")
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        print("No transcript directory at %s" % a.root)
        sys.exit(1)

    overall, by_project, files, main_acc, sub_acc = scan(a.root, a.project)
    report(overall, by_project, files, main_acc, sub_acc)

    if a.json:
        out = {
            "turns": overall.turns,
            "sessions": len(overall.sessions),
            "tokens": {"uncached_input": overall.input, "cache_read": overall.read,
                       "cache_write_1h": overall.write_1h,
                       "cache_write_5m": overall.write_5m,
                       "output": overall.out, "thinking": overall.think},
            "cache_hit_rate": overall.hit_rate,
            "thinking_share": overall.think_share,
            "cost_usd": overall.cost,
            "main_vs_subagent": {"main_cost_usd": main_acc.cost, "main_turns": main_acc.turns,
                                 "subagent_cost_usd": sub_acc.cost, "subagent_turns": sub_acc.turns},
            "by_project": dict((k, {"cost_usd": v.cost, "hit_rate": v.hit_rate,
                                    "thinking_share": v.think_share, "turns": v.turns})
                               for k, v in by_project.items() if v.turns),
        }
        io.open(a.json, "w", encoding="utf-8").write(json.dumps(out, indent=2))
        print("\nwrote %s" % a.json)


if __name__ == "__main__":
    main()
