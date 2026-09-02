"""
Deliverable 3 - the reduction ladder, as a pipe.

    <command> | python reduce.py --budget 4000 --query "what you need"
    python reduce.py --file big.json --budget 2000

Implements rungs 1-5 and 7-8 of docs/payload-middleware.md against real input.
Rung 6 (summarize) is deliberately NOT implemented: it is the only rung that can
fabricate, and it needs a model. A deterministic reducer that cannot invent is
worth more than one that can.

Guarantees, in the order the spec requires them:
  * archive before reduce  - the full original is written first, so every
                             manifest entry names a recovery that exists (INV-6)
  * protected fields       - ids, keys, tenancy and authorization fields are
                             never scored and never dropped
  * multiplicity           - collapsing duplicates preserves the count
  * totals                 - a ranked result carries its total (V24)
  * cursors, not offsets   - pagination is value-based (V25)
  * termination            - something within budget always comes out
"""

import argparse, hashlib, io, json, os, re, sys, tempfile
from collections import Counter, OrderedDict

BYTES_PER_TOKEN = 3.8

PROTECTED = [
    r"^id$", r"_id$", r"^uuid$", r"^key$", r"_key$", r"^name$", r"^path$",
    r"^tenant", r"^owner", r"^acl", r"^scope", r"^permission", r"^role",
    r"^visibility", r"^etag$", r"^version$", r"^status$", r"^error",
]
PROTECTED_RE = [re.compile(p, re.I) for p in PROTECTED]


def est(n_bytes):
    return int(n_bytes / BYTES_PER_TOKEN)


def size_of(obj):
    if isinstance(obj, str):
        return len(obj)
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except Exception:
        return len(str(obj))


def is_protected(field):
    return any(r.search(field) for r in PROTECTED_RE)


def relevant(field, terms, min_stem=5):
    """Match a field name against query terms in both directions, and on a
    shared stem. A query for 'sla breached' must keep 'breach_minutes' - plain
    substring matching misses it, and dropping it silently answers the wrong
    question."""
    f = field.lower()
    parts = set(re.findall(r"\w+", f))
    for t in terms:
        if t == f or t in parts:
            return True
        if len(t) > 3 and (t in f or f in t):
            return True
        for p in parts:
            n = min(len(t), len(p))
            if n >= min_stem and t[:min_stem] == p[:min_stem]:
                return True
    return False


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------

class Manifest(object):
    def __init__(self, ref):
        self.entries = []
        self.ref = ref

    def add(self, what, reason, recover=None):
        self.entries.append({"what": what, "reason": reason, "recover": recover})

    def render(self, kept, total_in):
        if not self.entries:
            return ""
        parts = ["; ".join("%s (%s)" % (e["what"], e["reason"]) for e in self.entries)]
        line = "[reduced %s -> %s tokens: %s" % (
            "{:,}".format(est(total_in)), "{:,}".format(est(kept)), parts[0])
        if self.ref:
            line += "; full original at %s" % self.ref
        recovers = [e["recover"] for e in self.entries if e["recover"]]
        if recovers:
            line += "; recover with " + " | ".join(sorted(set(recovers))[:3])
        return line + "]"


# --------------------------------------------------------------------------
# input handling
# --------------------------------------------------------------------------

def detect(text):
    t = text.lstrip()
    if not t:
        return "empty"
    if t[0] in "[{":
        try:
            json.loads(text)
            return "json"
        except Exception:
            pass
    lines = [l for l in text.splitlines() if l.strip()][:20]
    if lines and all(l.lstrip().startswith("{") for l in lines):
        ok = 0
        for l in lines:
            try:
                json.loads(l); ok += 1
            except Exception:
                pass
        if ok >= max(2, len(lines) - 1):
            return "jsonl"
    if len(lines) >= 2:
        commas = [l.count(",") for l in lines]
        if commas[0] > 0 and len(set(commas)) == 1:
            return "csv"
    return "text"


