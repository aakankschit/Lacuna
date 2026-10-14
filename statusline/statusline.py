#!/usr/bin/env python3
"""Two-line Claude Code status line focused on what drives cost.

Line 1: model + effort, project, context bar, prompt-cache countdown (or re-cache cost when cold).
Line 2: session cost, last turn's cost split into cache read / cache write / output,
        cache hit ratio, plan usage for the 5-hour and 7-day windows.
Dollar figures are list-price estimates; on Pro/Max the plan-usage bars are what you spend.
The rate-limit and cache-TTL segments read fields that are not a public interface: when they are
absent those segments go blank rather than guess.

Deliberately self-contained — a status line runs on every refresh and must not fail — so it can be
copied anywhere. It picks up a pricing.json beside it or in the data directory if one exists.

Self-contained is not the same as free to differ. Where it reads what the CLI reads — the data
directory, the price files, the transcript — it reads them as scripts/lacuna_config.py and
scripts/cost/ccost.py do, by duplicated code rather than an import. It once fell behind on all
three at once: an unexpanded "~" in LACUNA_DATA, a malformed pricing.json, and a <synthetic> API
error line read as the last turn ("last $0.000").
"""
import json, math, os, sys, time

# $/MTok: input, 5m write, 1h write, cache read, output. Longest key first. The same table as
# scripts/pricing.json, because a copy of this file installed on its own cannot see that one.
PRICE = {
    "opus-5-5": (4, 5, 8, .20, 20), "sonnet-5-5": (2, 2.5, 4, .10, 10), "haiku-5-5": (.10, .125, .20, .01, .50),
    "fable-5-1": (10, 12.5, 20, .25, 50), "opus-5": (5, 6.25, 10, .50, 25), "opus-4-8": (5, 6.25, 10, .50, 25),
    "haiku-4-5": (1, 1.25, 2, .10, 5),
}
# The data directory exactly as lacuna_config resolves it: "~" and "$VAR" expanded, and no
# CLAUDE_PLUGIN_DATA fallback, which a hook sees and a shell does not.
DATA = os.path.expanduser(os.path.expandvars(os.environ.get("LACUNA_DATA") or "~/.lacuna"))


def _price_table(doc):
    """{model: 5-tuple} from a pricing file, accepting what lacuna_config accepts: the table under
    "models" or at the top level. A hand-edited entry that is not five numbers is skipped."""
    table = (doc.get("models") or doc) if isinstance(doc, dict) else None
    out = {}
    for k, v in (table.items() if isinstance(table, dict) else ()):
        if not isinstance(v, (list, tuple)):
            continue
        try:
            t = tuple(float(x) for x in v)
        except (TypeError, ValueError):
            continue
        if not str(k).startswith("_") and len(t) == 5 and all(math.isfinite(x) and x >= 0 for x in t):
            out[k[len("claude-"):] if k.startswith("claude-") else k] = t   # "claude-opus-5-5" is "opus-5-5"
    return out


for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "pricing.json"),
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json"),
           os.path.join(DATA, "pricing.json")):
    try:
        with open(_p) as _f:
            PRICE.update(_price_table(json.load(_f)))
    except Exception:     # this runs at import, outside main()'s guard: nothing here may raise
        pass
PRICE = dict(sorted(PRICE.items(), key=lambda kv: -len(kv[0])))
TIER = {"Haiku": 114, "Sonnet": 75, "Opus": 177, "Fable": 214}  # 256-colour codes per model family
DIM, RST = "\033[2m", "\033[0m"


def c(code, s):
    return f"\033[38;5;{code}m{s}{RST}"


def bar(frac, width=10):
    n = max(0, min(width, round(frac * width)))
    return "▰" * n + "▱" * (width - n)


def dur(sec):
    sec = max(0, int(sec))
    if sec >= 172_800:
        return f"{sec // 86400}d{sec % 86400 // 3600}h"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m"


def price(model):
    model = model if isinstance(model, str) else ""
    return next((v for k, v in PRICE.items() if k in model), PRICE["opus-5-5"])


