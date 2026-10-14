#!/usr/bin/env python3
"""Where did the tokens go? Report for the current session (or one transcript) plus the last N days.

usage: spend.py [--session PATH|ID] [--days N] [--cwd DIR] [--no-color]
Dollar figures are list-price estimates; on Pro/Max they measure plan usage, not billing. They are
also a lower bound (docs/LIMITS.md), and the report says so wherever it shows one: a call that was
refused and retried records the earlier attempts in usage.iterations, outside the top-level usage,
and whether they were billed is not in the transcript. Those are shown as the top of a range.
"""
import argparse, collections, glob, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccost import ROOT, avoidable_colds, earlier_usd, load, project_dir, short, split, ts

COLOR = True


def c(code, s):
    return f"\033[38;5;{code}m{s}\033[0m" if COLOR else str(s)


def dim(s):
    return f"\033[2m{s}\033[0m" if COLOR else str(s)


def bar(parts, width=40):
    """Stacked bar of read / write / output shares."""
    tot = sum(v for _, v, _ in parts) or 1
    out = ""
    for _, v, col in parts:
        out += c(col, "█" * round(width * v / tot))
    return out


LOWER_BOUND = "a lower bound — see docs/LIMITS.md"


def figure(low, gap):
    """A dollar figure that never silently drops the earlier attempts: a range when there are any."""
    return f"${low:.2f}–${low + gap:.2f}" if gap >= 0.005 else f"${low:.2f}"


def retried_line(n, gap):
    return (f"  {n} call(s) were retried after a refused or failed attempt. Those attempts would add "
            f"${gap:.2f} if billed; the transcript does not record whether they were, so the true "
            f"figure is anywhere in the range.")


def session_report(path):
    calls = load(path)
    if not calls:
        print("no API calls in", path)
        return
    tot = collections.Counter()
    turns = []
    for i, call in enumerate(calls):
        s, ctx = split(call)
        tot.update(s)
        turns.append((sum(s.values()), i, call, ctx, s))
    usd = sum(tot.values())
    nocache = sum(
        (call["u"].get("input_tokens", 0) + call["u"].get("cache_creation_input_tokens", 0)
         + call["u"].get("cache_read_input_tokens", 0)) * __import__("ccost").price(call["model"])[0] / 1e6
        + call["u"].get("output_tokens", 0) * __import__("ccost").price(call["model"])[4] / 1e6 for call in calls)
    span = ts(calls[-1]["ts"]) - ts(calls[0]["ts"])
    models = collections.Counter(short(x["model"]) for x in calls)
    colds = avoidable_colds(calls)
    cold = [(t, i, call) for t, i, call, _, _ in turns if i in colds]
    retried = [x for x in calls if x["earlier"]]
    gap = sum(earlier_usd(x) for x in retried)
    writes = tot["write5m"] + tot["write1h"]

    def share(x):        # a session that died on its first call has all-zero usage
        return f"{x / usd:.0%}" if usd else "0%"

    print(c(177, "■ This session") + dim(f"  {os.path.basename(path)[:8]}  {len(calls)} API calls over {span / 3600:.1f} h  ·  "
                                         + ", ".join(f"{m} ×{n}" for m, n in models.most_common())))
    print(f"  {c(255, figure(usd, gap))} list price, {LOWER_BOUND}   {dim(f'(same tokens without caching: ${nocache:.2f})')}")
    if gap >= 0.005:
        print(retried_line(len(retried), gap))
    print(f"  {bar([('read', tot['read'], 75), ('write', writes, 221), ('out', tot['output'], 177)])}")
    print(f"  {c(75, '█')} cache reads ${tot['read']:.2f} ({share(tot['read'])})   "
          f"{c(221, '█')} cache writes ${writes:.2f} ({share(writes)})   "
          f"{c(177, '█')} output+thinking ${tot['output']:.2f} ({share(tot['output'])})")
    if cold:
        print(f"  {c(203, f'{len(cold)} cold rewrites')} cost ${sum(t for t, _, _ in cold):.2f} "
              + dim("(whole context re-cached after the cache expired or was invalidated)"))
    print(dim("  most expensive turns:"))
    prev = {i: ctx for _, i, _, ctx, _ in turns}
    for t, i, call, ctx, s in sorted(turns, key=lambda x: -x[0])[:4]:
        delta = ctx - prev.get(i - 1, ctx)
        why = ("cold rewrite" if i in colds else
               f"+{delta / 1000:.0f}k new context" if delta > 15_000 else
               "long output/thinking" if s["output"] > 0.5 * t else "large context re-read")
        tools = ",".join(x.split("__")[-1] for x in call["tools"])[:30]
        print(f"    #{i:<4} {call['ts'][5:16].replace('T', ' ')}  ${t:.3f}  ctx {ctx / 1000:5.0f}k  {why}"
              + (dim(f"  → {tools}") if tools else ""))

    subs = glob.glob(os.path.join(path[:-6], "subagents", "*.jsonl"))
    if subs:
        by = collections.Counter()
        n = collections.Counter()
        for f in subs:
            sc = load(f)
            if sc:
                m = short(sc[0]["model"])
                by[m] += sum(sum(split(x)[0].values()) for x in sc)
                n[m] += 1
        print(f"  subagents: {sum(n.values())} runs, ${sum(by.values()):.2f}  "
              + dim("  ".join(f"{m} ×{n[m]} ${v:.2f}" for m, v in by.most_common())))


