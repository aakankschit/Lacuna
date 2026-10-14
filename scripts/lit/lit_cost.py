"""Pre-flight cost estimate for a literature run, from measured calibration (calibration.json).

usage: python3 lit_cost.py <file.md|file.pdf|dir> [...] [--per-agent K] [--model M] [--effort E] [--json]
Counts tokens locally (no LLM), then prints a cost/time table for every measured model x effort, or one row.
"""
import argparse, json, math, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import lacuna_config


def die2(msg):
    """Bad input."""
    print(msg, file=sys.stderr)
    sys.exit(2)


def die3(msg):
    """A dependency or a parse is missing."""
    print(msg, file=sys.stderr)
    sys.exit(3)


# Your own calibration in the data directory wins; the shipped one is someone else's runs.
# A hand-edited file that does not parse is reported and the shipped one used, as lacuna_config does
# for config.json and pricing.json; it once ended `lit estimate` with a traceback.
_user_cal = os.path.join(lacuna_config.DATA, "calibration.json")
CAL_PATH, CAL_WARNING = os.path.join(HERE, "calibration.json"), None
if os.path.exists(_user_cal):
    try:
        with open(_user_cal) as f:
            _cal = json.load(f)
        if not isinstance(_cal, dict):
            raise ValueError("the top level is not an object")
        CAL_PATH = _user_cal
    except (OSError, ValueError) as e:
        CAL_WARNING = f"your calibration {_user_cal} is unusable ({e}); using the shipped one instead"
with open(CAL_PATH) as f:
    CAL = json.load(f)
CAL_SOURCE = ("upstream — someone else's runs on their hardware, not yours"
              if CAL.get("_source") == "upstream" else "yours")
POOL = 8                  # lit_pipeline runs at most this many readers concurrently
STARTUP = 3_700           # measured: slim reader agent's first request
HAIKU_TIER = 100_000      # the cheap tier's input price rises 5x above this prompt size
IDEATION = {"notes_tok": 1_500, "context_tok": 6_000, "out_tok": 8_000}  # notes + project context, output


def pdf_text(path):
    """PDF text for counting. Prefer the parser venv's pymupdf, which `doctor --install-pdf`
    already provides, and fall back to poppler's pdftotext if that is what the machine has."""
    venv = lacuna_config.path("python")
    if os.path.exists(venv):
        r = subprocess.run([venv, "-I", "-c",
                            "import sys,pymupdf;print(''.join(p.get_text() for p in pymupdf.open(sys.argv[1])))",
                            path], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        die2(f"ERROR: cannot read {os.path.basename(path)}: corrupt, encrypted, or not a PDF. `lacuna lit read` would fail on it too.")
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True).stdout
    die3("ERROR: estimating a PDF needs either the parser venv (`lacuna doctor --install-pdf`) "
             "or `pdftotext` (poppler) on PATH. Estimating a parsed .md needs neither.")


def tokens(path):
    if path.endswith(".pdf"):
        text = pdf_text(path)
    else:
        text = open(path, errors="ignore").read()
    ratio = 2.1 if sum(ch in "∑∫αβγσθλμ∂≤≥" for ch in text) > len(text) / 2000 else 2.9  # math-heavy text
    return len(text) / ratio


EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def effective_budget(raw):
    """(threshold or None, warning or None). Only a positive int/float (not bool) enables the check."""
    if raw is None:
        return None, None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(raw):  # json accepts Infinity and NaN; neither is a threshold
            return None, f"budget_warn_usd is {raw!r}, not a usable number; the budget check is disabled."
        return (raw, None) if raw > 0 else (None, None)
    return None, (f"budget_warn_usd is {raw!r} ({type(raw).__name__}), not a number; "
                  f"the budget check is disabled.")


