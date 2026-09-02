"""
Phase 0b - what is filling the context?

The `usage` block gives cached/uncached/written totals but no intra-input split.
This recovers the split from the transcript's own records: it measures the SIZE
of every content block and attributes it to a category, and for tool results to
the tool that produced them.

CONTENT-FREE BY CONSTRUCTION. It measures len(json.dumps(block)) and reads
block type, tool name, and attachment type - all metadata. It never inspects,
stores or prints the text of any message, tool result or attachment.

Two outputs:
  1. Attribution  - which categories account for the bytes entering context
  2. Growth curve - how context size evolves with turn index, from the
                    cache_read totals, which are the actual prefix size

Usage:
    python context_growth.py [--project NAME] [--include-subagents]
"""

import argparse, collections, glob, io, json, os

BYTES_PER_TOKEN = 3.8      # matches tools/baseline_profile.py conventions


def est(nbytes):
    return int(nbytes / BYTES_PER_TOKEN)


def blocksize(b):
    try:
        return len(json.dumps(b, ensure_ascii=False))
    except Exception:
        return 0


class Growth(object):
    def __init__(self):
        self.cat = collections.Counter()          # category -> bytes
        self.tool_bytes = collections.Counter()   # tool name -> result bytes
        self.tool_calls = collections.Counter()
        self.tool_sizes = collections.defaultdict(list)
        self.att = collections.Counter()          # attachment type -> bytes
        self.att_n = collections.Counter()
        self.by_turn = collections.defaultdict(list)   # turn index -> prompt sizes
        self.turns = 0


def scan(root, project_filter, include_sub):
    g = Growth()
    for proj in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(proj):
            continue
        if project_filter and project_filter.lower() not in os.path.basename(proj).lower():
            continue
        for dirpath, _dn, filenames in os.walk(proj):
            is_sub = "subagents" in dirpath.replace("\\", "/").split("/")
            if is_sub and not include_sub:
                continue
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                scan_file(os.path.join(dirpath, fn), g)
    return g


def scan_file(path, g):
    id2tool = {}
    turn = 0
    try:
        fh = io.open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")

            if t == "attachment":
                a = d.get("attachment") or {}
                k = a.get("type") or "?"
                g.att[k] += blocksize(a)
                g.att_n[k] += 1
                g.cat["attachments / system reminders"] += blocksize(a)
                continue

            m = d.get("message")
            if not isinstance(m, dict):
                continue
            c = m.get("content")

            if t == "assistant":
                u = m.get("usage")
                if isinstance(u, dict):
                    turn += 1
                    g.turns += 1
                    prompt = ((u.get("input_tokens") or 0)
                              + (u.get("cache_read_input_tokens") or 0)
                              + (u.get("cache_creation_input_tokens") or 0))
                    if prompt:
                        g.by_turn[turn].append(prompt)
                if isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        n = blocksize(b)
                        if bt == "text":
                            g.cat["assistant text"] += n
                        elif bt == "thinking":
                            g.cat["assistant thinking"] += n
                        elif bt == "tool_use":
                            g.cat["tool call parameters"] += n
                            name = b.get("name") or "?"
                            g.tool_calls[name] += 1
                            if b.get("id"):
                                id2tool[b["id"]] = name

            elif t == "user":
                if isinstance(c, str):
                    g.cat["user messages"] += len(c)
                elif isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        n = blocksize(b)
                        if bt == "tool_result":
                            g.cat["tool results"] += n
                            name = id2tool.get(b.get("tool_use_id"), "?")
                            g.tool_bytes[name] += n
                            g.tool_sizes[name].append(n)
                        elif bt in ("text",):
                            g.cat["user messages"] += n
                        else:
                            g.cat["other blocks"] += n


def pct(x):
    return "%5.1f%%" % (100 * x)


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def report(g, include_sub):
    W = 78
    print("=" * W)
    print("PHASE 0b - WHAT IS FILLING THE CONTEXT")
    print("=" * W)
    print("scope               : %s" % ("main sessions + sub-agents" if include_sub
                                        else "main sessions only"))
    print("assistant turns     : {:,}".format(g.turns))

    total = sum(g.cat.values())
    if not total:
        print("\nNothing measured.")
        return

    print("")
    print("-" * W)
    print("ATTRIBUTION  (bytes entering the transcript, by category)")
    print("-" * W)
    print("  %-34s %14s %9s %9s" % ("category", "est. tokens", "share", "per turn"))
    for k, v in g.cat.most_common():
        print("  %-34s %14s %8s %9s"
              % (k, "{:,}".format(est(v)), pct(v / float(total)),
                 "{:,}".format(est(v) // max(g.turns, 1))))
    print("  %-34s %14s" % ("TOTAL", "{:,}".format(est(total))))

    print("")
    print("-" * W)
    print("TOOL RESULTS  (the largest single category, by tool)")
    print("-" * W)
    tr_total = float(g.cat.get("tool results", 0)) or 1.0
    print("  %-22s %12s %8s %10s %10s %8s"
          % ("tool", "est. tokens", "share", "calls", "median", "p95"))
    for name, v in g.tool_bytes.most_common(12):
        sizes = sorted(g.tool_sizes[name])
        p95 = sizes[int(len(sizes) * 0.95)] if sizes else 0
        print("  %-22s %12s %7s %10s %10s %8s"
              % (name[:22], "{:,}".format(est(v)), pct(v / tr_total),
                 "{:,}".format(len(sizes)), "{:,}".format(est(median(sizes))),
                 "{:,}".format(est(p95))))

    print("")
    print("-" * W)
    print("ATTACHMENTS  (injected by the harness, not by you)")
    print("-" * W)
    print("  %-34s %12s %10s %10s" % ("type", "est. tokens", "count", "each"))
    for k, v in g.att.most_common(8):
        n = g.att_n[k]
        print("  %-34s %12s %10s %10s"
              % (k[:34], "{:,}".format(est(v)), "{:,}".format(n),
                 "{:,}".format(est(v) // max(n, 1))))

    print("")
    print("-" * W)
    print("GROWTH CURVE  (median prompt size at turn N, from cache_read)")
    print("-" * W)
    marks = [1, 2, 3, 5, 10, 20, 40, 75, 150, 300, 600, 1200]
    prev = None
    for mk in marks:
        vals = []
        for turn, sizes in g.by_turn.items():
            if mk <= turn < mk * 1.6:
                vals.extend(sizes)
        if not vals:
            continue
        med = median(vals)
        delta = ""
        if prev:
            delta = "  %+7s/turn" % "{:,}".format(int((med - prev) / max(mk * 0.6, 1)))
        print("  turn %-6s n=%-6s median prompt %11s%s"
              % (mk, len(vals), "{:,}".format(med), delta))
        prev = med

    print("")
    print("-" * W)
    print("READING THIS")
    print("-" * W)
    print("  Byte sizes are converted at %.1f bytes/token and are an estimate."
          % BYTES_PER_TOKEN)
    print("  They measure what ENTERS the transcript. The prompt is the accumulated")
    print("  prefix, so a category that is small per turn can still dominate the")
    print("  prompt once it has accumulated across a long session - compare the")
    print("  per-turn column against the growth curve.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--project", default=None)
    ap.add_argument("--include-subagents", action="store_true")
    a = ap.parse_args()
    g = scan(a.root, a.project, a.include_subagents)
    report(g, a.include_subagents)


if __name__ == "__main__":
    main()