def fleet_report(days):
    since = time.time() - days * 86400
    by_kind, by_model, by_proj = collections.Counter(), collections.Counter(), collections.Counter()
    hit = collections.Counter()
    cold_n, cold_usd, sessions = 0, 0.0, set()
    # A forked or resumed session copies its parent's calls into a new file, message ids and all.
    # Deduplicated within a file only, one fork counted $23.44 of real spend twice.
    seen, retried, gap = set(), 0, 0.0
    for f in sorted(glob.glob(f"{ROOT}/**/*.jsonl", recursive=True)):
        if os.path.getmtime(f) < since:
            continue
        kind = "subagent" if "/subagents/" in f else "main"
        # Claude Code encodes a project path as "-Users-me-code-lab-a"; the last two segments
        # keep "lab-a" and "team-a" apart, which the last segment alone would merge into "a".
        enc = f[len(ROOT) + 1:].split("/")[0]
        proj = "-".join([p for p in enc.split("-") if p][-2:]) or "?"
        calls = load(f)
        colds = avoidable_colds(calls)
        for i, call in enumerate(calls):
            if ts(call["ts"]) < since:
                continue
            if call["id"] in seen:
                continue
            seen.add(call["id"])
            if call["earlier"]:
                retried += 1
                gap += earlier_usd(call)
            s, _ = split(call)
            usd = sum(s.values())
            by_kind[kind] += usd
            by_model[short(call["model"])] += usd
            by_proj[proj] += usd
            u = call["u"]
            hit["read"] += u.get("cache_read_input_tokens", 0)
            hit["all"] += u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("input_tokens", 0)
            if kind == "main" and i in colds:
                cold_n += 1
                cold_usd += usd
            sessions.add(f)
    tot = sum(by_kind.values())
    print(c(75, f"■ Last {days} days") + dim(f"  {len(sessions)} transcripts"))
    print(f"  {c(255, figure(tot, gap))} list price, {LOWER_BOUND}")
    print(f"  main ${by_kind['main']:.2f}  ·  subagents ${by_kind['subagent']:.2f}"
          f"  ·  cache hit {hit['read'] / max(hit['all'], 1):.1%}")
    if gap >= 0.005:
        print(retried_line(retried, gap))
    print(f"  {c(203, f'{cold_n} cold rewrites')} ${cold_usd:.2f}  " + dim("← avoidable with handoff + /clear before breaks"))
    print("  by model:   " + "  ".join(f"{m} ${v:.2f}" for m, v in by_model.most_common(6) if v >= 0.005))
    print("  by project: " + "  ".join(f"{p} ${v:.2f}" for p, v in by_proj.most_common(5)))


def main():
    global COLOR
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args()
    COLOR = not a.no_color and sys.stdout.isatty()
    path = a.session
    if path and not os.path.exists(path):
        path = next(iter(glob.glob(f"{ROOT}/*/{path}*.jsonl")), None)
        if not path:
            print(f"ERROR: no transcript matches --session {a.session!r}. Omit --session for this "
                  f"directory's most recent one.", file=sys.stderr)
            sys.exit(2)
    if not path:
        cands = glob.glob(os.path.join(project_dir(a.cwd), "*.jsonl"))
        path = max(cands, key=os.path.getmtime) if cands else None
    if path:
        session_report(path)
        print()
    fleet_report(a.days)


if __name__ == "__main__":
    main()