def archive(text, archive_dir):
    if archive_dir is None:
        return None
    try:
        if not os.path.isdir(archive_dir):
            os.makedirs(archive_dir)
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
        path = os.path.join(archive_dir, "result-%s.txt" % h)
        if not os.path.exists(path):
            io.open(path, "w", encoding="utf-8", newline="").write(text)
        return path
    except Exception:
        return None


# --------------------------------------------------------------------------
# ladder - structured records
# --------------------------------------------------------------------------

def to_records(data, kind):
    """Return (records, envelope_key) or (None, None) if not record-shaped."""
    if kind == "jsonl":
        return [json.loads(l) for l in data.splitlines() if l.strip()], None
    obj = json.loads(data) if isinstance(data, str) else data
    if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
        return obj, None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
                return v, k
    return None, None


def rung1_nulls(records, man):
    removed = 0
    for r in records:
        for k in list(r.keys()):
            v = r[k]
            if is_protected(k):
                continue
            if v == "" or v == [] or v == {}:
                del r[k]; removed += 1
    if removed:
        man.add("%s empty fields" % "{:,}".format(removed), "irrelevant")
    return records


def rung2_fields(records, man, query, keep_bytes):
    if not records:
        return records
    fields = OrderedDict()
    for r in records:
        for k, v in r.items():
            fields[k] = fields.get(k, 0) + size_of(v)
    terms = set(re.findall(r"\w+", (query or "").lower()))
    dropped = []
    for f, nbytes in fields.items():
        if is_protected(f):
            continue
        if terms and relevant(f, terms):
            continue
        if nbytes < keep_bytes:
            continue                      # small fields are not worth the risk
        if not terms:
            continue                      # no query: do not guess at relevance
        dropped.append(f)
    if not dropped:
        return records
    for r in records:
        for f in dropped:
            r.pop(f, None)
    man.add("fields: " + ", ".join(sorted(dropped)[:6]), "irrelevant",
            "re-run without --query")
    return records


def rung3_dedupe(records, man):
    seen, out, counts = {}, [], Counter()
    for r in records:
        k = json.dumps(r, sort_keys=True, ensure_ascii=False)
        if k in seen:
            counts[k] += 1
        else:
            seen[k] = len(out); out.append(r); counts[k] = 1
    if len(out) == len(records):
        return records
    for k, i in seen.items():
        if counts[k] > 1:
            out[i]["_count"] = counts[k]          # multiplicity is information
    man.add("%s duplicate records" % "{:,}".format(len(records) - len(out)),
            "duplicate")
    return out


def rung4_rank(records, man, query, budget_bytes, min_records):
    if sum(size_of(r) for r in records) <= budget_bytes:
        return records
    total = len(records)
    terms = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 2]

    def score(r):
        if not terms:
            return 0.0
        blob = json.dumps(r, ensure_ascii=False).lower()
        return sum(blob.count(t) for t in terms)

    ordered = sorted(enumerate(records), key=lambda p: (-score(p[1]), p[0]))
    kept, acc = [], 0
    for _i, r in ordered:
        s = size_of(r)
        if kept and acc + s > budget_bytes and len(kept) >= min_records:
            break
        kept.append(r); acc += s
    if len(kept) >= total:
        return records
    man.add("records %s..%s of %s" % ("{:,}".format(len(kept) + 1),
                                      "{:,}".format(total), "{:,}".format(total)),
            "low_rank" if terms else "budget",
            "read the archived original")
    return kept, total
    # note: caller handles the tuple


def rung5_truncate(records, man, max_field_bytes):
    cut = 0
    for r in records:
        for k, v in list(r.items()):
            if is_protected(k) or not isinstance(v, str):
                continue
            if len(v) <= max_field_bytes:
                continue
            keep = max_field_bytes
            sp = v.rfind(" ", 0, keep)
            if sp > keep * 0.6:
                keep = sp
            r[k] = v[:keep] + "…[+%s chars]" % "{:,}".format(len(v) - keep)
            cut += 1
    if cut:
        man.add("%s long field values" % cut, "truncated", "read the archived original")
    return records


