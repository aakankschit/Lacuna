"""Shared configuration for lacuna: data locations, model routing, and prices.

Three layers, later wins:
  1. the defaults in this file
  2. $LACUNA_DATA/config.json  (written by `lacuna doctor`; edit it freely)
  3. environment variables           (LACUNA_* — for one-off overrides and CI)

Nothing here reads the network, and nothing here is specific to a research field. Model prices
change; override them in $LACUNA_DATA/pricing.json rather than waiting for a release.
"""
import json, math, os, sys

# Code lives next to this file (read-only, replaced on every plugin update).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ONE data directory, whoever is asking. It deliberately does not fall back to CLAUDE_PLUGIN_DATA:
# hooks get that variable and a shell does not, so the two would read different config files and a
# setting made in one would be silently ignored by the other.
# expanduser/expandvars because this is typed by hand and exported in shell profiles. Without it
# "~/lacuna" became a literal "./~/lacuna" next to the cwd while config.json was looked for
# elsewhere: two data directories, and `doctor` reported every step OK.
DATA = os.path.expanduser(os.path.expandvars(
    os.environ.get("LACUNA_DATA") or "~/.lacuna"))

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
        "tester":   "sonnet",         # writes tests blind to the implementation; the strong tier's edge here did not replicate
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
    """Overlay `over` onto `base`. A null or wrong-typed section is ignored rather than fatal:
    every script imports this module, so a typo in config.json must not take the whole tool down."""
    for k, v in (over or {}).items():
        if isinstance(base.get(k), dict):
            if isinstance(v, dict):
                _merge(base[k], v)
            elif v is not None:
                _error(f"config.json: {k!r} should be an object, not {type(v).__name__}. Ignoring it.")
            # v is None -> leave the defaults in place
        else:
            base[k] = v
    return base


CONFIG_ERRORS = []


def _error(msg):
    if msg not in CONFIG_ERRORS:        # prices() and price_meta() both read the same file
        CONFIG_ERRORS.append(msg)


def warn_config_errors(file=None):
    """Say, on stderr, every way config.json or pricing.json was not used as written. A command that
    spends money calls this first: a trailing comma once silently sent `synth` to the default
    (dearest) planner, skipped the user's after.synth hook and priced at shipped rates, and only
    `lacuna config` ever said so."""
    for e in CONFIG_ERRORS:
        print(f"warning: {e}", file=file or sys.stderr)