def _num(v):
    """v if it is a finite number (not a bool), else None. stdin JSON is not ours: NaN and Infinity
    parse, and `round(nan)` raises."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _obj(v):
    return v if isinstance(v, dict) else {}


def _n(d, key):
    """A token count from a transcript: a missing, null or non-numeric field is 0."""
    v = _num(d.get(key)) if isinstance(d, dict) else None
    return v if v is not None else 0


def cost(u, model):
    """Dollar split of one usage block: (read$, write$, out$, uncached$)."""
    inp, w5, w1, rd, out = price(model)
    cc = u.get("cache_creation")
    t5, t1 = _n(cc, "ephemeral_5m_input_tokens"), _n(cc, "ephemeral_1h_input_tokens")
    if not (t5 or t1):
        t1 = _n(u, "cache_creation_input_tokens")
    return (_n(u, "cache_read_input_tokens") * rd / 1e6, (t5 * w5 + t1 * w1) / 1e6,
            _n(u, "output_tokens") * out / 1e6, _n(u, "input_tokens") * inp / 1e6)


def last_turn(path):
    """The most recent main-thread API call: ((read$, write$, out$, uncached$), earlier$).

    `earlier$` prices the attempts before the last in `usage.iterations` (a refusal, a fallback),
    which the top-level usage leaves out. Whether they were billed is not in the transcript."""
    # A path that is not a string must never reach open(): `true` is file descriptor 1, and the
    # `with` would close the status line's own stdout.
    if not isinstance(path, str) or not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 1_500_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, ValueError):       # ValueError: a NUL byte in the path
        return None
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except (ValueError, RecursionError):
            continue
        if not isinstance(e, dict):           # valid JSON that is not a transcript line
            continue
        m = e.get("message")
        u = m.get("usage") if isinstance(m, dict) else None
        if e.get("type") != "assistant" or not isinstance(u, dict) or e.get("isSidechain"):
            continue
        if m.get("model") == "<synthetic>":   # an API error Claude Code recorded, with zero usage
            continue
        its = u.get("iterations") if isinstance(u.get("iterations"), list) else []
        earlier = sum(sum(cost(i, i.get("model") or m.get("model"))) for i in its[:-1] if isinstance(i, dict))
        return cost(u, m.get("model")), earlier
    return None


def main():
    try:
        d = json.load(sys.stdin)
    except (ValueError, RecursionError):
        return          # a status line that prints a traceback is worse than one that prints nothing
    if not isinstance(d, dict):
        return
    try:
        cols = int(os.environ.get("COLUMNS") or 120)
    except ValueError:
        cols = 120
    # Every field is read by type: this is Claude Code's internal JSON, and a field of the wrong
    # type must blank its segment, not the whole line (or, for a path, close stdout).
    model = _obj(d.get("model"))
    name = model.get("display_name")
    name = name if isinstance(name, str) and name else "?"
    fam = next((k for k in TIER if k in name), "Opus")
    eff = _obj(d.get("effort")).get("level")
    eff = eff if isinstance(eff, str) else None
    cw = _obj(d.get("context_window"))
    u = _obj(cw.get("current_usage"))
    ctx = sum(_n(u, k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    win = _num(cw.get("context_window_size"))
    win = win if win and win > 0 else 1_000_000
    pc = _obj(d.get("prompt_cache"))
    _, _, w1, _, _ = price(model.get("id"))
    pdir, cwd = _obj(d.get("workspace")).get("project_dir"), d.get("cwd")
    proj = os.path.basename(next((x for x in (pdir, cwd) if isinstance(x, str) and x), ""))
    expires = _num(pc.get("expires_at"))

    # line 1: who, where, how full, how warm
    l1 = [c(TIER[fam], f"◆ {name}") + (f" {DIM}{eff}{RST}" if eff else "")]
    if proj:
        l1.append(f"{DIM}{proj}{RST}")
    if ctx:
        col = 114 if ctx < 100_000 else 221 if ctx < 200_000 else 203
        l1.append(f"ctx {c(col, bar(ctx / min(win, 400_000)))} {c(col, f'{ctx / 1000:.0f}k')}")
        left = (expires or 0) - time.time()
        if expires and left > 0:
            col = 114 if left > 600 else 221
            ttl = pc.get("ttl")
            ttl = ttl if isinstance(ttl, str) else ""
            # "1h cache · 42m left", never "1h 42m left" — which reads as a single duration
            l1.append(f"cache {c(col, '●')} " + (f"{ttl} cache · " if ttl else "")
                      + c(col, dur(left) + " left"))
        elif expires:
            # We were given an expiry time and it has passed: the cache is cold.
            l1.append(c(203, f"cache ○ cold · next prompt re-caches ≈${ctx * w1 / 1e6:.2f}"))
        # No expiry time (the field is absent before a process's first request, and null when the
        # last response had no cache tokens): say nothing rather than announce a cold cache that
        # may well be warm. A red dollar figure on a warm cache is worse than no figure.
        if ctx >= 150_000:
            l1.append(c(221, "→ /compact or handoff at next break"))
    else:
        l1.append(f"{DIM}ctx -{RST}")

    # line 2: what it has cost, what the last turn cost, how much plan is left
    cost = _num(_obj(d.get("cost")).get("total_cost_usd")) or 0
    l2 = [f"${cost:.2f} session"]
    lt = last_turn(d.get("transcript_path"))
    if lt:
        (rd, wr, out, unc), earlier = lt
        low = rd + wr + out + unc
        # Earlier attempts in the same call may or may not have been billed: show both ends.
        fig = f"${low:.3f}–${low + earlier:.3f}" if earlier >= 0.0005 else f"${low:.3f}"
        l2.append(f"last {fig} {DIM}(read {rd:.3f} · write {wr:.3f} · out {out:.3f}){RST}")
    hr = _num(pc.get("hit_ratio"))
    if hr is not None:
        l2.append(f"hit {hr * 100:.0f}%" if hr <= 1 else f"hit {hr:.0f}%")
    rl = _obj(d.get("rate_limits"))
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = _obj(rl.get(key))
        p = _num(w.get("used_percentage"))
        if p is None:
            continue
        col = 114 if p < 60 else 221 if p < 85 else 203
        resets = _num(w.get("resets_at"))
        reset = f" {DIM}↻{dur(resets - time.time())}{RST}" if resets else ""
        l2.append(f"{label} {c(col, bar(p / 100, 6))} {c(col, f'{p:.0f}%')}{reset}")

    sep = f" {DIM}│{RST} "
    print(sep.join(l1))
    print(sep.join(l2 if cols >= 90 else l2[:2]))


if __name__ == "__main__":
    # The status line runs on every refresh. Nothing it reports is worth breaking the prompt for.
    try:
        main()
    except Exception:
        pass
