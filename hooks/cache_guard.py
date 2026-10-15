#!/usr/bin/env python3
"""Prompt-cache guard for Claude Code.

An idle session is expensive in a way nothing on screen tells you about: once the prompt cache
expires, the next prompt silently pays a full cold write of the whole context. This hook makes
that cost visible before you pay it.

SessionStart      prints a one-line routing reminder; on resume, warns when the cache has
                  probably expired and what the first prompt will cost.
UserPromptSubmit  holds the first prompt after an idle period on a large context, says what
                  sending it costs, and lets the next prompt straight through.

The estimate is a heuristic over cache behaviour that is not a documented contract, and it is
list price, not your bill. It can be wrong in either direction; it is meant to be noticed, not
trusted to the cent.

Settings: LACUNA_IDLE_MIN or `idle_minutes` in config.json (default 60),
LACUNA_MIN_CTX (40000), LACUNA_GUARD_OFF=1 to disable entirely.
"""
import json, math, os, re, sys, time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
try:
    import lacuna_config
    _DATA, _CFG, _PRICES = lacuna_config.DATA, lacuna_config.CONFIG, lacuna_config.prices()
except Exception:                                      # the hook must never break a session
    _DATA = os.path.expanduser(os.path.expandvars(os.environ.get("LACUNA_DATA") or "~/.lacuna"))
    _CFG, _PRICES = {}, {}


def _num(value, default, lo=0.0, hi=1e9):
    """A setting that must be a finite number in [lo, hi]. config.json is hand-edited and the
    environment is typed by hand ("inf", "nan", "-5" all parse as floats), and a hook that raises
    at import breaks every prompt in the session: anything else is the default."""
    if isinstance(value, bool):
        return float(default)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) and lo <= v <= hi else float(default)


def _count(d, key):
    """A token count or duration from hook JSON: a finite non-negative number, else None."""
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 else None


# 1 minute to a year of idle; 0 would hold every prompt. A bad environment value falls back to the
# config file's, then to the default.
IDLE_MIN = _num(os.environ.get("LACUNA_IDLE_MIN"), _num(_CFG.get("idle_minutes"), 60, 1, 525_600), 1, 525_600)
MIN_CTX = int(_num(os.environ.get("LACUNA_MIN_CTX"), 40_000))
GRACE_S = 15 * 60
STATE = os.path.join(_DATA, "cache_guard")
FALLBACK_WRITE_1H = 8.0   # $/MTok if the model is not in the price table
_m = _CFG.get("models", {})
ROUTING = (f"Routing (measured — see the plugin's docs/benchmarks): "
           f"{_m.get('reader', 'haiku')} reads and fact-checks · {_m.get('coder', 'sonnet')} writes code · "
           f"{_m.get('planner', 'claude-opus-5')} plans · {_m.get('tester', 'sonnet')} writes tests. "
           f"Effort is a cost multiplier, not a quality lever. "
           f"Stepping away for more than {IDLE_MIN:.0f} min? Write a handoff, then /clear.")


def compacted_since(path, after_ts):
    """True if the transcript shows a compaction after the given time.

    /compact is the remedy this hook recommends, and it replaces the context with a summary. The
    newest assistant call still in the transcript is the pre-compaction one, so without this the
    hold quotes several times the real context immediately after the user did what it asked.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4_000_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, ValueError, TypeError):    # ValueError: a path with a NUL byte
        return False
    for ln in reversed(lines):
        if '"compact' not in ln and "isCompactSummary" not in ln:
            continue
        try:
            e = json.loads(ln)
        except (ValueError, RecursionError):
            continue
        if not isinstance(e, dict) or not (e.get("isCompactSummary") or e.get("subtype") == "compact_boundary"):
            continue
        # Both shapes Claude Code writes carry a timestamp. An undated line is no evidence: it
        # must not switch the guard off for the whole session.
        try:
            when = datetime.fromisoformat((e.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, OverflowError, OSError):
            continue
        if when >= after_ts:
            return True
    return False


def last_call(path):
    """(unix time, context tokens, model) of the last main-thread API call, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4_000_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, ValueError, TypeError):    # TypeError/ValueError: a path that is not a usable string
        return None
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except (ValueError, RecursionError):
            continue
        m = e.get("message") if isinstance(e, dict) else None     # valid JSON that is not a call
        u = m.get("usage") if isinstance(m, dict) else None
        if not isinstance(u, dict) or e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        # Claude Code records an API failure (a session limit, an overload) as an assistant line
        # whose model is <synthetic> and whose usage is all zeros. Taken as the newest call it
        # reports 0 tokens of context, so the guard stays silent exactly when the next prompt will
        # re-cache everything. Judged by the model field, not the raw line: a real call whose text
        # merely mentions "<synthetic>" was skipped, and an idle hour was invented.
        if e["message"].get("model") == "<synthetic>":
            continue
        ctx = sum(_count(u, k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                              "cache_read_input_tokens"))
        try:
            ts = datetime.fromisoformat((e.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, OverflowError, OSError):
            continue          # an undated line is not a call we can age; look further back
        model = e["message"].get("model", "")
        return ts, ctx, model if isinstance(model, str) else ""
    return None


def rewrite_usd(ctx, model):
    """Cost of re-writing ctx tokens to a 1-hour cache, and whether we knew the model's price."""
    hit = next(((k, v[2]) for k, v in _PRICES.items() if k in (model or "")), None)
    rate = hit[1] if hit else FALLBACK_WRITE_1H
    return ctx * rate / 1e6, bool(hit)


def state(sid, update=None):
    p = os.path.join(STATE, f"{sid}.json")
    if update is None:
        try:
            with open(p) as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError, RecursionError):
            return {}
    os.makedirs(STATE, exist_ok=True)
    json.dump(update, open(p, "w"))


