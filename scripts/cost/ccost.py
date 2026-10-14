"""Shared pricing and transcript parsing for Claude Code cost tools.

Prices are list $/MTok and live in scripts/pricing.json; override them for your account in
$LACUNA_DATA/pricing.json rather than editing code.
Transcript JSONL is an internal format, not a documented contract: treat results as estimates.
"""
import json, os, re, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lacuna_config

PRICE = lacuna_config.prices()   # input, 5m write, 1h write, cache read, output; longest key first
ROOT = lacuna_config.TRANSCRIPTS
FALLBACK = PRICE.get("opus-5-5") or next(iter(PRICE.values()))


def price(model):
    model = lacuna_config.resolve(model)
    return next((v for k, v in PRICE.items() if k in (model or "")), FALLBACK)


def short(model):
    return re.sub(r"^claude-|-\d{8}$", "", model or "?")


def load(path):
    """API calls in order, deduplicated by message id: dicts with id, model, usage, ts, tools, side
    (a sidechain call, on another thread than the main conversation) and earlier (the attempts
    before the last in usage.iterations, as (usage, model) pairs).

    Claude Code records a failed API call (a session limit, an overload) as an assistant line whose
    model is <synthetic> and whose usage is zero. It is not a call: counted as one it inflated the
    call count and, as a session's first line, turned the real first cache write into an
    "avoidable" one."""
    calls, order = {}, []
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except (ValueError, RecursionError):   # an absurdly nested line recurses, not ValueErrors
                continue
            if not isinstance(d, dict) or d.get("type") != "assistant":
                continue
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            u, mid = m.get("usage"), m.get("id")
            if not isinstance(u, dict) or not mid or m.get("model") == "<synthetic>":
                continue
            u = {k: (v if v is not None else 0) for k, v in u.items()}   # a null count is 0, not a crash
            if mid not in calls:
                order.append(mid)
            tools = [c.get("name") for c in m.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use"]
            its = u.get("iterations") if isinstance(u.get("iterations"), list) else []
            calls[mid] = dict(id=mid, model=m.get("model"), u=u, ts=d.get("timestamp") or "",
                              side=bool(d.get("isSidechain")),
                              earlier=[(i, i.get("model") or m.get("model")) for i in its[:-1] if isinstance(i, dict)],
                              tools=calls.get(mid, {}).get("tools", []) + tools)
    return [calls[i] for i in order]


def split(c):
    """Dollar split of one call: dict(uncached, write5m, write1h, read, output) and context size."""
    return split_usage(c["u"], c["model"])


def earlier_usd(c):
    """What the attempts before the last in usage.iterations would cost, if they were billed.

    The top-level usage is the last attempt only. Anthropic bills a refusal before any output only
    for some categories, and the transcript does not record which: so this is the gap between the
    figure reported and the most it could have been, never something to add in silently."""
    return sum(earlier_split(c).values())


def earlier_split(c):
    """earlier_usd by category (uncached, write5m, write1h, read, output), so a report that shows a
    category subtotal can show the same range as the total."""
    out = {}
    for u, model in c.get("earlier", ()):
        for k, v in split_usage(u, model)[0].items():
            out[k] = out.get(k, 0) + v
    return out


def split_usage(u, model):
    pi, p5, p1, pr, po = price(model)
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = 0, u.get("cache_creation_input_tokens", 0)  # no breakdown: assume 1h
    s = dict(uncached=u.get("input_tokens", 0) * pi, write5m=(w5 or 0) * p5, write1h=(w1 or 0) * p1,
             read=u.get("cache_read_input_tokens", 0) * pr, output=u.get("output_tokens", 0) * po)
    s = {k: v / 1e6 for k, v in s.items()}
    ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return s, ctx


def ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


IDLE_S = 300    # the shortest cache lifetime: a re-write sooner than this is not an expired cache


def is_cold(c, prev=None):
    """True when this call paid to write a large cache instead of reading one.

    A warm call reads about what the previous call had in context. After the cache expires, the
    next call still reads what is warm for everyone — Claude Code's ~42k system prompt and tools —
    and writes the rest. So "reads under 40k" alone missed most idle re-caches: on one machine, 9
    worth $39.66 each read 42.2k-42.7k (one: 6.5 h idle, 647,597 written, 42,210 read). Against
    the previous call, such a write reads under half of it after an idle gap. The gap keeps out a
    compaction, which also writes a new context, seconds after the call before it.
    """
    u = c["u"]
    read = u.get("cache_read_input_tokens", 0)
    if u.get("cache_creation_input_tokens", 0) <= 40_000:
        return False
    if prev is None:
        # Nothing to compare against: a large write with almost no read is the only safe signal.
        return read < 40_000
    # Against the previous call is the only honest test, at every size. "read under 40k" alone
    # counted a warm cache as waste whenever a small session added a large tool result: measured,
    # 54 of 72 flagged calls on one machine had read ~all of the previous context (one read 11,115
    # of 11,117 and wrote 43,903, 45 s later). That is the cache working, not idling.
    # A /model switch re-writes the cache too, minutes after the last call: that is not idling.
    return (read < split(prev)[1] / 2 and ts(c["ts"]) - ts(prev["ts"]) >= IDLE_S
            and c["model"] == prev["model"])


def is_avoidable_cold(c, prev):
    """True when a cold write could have been avoided. `prev` is the call before it on the same
    thread, or None.

    A session's FIRST call always writes its cache; that is the price of starting work, not waste.
    Every later cold write means the cache expired while the session sat idle.
    """
    return prev is not None and is_cold(c, prev)


def avoidable_colds(calls):
    """Indices of the avoidable cold rewrites among one transcript's calls. Both the session report
    and the fleet report use this, or the same transcript gets two different counts. A sidechain
    call is on another thread, so it is neither judged nor the "previous call" of a main one."""
    out, prev = set(), None
    for i, c in enumerate(calls):
        if c.get("side"):
            continue
        if is_avoidable_cold(c, prev):
            out.add(i)
        prev = c
    return out


def project_dir(cwd):
    return os.path.join(ROOT, re.sub(r"[^A-Za-z0-9]", "-", cwd))
