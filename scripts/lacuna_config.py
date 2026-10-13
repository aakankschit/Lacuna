"""Shared configuration for lacuna: data locations, model routing, and prices.

Three layers, later wins:
  1. the defaults in this file
  2. $LACUNA_DATA/config.json  (written by `lacuna doctor`; edit it freely)
  3. environment variables           (LACUNA_* — for one-off overrides and CI)

Nothing here reads the network, and nothing here is specific to a research field. Model prices
change; override them in $LACUNA_DATA/pricing.json rather than waiting for a release.
"""
import json, os

# Code lives next to this file (read-only, replaced on every plugin update).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ONE data directory, whoever is asking. It deliberately does not fall back to CLAUDE_PLUGIN_DATA:
# hooks get that variable and a shell does not, so the two would read different config files and a
# setting made in one would be silently ignored by the other.
DATA = os.environ.get("LACUNA_DATA") or os.path.expanduser("~/.lacuna")

# Claude Code's own transcript directory. Not configurable by us — this is where it writes.
TRANSCRIPTS = os.path.expanduser("~/.claude/projects")

DEFAULTS = {
    # --- model routing. Every value is passed verbatim to `claude --model`, so an alias
    # ("haiku", "sonnet", "opus") or a pinned id ("claude-opus-5") both work. ---
    "models": {
        "reader":   "haiku",          # one agent per document. Recall is model-independent; see docs/benchmarks.
        "digester": "sonnet",         # consolidates notes into a digest
        "planner":  "claude-opus-5",  # turns notes into ranked proposals. The one place a strong model pays.
        "verifier": "haiku",          # grep-only fact check. Cheap, and catches the dominant error class.
        "coder":    "sonnet",         # implements a pinned contract
        "tester":   "claude-opus-5",  # writes tests blind to the implementation
    },
    # Effort is a cost multiplier, not a quality lever (up to 43x for no measured gain).
    # Raise it only for the verifier, where it is cheap and does help.
    "effort": {"reader": "medium", "digester": "medium", "planner": "medium",
               "verifier": "high", "coder": "medium", "tester": "medium"},
    "paths": {
        "python":  os.path.join(DATA, "venv", "bin", "python"),  # the parser venv
        "books":   os.path.join(DATA, "books"),
        "cache":   os.path.join(DATA, "cache"),
        "results": os.path.expanduser("~/.claude/results"),
        "ledger":  os.path.join(DATA, "cost-ledger.jsonl"),
    },
    # Extension seam. Each value is a shell command; the stage's output path is appended as $1.
    # A project-specific plugin sets these to file results wherever it keeps records.
    "after": {"read": "", "synth": "", "verify": ""},
    # Warn before spending more than this on one pipeline run, in USD.
    "budget_warn_usd": 2.0,
    # Session hygiene: idle minutes after which the prompt cache is assumed cold.
    "idle_minutes": 60,
}

# Prices live in scripts/pricing.json (dated, with its source URL) so they can be corrected
# without a code change. $LACUNA_DATA/pricing.json layers over it and survives updates.
# This inline table is the last-resort fallback if both files are unreadable.
PRICE_FALLBACK = {
    "opus-5-5":   (4.00, 5.00, 8.00, 0.20, 20.00),
    "sonnet-5-5": (2.00, 2.50, 4.00, 0.10, 10.00),
    "haiku-5-5":  (0.10, 0.125, 0.20, 0.01, 0.50),
}

ENV_KEYS = {  # LACUNA_<KEY> -> where it lands
    "READER": ("models", "reader"), "DIGESTER": ("models", "digester"),
    "PLANNER": ("models", "planner"), "VERIFIER": ("models", "verifier"),
    "CODER": ("models", "coder"), "TESTER": ("models", "tester"),
    "PYTHON": ("paths", "python"), "BOOKS": ("paths", "books"),
    "CACHE": ("paths", "cache"), "RESULTS": ("paths", "results"),
    "LEDGER": ("paths", "ledger"),
}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


CONFIG_ERRORS = []


