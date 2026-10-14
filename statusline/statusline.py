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
"""
import json, os, sys, time

# $/MTok: input, 5m write, 1h write, cache read, output. Longest key first.
PRICE = {
    "opus-5-5": (4, 5, 8, .20, 20), "sonnet-5-5": (2, 2.5, 4, .10, 10), "haiku-5-5": (.10, .125, .20, .01, .50),
    "fable-5-1": (10, 12.5, 20, .25, 50), "opus-5": (5, 6.25, 10, .50, 25), "opus-4-8": (5, 6.25, 10, .50, 25),
}
for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "pricing.json"),
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json"),
           os.path.join(os.environ.get("LACUNA_DATA")
                        or os.environ.get("CLAUDE_PLUGIN_DATA")
                        or os.path.expanduser("~/.lacuna"), "pricing.json")):
    try:
        with open(_p) as _f:
            PRICE.update({k: tuple(v) for k, v in (json.load(_f).get("models") or {}).items()
                          if not k.startswith("_") and len(v) == 5})
    except (OSError, ValueError, TypeError):
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
    return next((v for k, v in PRICE.items() if k in (model or "")), PRICE["opus-5-5"])


def last_turn(path):
    """Cost split of the most recent main-thread API call: (read$, write$, out$, uncached$)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 1_500_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, TypeError):
        return None
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        m = e.get("message") or {}
        u = m.get("usage")
        if e.get("type") != "assistant" or not u or e.get("isSidechain"):
            continue
        inp, w5, w1, rd, out = price(m.get("model"))
        cc = u.get("cache_creation") or {}
        t5, t1 = cc.get("ephemeral_5m_input_tokens", 0), cc.get("ephemeral_1h_input_tokens", 0)
        if not (t5 or t1):
            t1 = u.get("cache_creation_input_tokens", 0)
        return (u.get("cache_read_input_tokens", 0) * rd / 1e6, (t5 * w5 + t1 * w1) / 1e6,
                u.get("output_tokens", 0) * out / 1e6, u.get("input_tokens", 0) * inp / 1e6)
    return None


def main():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return          # a status line that prints a traceback is worse than one that prints nothing
    cols = int(os.environ.get("COLUMNS", 120))
    model = d.get("model") or {}
    name = model.get("display_name", "?")
    fam = next((k for k in TIER if k in name), "Opus")
    eff = (d.get("effort") or {}).get("level")
    cw = d.get("context_window") or {}
    u = cw.get("current_usage") or {}
    ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    win = cw.get("context_window_size") or 1_000_000
    pc = d.get("prompt_cache") or {}
    _, _, w1, _, _ = price(model.get("id"))
    proj = os.path.basename((d.get("workspace") or {}).get("project_dir") or d.get("cwd") or "")

    # line 1: who, where, how full, how warm
    l1 = [c(TIER[fam], f"◆ {name}") + (f" {DIM}{eff}{RST}" if eff else "")]
    if proj:
        l1.append(f"{DIM}{proj}{RST}")
    if ctx:
        col = 114 if ctx < 100_000 else 221 if ctx < 200_000 else 203
        l1.append(f"ctx {c(col, bar(ctx / min(win, 400_000)))} {c(col, f'{ctx / 1000:.0f}k')}")
        left = (pc.get("expires_at") or 0) - time.time()
        if pc.get("expires_at") and left > 0:
            col = 114 if left > 600 else 221
            ttl = pc.get("ttl", "")
            # "1h cache · 42m left", never "1h 42m left" — which reads as a single duration
            l1.append(f"cache {c(col, '●')} " + (f"{ttl} cache · " if ttl else "")
                      + c(col, dur(left) + " left"))
        elif pc.get("expires_at"):
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
    cost = (d.get("cost") or {}).get("total_cost_usd") or 0
    l2 = [f"${cost:.2f} session"]
    lt = last_turn(d.get("transcript_path"))
    if lt:
        rd, wr, out, unc = lt
        l2.append(f"last ${rd + wr + out + unc:.3f} {DIM}(read {rd:.3f} · write {wr:.3f} · out {out:.3f}){RST}")
    if pc.get("hit_ratio") is not None:
        l2.append(f"hit {pc['hit_ratio'] * 100:.0f}%" if pc["hit_ratio"] <= 1 else f"hit {pc['hit_ratio']:.0f}%")
    rl = d.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key)
        if not w or w.get("used_percentage") is None:
            continue
        p = w["used_percentage"]
        col = 114 if p < 60 else 221 if p < 85 else 203
        reset = f" {DIM}↻{dur(w['resets_at'] - time.time())}{RST}" if w.get("resets_at") else ""
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