def estimate(files, per_agent, model=None, effort=None, cal=None, config=None):
    """Build the estimate as a plain dict. Reads `cal` and `config`; mutates neither.

    Returns (report, extra); `extra` carries unrounded figures for the human table. Raises SystemExit(2)
    on an unknown `model`."""
    cal = CAL if cal is None else cal
    config = lacuna_config.CONFIG if config is None else config
    toks = [tokens(f) for f in files]
    k = max(1, per_agent)
    total = sum(toks)
    groups = [toks[i:i + k] for i in range(0, len(toks), k)]
    biggest = max((sum(g) for g in groups), default=0) + STARTUP
    surcharge = biggest > HAIKU_TIER
    known = [m for m in cal if isinstance(cal[m], dict)]
    if model and model not in known:
        die2(f"ERROR: no calibration for model {model!r}. Calibrated models: {', '.join(known)}.\n"
             f"Add a row to {CAL_PATH} from your own runs in {lacuna_config.path('ledger')}, "
             f"or omit --model to see every calibrated row.")
    models = [model] if model else known
    efforts = [effort] if effort else list(EFFORTS)
    warn, budget_msg = effective_budget(config.get("budget_warn_usd"))
    warnings = [CAL_WARNING] if CAL_WARNING and cal is CAL else []
    if surcharge:
        warnings.append(f"a reader exceeds {HAIKU_TIER:,} tokens: Haiku's long-context price (5x) "
                        f"applies; lower --per-agent")
    if budget_msg:
        warnings.append(budget_msg)
    biggest_n = max(len(g) for g in groups)
    waves = -(-len(groups) // POOL)      # readers run POOL at a time, so N groups take N/POOL waves
    ests = []
    for m in models:
        for e in efforts:
            c = cal.get(m, {}).get(e)
            if not c:
                continue
            # float() because a hand-written calibration may hold integers, and the contract says float
            usd = float(total / 1000 * c["usd_per_ktok"] * (5 if m == "haiku" and surcharge else 1))
            ests.append({"model": m, "effort": e, "usd": usd,
                         "minutes": float(c["minutes_per_paper"] * biggest_n * waves),
                         "over_budget": warn is not None and usd > warn})
    over = [x for x in ests if x["over_budget"]]
    cheap = min(over, key=lambda x: x["usd"]) if over else None  # min() keeps the first of equals
    new_ctx = IDEATION["notes_tok"] * len(files) + IDEATION["context_tok"]
    planner = lacuna_config.model("planner")
    concrete = lacuna_config.resolve(planner)
    _, _, w1h, _, out_price = next((v for kk, v in lacuna_config.prices().items() if kk in concrete),
                                   (0, 0, 0, 0, 0))
    synth = None
    if out_price:
        synth = {"model": planner,
                 "usd": new_ctx * w1h / 1e6 + IDEATION["out_tok"] * out_price / 1e6,
                 "context_tokens": new_ctx}
    else:
        warnings.append(f"no price for the planner model {planner!r} in the price table; cannot "
                        f"estimate the synthesis step.")
    src = "upstream" if cal.get("_source") == "upstream" else "yours"
    report = {
        "schema": 1, "documents": len(files), "total_tokens": round(total), "per_agent": k,
        "readers": len(groups), "largest_reader_tokens": round(biggest),
        "long_context_surcharge": surcharge,
        "calibration": {"source": src, "path": os.path.abspath(CAL_PATH), "measured": cal.get("_measured")},
        "budget_warn_usd": None if warn is None else float(warn),
        "estimates": ests, "any_over_budget": bool(over),
        "cheapest_over_budget": (None if cheap is None else
                                 {"model": cheap["model"], "effort": cheap["effort"], "usd": cheap["usd"]}),
        "synthesis": synth, "prices_retrieved": lacuna_config.price_meta()["retrieved"],
        "warnings": warnings,
    }
    extra = {"total": total, "biggest": biggest, "models": models, "efforts": efforts, "waves": waves,
             "over": over, "cheap": cheap, "budget_msg": budget_msg, "new_ctx": new_ctx}
    return report, extra


def main():
    ap = argparse.ArgumentParser(
        prog="lacuna lit estimate", description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="documents (.md/.txt/.pdf) or directories of them")
    # 1 to match `lit read`, which runs one agent per document.
    ap.add_argument("--per-agent", type=int, default=1, help="documents per reader agent (default 1)")
    ap.add_argument("--model", help="show only this calibrated model")
    ap.add_argument("--effort", choices=EFFORTS, help="show only this effort level")
    ap.add_argument("--json", action="store_true", help="print one machine-readable JSON object")
    # intermixed, so a flag between two paths (`a.md --json b.md`) is a flag, not an extra argument
    a = ap.parse_intermixed_args()

    missing = [p for p in a.paths if not os.path.exists(p)]
    if missing:
        die2(f"ERROR: no such path: {', '.join(missing)}")
    files = [os.path.join(d, f) for d in a.paths if os.path.isdir(d) for f in sorted(os.listdir(d))
             if f.endswith((".md", ".txt", ".pdf"))] + [p for p in a.paths if not os.path.isdir(p)]
    if not files:
        die2("ERROR: no .md, .txt or .pdf documents found in those paths.")
    # A PDF yielding no text is broken, encrypted or a scan. Pricing it at $0.00 and exiting 0
    # tells the user the run is free. A small .md or .txt is simply small, and is left alone.
    unreadable = [f for f in files if f.endswith(".pdf") and tokens(f) < 20]
    if unreadable:
        die2("ERROR: no extractable text in: "
             + ", ".join(os.path.basename(f) for f in unreadable)
             + "\nAn estimate of $0.00 would be meaningless — the file is corrupt, encrypted, or a "
               "scan with no text layer. `lacuna lit read` would fail on it too.")
    r, x = estimate(files, a.per_agent, a.model or None, a.effort or None)  # nothing printed yet
    if CAL_WARNING:
        print(f"warning: {CAL_WARNING}", file=sys.stderr)
    if a.json:
        print(json.dumps(r, allow_nan=False))
        if x["budget_msg"]:
            print(f"warning: {x['budget_msg']}", file=sys.stderr)
        return
    k, total, biggest = r["per_agent"], x["total"], x["biggest"]
    print(f"{r['documents']} documents, ~{total:,.0f} tokens; {r['readers']} reader(s) at {k} per reader "
          f"(largest reader context ~{biggest:,.0f} tokens)")
    if r["long_context_surcharge"]:
        print(f"  ! a reader exceeds {HAIKU_TIER:,} tokens: Haiku's long-context price (5x) applies; lower --per-agent")
    by = {(e["model"], e["effort"]): e for e in r["estimates"]}
    print(f"{'model':15s} " + " ".join(f"{e:>14s}" for e in x["efforts"]))
    for m in x["models"]:
        cells = []
        for e in x["efforts"]:
            c = by.get((m, e))
            if not c:
                cells.append(f"{'-':>14s}"); continue
            cells.append(f"${c['usd']:7.2f} {c['minutes']:4.0f}m")
        print(f"{m:15s} " + " ".join(cells))
    s = r["synthesis"]
    if s is None:
        print(f"\n! no price for the planner model {lacuna_config.model('planner')!r} in the price table; "
              f"cannot estimate the synthesis step. Add it to {os.path.join(lacuna_config.DATA, 'pricing.json')}.")
    else:
        print(f"\nsynthesis over {r['documents']} notes with {s['model']}: ~${s['usd']:.2f} "
              f"(+{s['context_tokens'] / 1000:.0f}k context that every later turn in that session re-reads)")
    if x["budget_msg"]:
        print(f"warning: {x['budget_msg']}", file=sys.stderr)
    if x["over"]:
        ch = x["cheap"]
        print(f"\n!  {len(x['over'])} of these combinations cost more than budget_warn_usd "
              f"(${r['budget_warn_usd']:.2f}). Show the estimate and get approval before running one. "
              f"Cheapest over the line: {ch['model']} at {ch['effort']} effort.")
    print(f"\nCalibration: {CAL_SOURCE} ({CAL_PATH}).")
    print(f"Recalibrate from your own runs: {lacuna_config.path('ledger')}. Prices retrieved "
          f"{r['prices_retrieved']}; list price, not your bill.")


if __name__ == "__main__":
    main()