def _read_json(path, quiet=True):
    """Parse a JSON file. A malformed one is recorded rather than silently ignored."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except ValueError as e:
        CONFIG_ERRORS.append(f"{path}: not valid JSON ({e}). Using defaults for everything in it. "
                             f"JSON has no comments — a line starting with // will do this.")
    except OSError as e:
        CONFIG_ERRORS.append(f"{path}: cannot read ({e}).")
    return {}


SOURCES = {}   # (section, key) -> "default" | "config.json" | "$LACUNA_<NAME>"


def load():
    """The effective configuration, as a plain dict. Fills SOURCES with each value's origin."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    SOURCES.clear()
    del CONFIG_ERRORS[:]        # load() may run twice (import, then __main__); do not duplicate
    for section, body in cfg.items():
        if isinstance(body, dict):
            for key in body:
                SOURCES[(section, key)] = "default"
    from_file = _read_json(os.path.join(DATA, "config.json"))
    for section, body in (from_file or {}).items():
        if isinstance(body, dict):
            for key in body:
                SOURCES[(section, key)] = "config.json"
    _merge(cfg, from_file)
    for env, (section, key) in ENV_KEYS.items():
        v = os.environ.get(f"LACUNA_{env}")
        if v:
            cfg[section][key] = v
            SOURCES[(section, key)] = f"$LACUNA_{env}"
    # Paths are written by hand in config.json, so "~/books" and "$HOME/books" must work.
    # Without this a tilde becomes a literal directory named "~" and the error is baffling.
    cfg["paths"] = {k: os.path.expanduser(os.path.expandvars(v)) if isinstance(v, str) else v
                    for k, v in cfg["paths"].items()}
    return cfg


CONFIG = load()


def model(role):
    """The model id for a pipeline role: reader, digester, planner, verifier, coder, tester."""
    return CONFIG["models"].get(role) or DEFAULTS["models"][role]


def effort(role):
    return CONFIG["effort"].get(role, "medium")


def path(name):
    return CONFIG["paths"].get(name) or DEFAULTS["paths"][name]


def _price_table(doc):
    """The {model: 5-tuple} map out of a pricing file, ignoring _comment-style metadata."""
    out = {}
    for k, v in (doc.get("models") or doc).items():
        if k.startswith("_"):
            continue
        try:
            t = tuple(float(x) for x in v)
            if len(t) == 5:
                out[k] = t
        except (TypeError, ValueError):
            continue
    return out


def prices():
    """Prices, longest key first so "opus-5-5" is never matched by "opus-5".

    Shipped table, then the user's override in the data directory, then a built-in fallback.
    """
    p = _price_table(_read_json(os.path.join(ROOT, "scripts", "pricing.json")))
    p.update(_price_table(_read_json(os.path.join(DATA, "pricing.json"))))
    if not p:
        p = dict(PRICE_FALLBACK)
    return dict(sorted(p.items(), key=lambda kv: -len(kv[0])))


def price_meta():
    """When the shipped table was retrieved, for honest reporting."""
    d = _read_json(os.path.join(ROOT, "scripts", "pricing.json"))
    u = _read_json(os.path.join(DATA, "pricing.json"))
    return {"retrieved": (u.get("_retrieved") or d.get("_retrieved") or "unknown"),
            "overridden": bool(u)}


if __name__ == "__main__":
    import sys
    cfg = load()
    if "--json" in sys.argv:
        print(json.dumps({**cfg, "data_dir": DATA, "prices": {k: list(v) for k, v in prices().items()}}, indent=2))
    else:
        def src(section, key):
            s = SOURCES.get((section, key), "default")
            return "" if s == "default" else f" [{s}]"

        for e in CONFIG_ERRORS:
            print(f"!  {e}")
        print(f"data dir   {DATA}" + ("" if os.path.isdir(DATA) else "   (missing — run `lacuna doctor`)"))
        for name in ("python", "books", "cache", "results", "ledger"):
            label = {"python": "parser py"}.get(name, name)
            missing = ("   (missing — run `lacuna doctor --install-pdf`)"
                       if name == "python" and not os.path.exists(path(name)) else "")
            print(f"{label:10s} {path(name)}{src('paths', name)}{missing}")
        print("roles      " + "  ".join(f"{k}={v}{src('models', k)}" for k, v in cfg["models"].items()))
        print("effort     " + "  ".join(f"{k}={v}{src('effort', k)}" for k, v in cfg["effort"].items()))
        hooks = {k: v for k, v in cfg.get("after", {}).items() if v}
        print("after      " + ("  ".join(f"{k}={v}" for k, v in hooks.items()) if hooks else "(none)"))
        print(f"budget     warn above ${cfg.get('budget_warn_usd', 0):.2f}   idle {cfg.get('idle_minutes')} min")
        print("\nValues marked [config.json] or [$LACUNA_*] override the built-in default.")