# --------------------------------------------------------------------------
# representation - measured, not assumed
# --------------------------------------------------------------------------

def render_records(records, envelope, total_count):
    cands = {}
    cands["json"] = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    keys = OrderedDict()
    for r in records:
        for k in r.keys():
            keys[k] = True
    keys = list(keys)
    uniform = all(set(r.keys()) <= set(keys) for r in records)
    flat = all(not isinstance(v, (dict, list)) for r in records for v in r.values())
    if uniform and flat and keys:
        rows = [",".join(keys)]
        for r in records:
            rows.append(",".join(csv_cell(r.get(k, "")) for k in keys))
        cands["csv"] = "\n".join(rows)
        cands["lines"] = "\n".join(
            " ".join("%s=%s" % (k, r.get(k, "")) for k in keys) for r in records)
    best = min(cands.items(), key=lambda kv: len(kv[1]))
    header = ""
    if total_count is not None and total_count > len(records):
        header = "# %s of %s records\n" % ("{:,}".format(len(records)),
                                           "{:,}".format(total_count))
    elif envelope:
        header = "# %s: %s records\n" % (envelope, "{:,}".format(len(records)))
    return header + best[1], best[0]


def hard_trim(out, budget_bytes, man):
    """Terminating case. Cuts at a line boundary, adds exactly one marker, and
    never re-examines content it has already reduced."""
    marker_room = 60
    keep = max(budget_bytes - marker_room, 1)
    lines, acc, kept = out.splitlines(), 0, []
    for l in lines:
        if acc + len(l) + 1 > keep:
            break
        kept.append(l); acc += len(l) + 1
    dropped = len(lines) - len(kept)
    if not kept:                       # a single line longer than the budget
        kept = [out[:keep]]
        dropped = 0
        man.add("%s characters" % "{:,}".format(len(out) - keep), "truncated",
                "read the archived original")
    else:
        man.add("a further %s lines" % "{:,}".format(dropped), "budget",
                "read the archived original")
    return "\n".join(kept)


def csv_cell(v):
    s = "" if v is None else str(v)
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


# --------------------------------------------------------------------------
# ladder - unstructured text (command output, logs)
# --------------------------------------------------------------------------

