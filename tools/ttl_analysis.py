"""
Phase 0c - is the 1-hour cache TTL earning its 2x write premium?

A cache write costs 1.25x base input at the 5-minute TTL and 2.0x at 1 hour. A
read refreshes the entry's timer at no cost, so:

  * consecutive prefix-sharing requests starting under 5 minutes apart keep a
    5-minute entry warm indefinitely - the 1-hour TTL buys nothing there except
    the doubled write price
  * a gap of 5-60 minutes is the only window where 1h pays: it survives, while
    a 5-minute entry expires and the next request rewrites the whole prefix
  * over an hour, neither helps

So the question is empirical: how often do gaps land in the 5-60 minute window,
and is surviving them worth paying 2x on every write?

CONTENT-FREE. Reads timestamp, sessionId, type and message.usage only.

Usage:
    python ttl_analysis.py [--project NAME]
"""

import argparse, collections, glob, io, json, os
from datetime import datetime

BASE_INPUT_PER_MTOK = 5.00          # Opus-tier
W5, W1H = 1.25, 2.00                # write multipliers


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def scan(root, project_filter):
    sessions = collections.defaultdict(list)   # sid -> [(ts, prompt_tokens, write_tokens)]
    for proj in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(proj):
            continue
        if project_filter and project_filter.lower() not in os.path.basename(proj).lower():
            continue
        for dirpath, _dn, fns in os.walk(proj):
            if "subagents" in dirpath.replace("\\", "/").split("/"):
                continue                        # main sessions only: sub-agents share ids
            for fn in fns:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, fn)
                key = os.path.basename(path)    # file == one session, avoids id collisions
                for line in io.open(path, encoding="utf-8", errors="replace"):
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    m = d.get("message") or {}
                    u = m.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = parse_ts(d.get("timestamp"))
                    if ts is None:
                        continue
                    prompt = ((u.get("input_tokens") or 0)
                              + (u.get("cache_read_input_tokens") or 0)
                              + (u.get("cache_creation_input_tokens") or 0))
                    cc = u.get("cache_creation") or {}
                    w = ((cc.get("ephemeral_1h_input_tokens") or 0)
                         + (cc.get("ephemeral_5m_input_tokens") or 0)
                         or (u.get("cache_creation_input_tokens") or 0))
                    sessions[key].append((ts, prompt, w))
    return sessions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--project", default=None)
    a = ap.parse_args()

    sessions = scan(a.root, a.project)
    gaps = []                      # (seconds, prompt_tokens_of_the_following_turn)
    writes = 0
    for _sid, rows in sessions.items():
        rows.sort(key=lambda r: r[0])
        for i in range(1, len(rows)):
            dt = (rows[i][0] - rows[i - 1][0]).total_seconds()
            if dt < 0:
                continue
            gaps.append((dt, rows[i][1]))
        writes += sum(r[2] for r in rows)

    if not gaps:
        print("No timestamped turns found.")
        return

    W = 74
    print("=" * W)
    print("PHASE 0c - CACHE TTL: IS 1 HOUR EARNING ITS 2x WRITE PREMIUM?")
    print("=" * W)
    print("sessions            : {:,}".format(len(sessions)))
    print("consecutive turns   : {:,}".format(len(gaps)))
    print("cache writes        : {:,} tokens".format(writes))

    bands = [("under 1 min", 0, 60), ("1-5 min", 60, 300),
             ("5-60 min  <- the only window 1h pays", 300, 3600),
             ("over 1 hour", 3600, 10 ** 12)]
    print("")
    print("-" * W)
    print("GAP BETWEEN CONSECUTIVE TURNS  (start to start)")
    print("-" * W)
    counts = {}
    for label, lo, hi in bands:
        sel = [g for g in gaps if lo <= g[0] < hi]
        counts[label] = sel
        print("  %-38s %8s  %5.1f%%" % (label, "{:,}".format(len(sel)),
                                        100.0 * len(sel) / len(gaps)))

    srt = sorted(g[0] for g in gaps)
    print("")
    print("  p50 %.0fs   p90 %.0fs   p99 %.0fs"
          % (srt[len(srt) // 2], srt[int(len(srt) * 0.9)], srt[int(len(srt) * 0.99)]))

    # --- the comparison -----------------------------------------------------
    mid = counts["5-60 min  <- the only window 1h pays"]
    rebuild_tokens = sum(g[1] for g in mid)      # prefix that a 5m entry would lose

    cost_1h = writes * BASE_INPUT_PER_MTOK * W1H / 1e6
    cost_5m = (writes * BASE_INPUT_PER_MTOK * W5 / 1e6
               + rebuild_tokens * BASE_INPUT_PER_MTOK * W5 / 1e6)

    print("")
    print("-" * W)
    print("THE COMPARISON")
    print("-" * W)
    print("  all writes at 1h  : %s tok x 2.00   = $%8.2f" % ("{:,}".format(writes), cost_1h))
    print("  all writes at 5m  : %s tok x 1.25   = $%8.2f"
          % ("{:,}".format(writes), writes * BASE_INPUT_PER_MTOK * W5 / 1e6))
    print("    + cold rebuilds : %s tok x 1.25   = $%8.2f"
          % ("{:,}".format(rebuild_tokens), rebuild_tokens * BASE_INPUT_PER_MTOK * W5 / 1e6))
    print("                                        ---------")
    print("  5m total                              $%8.2f" % cost_5m)
    print("")
    delta = cost_1h - cost_5m
    if delta > 0:
        print("  => 5-minute TTL would be CHEAPER by $%.2f" % delta)
        print("     %.1f%% of gaps sit in the 5-60 min window; the 2x premium on every"
              % (100.0 * len(mid) / len(gaps)))
        print("     write is not repaid by the rebuilds it avoids.")
    else:
        print("  => 1-hour TTL is EARNING its premium by $%.2f" % (-delta))
        print("     %.1f%% of gaps sit in the 5-60 min window, and the prefix rebuilds"
              % (100.0 * len(mid) / len(gaps)))
        print("     a 5-minute entry would force cost more than the 2x on writes.")
    print("")
    print("  Caveat: TTL is a harness setting, not an application one. This says")
    print("  what the right choice would be, not that it is available to change.")


if __name__ == "__main__":
    main()