def _read_json(path, quiet=True):
    """Parse a JSON file. A malformed one is recorded rather than silently ignored."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, RecursionError) as e:   # absurdly nested input raises RecursionError, not ValueError
        _error(f"{path}: not valid JSON ({e}). Using defaults for everything in it. "
               f"JSON has no comments — a line starting with // will do this.")
    except OSError as e:
        _error(f"{path}: cannot read ({e}).")
    return {}


def _validate(cfg):
    """Every string setting is a string without a NUL byte, or the default is used and the reason
    recorded. config.json is hand-edited: `"cache": true` made a directory named ./True, and a
    number for a model crashed `estimate`, `read` and `synth`. Empty means "use the default"."""
    for section, body in DEFAULTS.items():
        if not isinstance(body, dict):
            continue
        got = cfg.get(section)
        if not isinstance(got, dict):
            cfg[section] = got = json.loads(json.dumps(body))
        for key, default in body.items():
            v = got.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                got[key] = default
                if SOURCES.get((section, key)) != "default":
                    SOURCES[(section, key)] = "default"
            elif not isinstance(v, str) or "\0" in v:
                _error(f"{section}.{key} should be text, not {v!r}. Using the default.")
                got[key] = default
                SOURCES[(section, key)] = "default"


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
    if not isinstance(from_file, dict):
        _error("config.json: the top level should be an object. Ignoring the file.")
        from_file = {}
    for section, body in from_file.items():
        if isinstance(body, dict):
            for key in body:
                SOURCES[(section, key)] = "config.json"
    _merge(cfg, from_file)
    for env, (section, key) in ENV_KEYS.items():
        v = os.environ.get(f"LACUNA_{env}")
        if v and v.strip():
            cfg[section][key] = v
            SOURCES[(section, key)] = f"$LACUNA_{env}"
    _validate(cfg)
    # Paths are written by hand in config.json, so "~/books" and "$HOME/books" must work.
    # Without this a tilde becomes a literal directory named "~" and the error is baffling.
    # They are made absolute too: they are handed to agents, which run in another directory.
    cfg["paths"] = {k: os.path.abspath(os.path.expanduser(os.path.expandvars(v))) if isinstance(v, str) and v else v
                    for k, v in (cfg.get("paths") or {}).items()}
    return cfg


CONFIG = load()


def model(role):
    """The model id for a pipeline role: reader, digester, planner, verifier, coder, tester."""
    return CONFIG["models"].get(role) or DEFAULTS["models"][role]


def effort(role):
    return CONFIG["effort"].get(role) or "medium"


def path(name):
    return CONFIG["paths"].get(name) or DEFAULTS["paths"][name]


# `claude --model` accepts bare aliases, and so does config.json, but a price table is keyed by
# concrete ids — so "sonnet" matched nothing and every cost involving an alias silently came out
# as zero or "unpriced". Map each alias to the version it currently resolves to.
ALIASES = {"haiku": "haiku-5-5", "sonnet": "sonnet-5-5", "opus": "opus-5-5", "fable": "fable-5-1"}


def resolve(model):
    """A concrete model id for pricing. Passes anything already concrete straight through."""
    return ALIASES.get((model or "").strip(), model)


def _price_key(k):
    """A price key as a user writes it: "claude-opus-5-5" and "opus-5-5" name the same model, and
    matching is by substring of the resolved id ("opus-5-5" for the alias "opus")."""
    return k[len("claude-"):] if k.startswith("claude-") else k


def canonical(model):
    """One spelling for a model, whether it was named by alias, by id or by a price key:
    "sonnet", "sonnet-5-5" and "claude-sonnet-5-5" are all "sonnet-5-5"."""
    return _price_key(resolve(model) or "")


def _price_table(doc, src=None):
    """The {model: 5-tuple} map out of a pricing file, ignoring _comment-style metadata. With `src`
    (the file's path), what it skips is recorded: a price that is silently not used is a bill that
    does not match the user's own table."""
    out = {}
    table = (doc.get("models") or doc) if isinstance(doc, dict) else None
    if not isinstance(table, dict):       # hand-edited: a list here crashed every price lookup
        if src and doc not in ({}, None):
            _error(f"{src}: should be an object of model -> five prices. Using the shipped prices.")
        return out
    for k, v in table.items():
        if k.startswith("_"):
            continue
        try:
            t = tuple(float(x) for x in v) if isinstance(v, (list, tuple)) else None   # "12345" is not five prices
        except (TypeError, ValueError):
            t = None
        if t is not None and len(t) == 5 and all(math.isfinite(x) and x >= 0 for x in t):   # NaN/inf priced a call at $nan
            out[_price_key(k)] = t
        elif src:
            _error(f"{src}: the price for {k!r} should be five non-negative numbers "
                   f"(input, 5m write, 1h write, cache read, output per MTok). Ignoring it.")
    return out


def prices():
    """Prices, longest key first so "opus-5-5" is never matched by "opus-5".

    Shipped table, then the user's override in the data directory, then a built-in fallback.
    """
    shipped, mine = os.path.join(ROOT, "scripts", "pricing.json"), os.path.join(DATA, "pricing.json")
    p = _price_table(_read_json(shipped), shipped)
    p.update(_price_table(_read_json(mine), mine))
    if not p:
        p = dict(PRICE_FALLBACK)
    return dict(sorted(p.items(), key=lambda kv: -len(kv[0])))


def price_meta():
    """When the shipped table was retrieved, for honest reporting.

    `overridden` means the user's file actually contributed prices — not merely that a file
    exists. A malformed override that yields nothing must not be reported as in effect, or the
    user is told their edit took when the shipped table is what is being used.
    """
    d, u = (x if isinstance(x, dict) else {} for x in (_read_json(os.path.join(ROOT, "scripts", "pricing.json")),
                                                       _read_json(os.path.join(DATA, "pricing.json"))))
    return {"retrieved": (u.get("_retrieved") or d.get("_retrieved") or "unknown"),
            "overridden": bool(_price_table(u))}


prices()        # reads both price files, so a malformed one is in CONFIG_ERRORS for whoever warns


if __name__ == "__main__":
    cfg = load()
    prices()
    if "--json" in sys.argv:
        # strict JSON: NaN and Infinity (which json.load accepts from config.json) are not JSON
        shown = {**cfg, "data_dir": DATA, "prices": {k: list(v) for k, v in prices().items()},
                 "errors": list(CONFIG_ERRORS)}
        if isinstance(shown.get("budget_warn_usd"), float) and not math.isfinite(shown["budget_warn_usd"]):
            shown["budget_warn_usd"] = None
        print(json.dumps(shown, indent=2, allow_nan=False))
    else:
        def src(section, key):
            s = SOURCES.get((section, key), "default")
            return "" if s == "default" else f" [{s}]"

        def _fmt_usd(v):   # config.json is hand-edited; a string here must not crash this report
            return f"${v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) \
                else f"{v!r} (not a number, so the budget check is off)"

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
        print(f"budget     warn above {_fmt_usd(cfg.get('budget_warn_usd'))}   idle {cfg.get('idle_minutes')} min")
        print("\nValues marked [config.json] or [$LACUNA_*] override the built-in default.")