def emit(obj):
    print(json.dumps(obj))
    sys.exit(0)


def main():
    if os.environ.get("LACUNA_GUARD_OFF"):
        return
    try:
        d = json.load(sys.stdin)
    except (ValueError, RecursionError):
        return                      # not our payload; say nothing
    if not isinstance(d, dict):
        return
    event = d.get("hook_event_name")
    # Every field is typed before use: `"transcript_path": true` once reached open(True), which is
    # file descriptor 1, and closing it killed the hook's own output.
    sid = d.get("session_id")
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid) if isinstance(sid, str) and sid else "unknown"   # it names files
    tpath = d.get("transcript_path")
    tpath = tpath if isinstance(tpath, str) else ""
    prompt = d.get("prompt")
    call = last_call(tpath)

    if event == "SessionStart":
        msg = ROUTING
        if d.get("source") in ("resume", "fork") and (call or "context_tokens" in d):
            # documented resume fields (Claude Code 2.1.251+); fall back to the transcript tail,
            # which is also the only place the model id is available
            idle = _count(d, "seconds_since_last_response")
            idle = (idle if idle is not None else (time.time() - call[0]) if call else 0) / 60
            ctx = _count(d, "context_tokens")
            ctx = ctx if ctx is not None else call[1] if call else 0
            model = call[2] if call else ""
            expired = d.get("prompt_cache_likely_expired")
            expired = expired if isinstance(expired, bool) else idle >= IDLE_MIN
            if expired and ctx >= MIN_CTX:
                est, known = rewrite_usd(ctx, model)
                given = _count(d, "estimated_cache_write_usd")
                usd = given or est
                priced = "" if known or given else " [unpriced model: rough]"
                msg = (f"Resumed after {idle / 60:.1f} h: the cache has expired, so your first prompt "
                       f"re-caches {ctx / 1000:.0f}k tokens (≈${usd:.2f} at list price{priced}). "
                       f"Cheaper: /clear and start from a written handoff. " + ROUTING)
        emit({"systemMessage": msg})

    if event != "UserPromptSubmit" or not call:
        return
    ts, ctx, model = call
    idle = (time.time() - ts) / 60
    st = state(sid)

    if idle >= IDLE_MIN and ctx >= MIN_CTX:
        if compacted_since(tpath, ts):
            return          # the context was replaced after that call; our figure is meaningless
        if time.time() - st.get("held_at", 0) < GRACE_S:
            state(sid, {**st, "held_at": 0})
            return  # second send after a hold: user chose to go ahead
        saved = os.path.join(STATE, f"{sid}.prompt.txt")
        os.makedirs(STATE, exist_ok=True)
        with open(saved, "w") as f:
            f.write(prompt if isinstance(prompt, str) else "")
        state(sid, {**st, "held_at": time.time()})
        emit({"decision": "block", "reason": (
            f"cache_guard held this prompt. The session was idle {idle / 60:.1f} h, longer than the "
            f"{IDLE_MIN:.0f}-min cache lifetime, so sending it re-caches all {ctx / 1000:.0f}k tokens "
            f"of context first (≈${rewrite_usd(ctx, model)[0]:.2f} at list price, before any work).\n"
            f"  • Cheapest: /clear, then pick up from a written handoff.\n"
            f"  • Or /compact <what to keep>: one full read, then every later turn is small.\n"
            f"  • Or send it again within 15 min to go ahead as is.\n"
            f"Your prompt is saved in {saved}")})


if __name__ == "__main__":
    # A hook that raises breaks every prompt in the session. Nothing this file does is worth
    # that, so any unexpected failure exits quietly and the session carries on unhelped.
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