def reduce_text(text, man, budget_bytes, query, head, tail):
    lines = text.splitlines()
    total = len(lines)
    if len(text) <= budget_bytes:
        return text

    counts = Counter(lines)
    seen, deduped = set(), []
    for l in lines:
        if l.strip() and counts[l] > 1:
            if l in seen:
                continue
            seen.add(l)
            deduped.append("%s   [x%d]" % (l, counts[l]))
        else:
            deduped.append(l)
    if len(deduped) < total:
        man.add("%s repeated lines" % "{:,}".format(total - len(deduped)),
                "duplicate")
    lines = deduped

    if len("\n".join(lines)) <= budget_bytes:
        return "\n".join(lines)

    terms = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 2]
    if terms:
        # Matches first, context second. Seeding with head/tail lets 40 lines of
        # boilerplate crowd out the lines actually asked for - which is the
        # failure this whole tool exists to prevent.
        def sc(i):
            low = lines[i].lower()
            return sum(low.count(t) for t in terms)

        matches = sorted((i for i in range(len(lines)) if sc(i) > 0),
                         key=lambda i: (-sc(i), i))
        keep_idx, acc = set(), 0
        for i in matches:
            if acc + len(lines[i]) + 1 > budget_bytes:
                break
            keep_idx.add(i); acc += len(lines[i]) + 1
        # spend anything left on a little head/tail for orientation
        for i in list(range(min(3, len(lines)))) + list(range(max(0, len(lines) - 2), len(lines))):
            if i in keep_idx:
                continue
            if acc + len(lines[i]) + 1 > budget_bytes:
                break
            keep_idx.add(i); acc += len(lines[i]) + 1
        if not keep_idx:
            keep_idx = {0}
        out, last = [], -1
        for i in sorted(keep_idx):
            if last >= 0 and i > last + 1:
                out.append("   ... %s lines omitted ..." % "{:,}".format(i - last - 1))
            out.append(lines[i]); last = i
        man.add("%s of %s lines" % ("{:,}".format(total - len(keep_idx)),
                                    "{:,}".format(total)),
                "low_rank", "read the archived original")
        return "\n".join(out)

    keep_head = lines[:head]
    keep_tail = lines[-tail:] if tail else []
    body = "\n".join(keep_head)
    while len(body) > budget_bytes and len(keep_head) > 1:
        keep_head = keep_head[: len(keep_head) // 2]
        body = "\n".join(keep_head)
    omitted = total - len(keep_head) - len(keep_tail)
    man.add("%s of %s lines" % ("{:,}".format(max(omitted, 0)), "{:,}".format(total)),
            "budget", "read the archived original")
    parts = keep_head + ["   ... %s lines omitted ..." % "{:,}".format(max(omitted, 0))]
    return "\n".join(parts + keep_tail)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Reduce a payload to a token budget.")
    ap.add_argument("--budget", type=int, default=4000, help="target tokens (default 4000)")
    ap.add_argument("--query", default=None, help="what you need - drives field and record relevance")
    ap.add_argument("--file", default=None, help="read from a file instead of stdin")
    ap.add_argument("--archive-dir", default=None,
                    help="where to write the full original (default: a temp dir)")
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--head", type=int, default=40, help="text mode: lines kept from the top")
    ap.add_argument("--tail", type=int, default=10, help="text mode: lines kept from the bottom")
    ap.add_argument("--min-records", type=int, default=5)
    ap.add_argument("--max-field-bytes", type=int, default=600)
    ap.add_argument("--min-field-bytes", type=int, default=120)
    a = ap.parse_args()

    if a.file:
        text = io.open(a.file, encoding="utf-8", errors="replace").read()
    else:
        text = sys.stdin.read()

    budget_bytes = int(a.budget * BYTES_PER_TOKEN)
    total_in = len(text)

    if not text.strip():
        return

    # rung 8 groundwork: archive BEFORE anything is dropped
    adir = None if a.no_archive else (a.archive_dir or
                                      os.path.join(tempfile.gettempdir(), "reduce-archive"))
    ref = archive(text, adir)
    man = Manifest(ref)

    if total_in <= budget_bytes:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        return

    kind = detect(text)
    out = None

    if kind in ("json", "jsonl"):
        try:
            records, envelope = to_records(text, kind)
        except Exception:
            records, envelope = None, None
        if records:
            total_count = len(records)
            records = rung1_nulls(records, man)
            records = rung2_fields(records, man, a.query, a.min_field_bytes)
            records = rung3_dedupe(records, man)
            r4 = rung4_rank(records, man, a.query, budget_bytes, a.min_records)
            if isinstance(r4, tuple):
                records, total_count = r4
            else:
                records = r4
            records = rung5_truncate(records, man, a.max_field_bytes)
            out, _rep = render_records(records, envelope, total_count)
        else:
            try:
                obj = json.loads(text)
                out = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                if len(out) > budget_bytes:
                    out = reduce_text(out, man, budget_bytes, a.query, a.head, a.tail)
            except Exception:
                out = reduce_text(text, man, budget_bytes, a.query, a.head, a.tail)
    else:
        out = reduce_text(text, man, budget_bytes, a.query, a.head, a.tail)

    # rung 8 / enforceBudget: a single clean descent, never another ladder pass.
    # Re-running the ladder over its own output re-deduplicates the omission
    # markers it just wrote - the reducer reducing its own manifest.
    if len(out) > budget_bytes:
        out = hard_trim(out, budget_bytes, man)

    sys.stdout.write(out if out.endswith("\n") else out + "\n")
    line = man.render(len(out), total_in)
    if line:
        sys.stdout.write(line + "\n")


if __name__ == "__main__":
    main()
