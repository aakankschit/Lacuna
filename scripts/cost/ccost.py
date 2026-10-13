"""Shared pricing and transcript parsing for Claude Code cost tools.

Prices are list $/MTok and live in scripts/lacuna_config.py; override them for your account in
$LACUNA_DATA/pricing.json rather than editing code.
Transcript JSONL is an internal format, not a documented contract: treat results as estimates.
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lacuna_config

PRICE = lacuna_config.prices()   # input, 5m write, 1h write, cache read, output; longest key first
ROOT = lacuna_config.TRANSCRIPTS
FALLBACK = PRICE.get("opus-5-5") or next(iter(PRICE.values()))


def price(model):
    return next((v for k, v in PRICE.items() if k in (model or "")), FALLBACK)


def short(model):
    return re.sub(r"^claude-|-\d{8}$", "", model or "?")


def load(path):
    """API calls in order, deduplicated by message id: dicts with model, usage, ts, tools."""
    calls, order = {}, []
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            u, mid = m.get("usage"), m.get("id")
            if not u or not mid:
                continue
            if mid not in calls:
                order.append(mid)
            tools = [c.get("name") for c in m.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use"]
            calls[mid] = dict(model=m.get("model"), u=u, ts=d.get("timestamp") or "",
                              tools=calls.get(mid, {}).get("tools", []) + tools)
    return [calls[i] for i in order]


def split(c):
    """Dollar split of one call: dict(uncached, write5m, write1h, read, output) and context size."""
    u = c["u"]
    pi, p5, p1, pr, po = price(c["model"])
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = 0, u.get("cache_creation_input_tokens", 0)  # no breakdown: assume 1h
    s = dict(uncached=u.get("input_tokens", 0) * pi, write5m=(w5 or 0) * p5, write1h=(w1 or 0) * p1,
             read=u.get("cache_read_input_tokens", 0) * pr, output=u.get("output_tokens", 0) * po)
    s = {k: v / 1e6 for k, v in s.items()}
    ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return s, ctx


def is_cold(c):
    u = c["u"]
    return u.get("cache_creation_input_tokens", 0) > 40_000 and u.get("cache_read_input_tokens", 0) < 40_000


def project_dir(cwd):
    return os.path.join(ROOT, re.sub(r"[^A-Za-z0-9]", "-", cwd))
