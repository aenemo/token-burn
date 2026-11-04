"""Compare two usage.json documents (reference vs candidate), per platform.

Usage:
  python tests/parity_check.py --reference-url http://127.0.0.1:8421/api/usage.json \
      --candidate-url data/usage.json [--tolerance 1.0]

Each input may be an http(s) URL or a local file path. Tolerance is a percentage.

Per platform (claude, cursor) it checks: status, minutes, series key sets,
subagent_runs counts, plan key sets, and per-field totals of the series fields
(input, output, cache, cost, requests, sub_input, sub_output, sub_cache, sub_cost;
list values are summed, None counts as 0). Per-series differences over the
tolerance are listed too, but only platform totals and structure decide the exit code.

Exit code 0: every platform total is within tolerance and structures match. 1 otherwise.
Expect small drift when the two documents were generated at different moments.
"""
import argparse
import json
import sys
import urllib.request

PLATFORMS = ("claude", "cursor")
FIELDS = ("input", "output", "cache", "cost", "requests",
          "sub_input", "sub_output", "sub_cache", "sub_cost")


def load(src):
    if src.lower().startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def total(value):
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        return sum(v for v in value if v is not None)
    return value


def diff_pct(old, new):
    if old == new:
        return 0.0
    if old == 0:
        return 100.0
    return abs(new - old) / abs(old) * 100.0


def count_of(value):
    if value is None:
        return 0
    if isinstance(value, (list, dict, tuple)):
        return len(value)
    return value


def keyset(value):
    return set(value.keys()) if isinstance(value, dict) else set()


def series_totals(platform_doc):
    out = {}
    for s in platform_doc.get("series") or []:
        out[s.get("key")] = {f: total(s.get(f)) for f in FIELDS}
    return out


def compare_platform(name, ref, cand, tol, out):
    """Return the number of failures for one platform; append report lines to out."""
    fails = 0
    out.append("== %s ==" % name)
    for field in ("status", "minutes"):
        a, b = ref.get(field), cand.get(field)
        ok = a == b
        fails += not ok
        out.append("  %-14s ref=%r cand=%r %s" % (field, a, b, "ok" if ok else "MISMATCH"))

    a, b = count_of(ref.get("subagent_runs")), count_of(cand.get("subagent_runs"))
    ok = a == b
    fails += not ok
    out.append("  %-14s ref=%s cand=%s %s" % ("subagent_runs", a, b, "ok" if ok else "MISMATCH"))

    pa, pb = keyset(ref.get("plan")), keyset(cand.get("plan"))
    ok = pa == pb
    fails += not ok
    out.append("  plan keys      %s" % ("ok" if ok else "MISMATCH only-ref=%s only-cand=%s" % (
        sorted(pa - pb), sorted(pb - pa))))

    rs, cs = series_totals(ref), series_totals(cand)
    ka, kb = set(rs), set(cs)
    ok = ka == kb
    fails += not ok
    out.append("  series keys    %s" % ("ok (%d)" % len(ka) if ok else "MISMATCH only-ref=%s only-cand=%s" % (
        sorted(map(str, ka - kb)), sorted(map(str, kb - ka)))))

    out.append("  %-10s %16s %16s %9s" % ("field", "ref", "cand", "diff %"))
    for f in FIELDS:
        old = sum(v[f] for v in rs.values())
        new = sum(v[f] for v in cs.values())
        d = diff_pct(old, new)
        bad = d > tol
        fails += bad
        out.append("  %-10s %16s %16s %8.3f%%%s" % (f, "%.4f" % old if f.endswith("cost") or f == "cost" else "%d" % old,
                                                    "%.4f" % new if f.endswith("cost") or f == "cost" else "%d" % new,
                                                    d, " !" if bad else ""))

    over = []
    for key in sorted(ka & kb, key=str):
        for f in FIELDS:
            d = diff_pct(rs[key][f], cs[key][f])
            if d > tol:
                over.append("    %s.%s ref=%s cand=%s (%.3f%%)" % (key, f, rs[key][f], cs[key][f], d))
    if over:
        out.append("  series over tolerance (informational):")
        out.extend(over)
    return fails


def compare(ref_doc, cand_doc, tol):
    """Return (exit_code, report_text)."""
    out = []
    fails = 0
    rp, cp = ref_doc.get("platforms") or {}, cand_doc.get("platforms") or {}
    for name in PLATFORMS:
        if name not in rp or name not in cp:
            out.append("== %s == MISSING (ref=%s cand=%s)" % (name, name in rp, name in cp))
            fails += 1
            continue
        fails += compare_platform(name, rp[name], cp[name], tol, out)
    out.append("")
    out.append("Tolerance %.3f%%. RESULT: %s (%d failure(s))" % (tol, "FAIL" if fails else "PASS", fails))
    return (1 if fails else 0), "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description="Compare two usage.json documents per platform.")
    p.add_argument("--reference-url", required=True, help="URL or file path of the reference usage.json")
    p.add_argument("--candidate-url", required=True, help="URL or file path of the candidate usage.json")
    p.add_argument("--tolerance", type=float, default=1.0, help="allowed difference in percent (default 1.0)")
    args = p.parse_args(argv)
    try:
        ref, cand = load(args.reference_url), load(args.candidate_url)
    except (OSError, ValueError) as e:
        print("error loading input: %s" % e, file=sys.stderr)
        return 1
    code, report = compare(ref, cand, args.tolerance)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
