"""Pre-flight cost estimate for a literature run, from measured calibration (calibration.json).

usage: python3 lit_cost.py <file.md|file.pdf|dir> [...] [--per-agent K] [--model M] [--effort E]
Counts tokens locally (no LLM), then prints a cost/time table for every measured model x effort, or one row.
"""
import json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import lacuna_config

# Your own calibration in the data directory wins; the shipped one is someone else's runs.
_user_cal = os.path.join(lacuna_config.DATA, "calibration.json")
CAL_PATH = _user_cal if os.path.exists(_user_cal) else os.path.join(HERE, "calibration.json")
with open(CAL_PATH) as f:
    CAL = json.load(f)
CAL_SOURCE = ("upstream — someone else's runs on their hardware, not yours"
              if CAL.get("_source") == "upstream" else "yours")
STARTUP = 3_700           # measured: slim reader agent's first request
HAIKU_TIER = 100_000      # the cheap tier's input price rises 5x above this prompt size
IDEATION = {"notes_tok": 1_500, "context_tok": 6_000, "out_tok": 8_000}  # notes + project context, output


def tokens(path):
    if path.endswith(".pdf"):
        if not shutil.which("pdftotext"):
            sys.exit("ERROR: estimating a PDF needs `pdftotext` (poppler) on PATH. Estimate the parsed "
                     ".md instead, or install poppler-utils / `brew install poppler`.")
        text = subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True).stdout
    else:
        text = open(path, errors="ignore").read()
    ratio = 2.1 if sum(ch in "∑∫αβγσθλμ∂≤≥" for ch in text) > len(text) / 2000 else 2.9  # math-heavy text
    return len(text) / ratio


def main():
    args, opts = [], {}
    it = iter(sys.argv[1:])
    for a in it:
        if a.startswith("--"):
            opts[a[2:]] = next(it)
        else:
            args.append(a)
    files = [os.path.join(a, f) for a in args if os.path.isdir(a) for f in sorted(os.listdir(a))
             if f.endswith((".md", ".txt", ".pdf"))] + [a for a in args if not os.path.isdir(a)]
    toks = [tokens(f) for f in files]
    k = int(opts.get("per-agent", 4))
    total = sum(toks)
    groups = [toks[i:i + k] for i in range(0, len(toks), k)]
    biggest = max((sum(g) for g in groups), default=0) + STARTUP
    print(f"{len(files)} documents, ~{total:,.0f} tokens; {len(groups)} reader(s) at {k} per reader "
          f"(largest reader context ~{biggest:,.0f} tokens)")
    if biggest > HAIKU_TIER:
        print(f"  ! a reader exceeds {HAIKU_TIER:,} tokens: Haiku's long-context price (5x) applies; lower --per-agent")
    known = [m for m in CAL if isinstance(CAL[m], dict)]
    if "model" in opts and opts["model"] not in known:
        sys.exit(f"ERROR: no calibration for model {opts['model']!r}. Calibrated models: {', '.join(known)}.\n"
                 f"Add a row to {CAL_PATH} from your own runs in {lacuna_config.path('ledger')}, "
                 f"or omit --model to see every calibrated row.")
    models = [opts["model"]] if "model" in opts else known
    efforts = [opts["effort"]] if "effort" in opts else ["low", "medium", "high", "xhigh", "max"]
    print(f"{'model':15s} " + " ".join(f"{e:>14s}" for e in efforts))
    for m in models:
        cells = []
        for e in efforts:
            c = CAL.get(m, {}).get(e)
            if not c:
                cells.append(f"{'-':>14s}"); continue
            usd = total / 1000 * c["usd_per_ktok"] * (5 if m == "haiku" and biggest > HAIKU_TIER else 1)
            mins = c["minutes_per_paper"] * max(len(g) for g in groups)  # readers run in parallel
            cells.append(f"${usd:7.2f} {mins:4.0f}m")
        print(f"{m:15s} " + " ".join(cells))
    new_ctx = IDEATION["notes_tok"] * len(files) + IDEATION["context_tok"]
    planner = lacuna_config.model("planner")
    pr = lacuna_config.prices()
    _, _, w1h, _, out_price = next((v for k, v in pr.items() if k in planner), (0, 0, 0, 0, 0))
    if not out_price:
        print(f"\n! no price for the planner model {planner!r} in the price table; cannot estimate the "
              f"synthesis step. Add it to {os.path.join(lacuna_config.DATA, 'pricing.json')}.")
    else:
        ide = new_ctx * w1h / 1e6 + IDEATION["out_tok"] * out_price / 1e6
        print(f"\nsynthesis over {len(files)} notes with {planner}: ~${ide:.2f} "
              f"(+{new_ctx / 1000:.0f}k context that every later turn in that session re-reads)")
    print(f"\nCalibration: {CAL_SOURCE} ({CAL_PATH}).")
    print(f"Recalibrate from your own runs: {lacuna_config.path('ledger')}. Prices retrieved "
          f"{lacuna_config.price_meta()['retrieved']}; list price, not your bill.")


if __name__ == "__main__":
    main()
