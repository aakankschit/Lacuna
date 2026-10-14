"""Pre-flight cost estimate for a literature run, from measured calibration (calibration.json).

usage: python3 lit_cost.py <file.md|file.pdf|dir> [...] [--per-agent K] [--model M] [--effort E]
Counts tokens locally (no LLM), then prints a cost/time table for every measured model x effort, or one row.
"""
import argparse, json, os, shutil, subprocess, sys

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
_user_cal = os.path.join(lacuna_config.DATA, "calibration.json")
CAL_PATH = _user_cal if os.path.exists(_user_cal) else os.path.join(HERE, "calibration.json")
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


def main():
    ap = argparse.ArgumentParser(
        prog="lacuna lit estimate", description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="documents (.md/.txt/.pdf) or directories of them")
    # 1 to match `lit read`, which runs one agent per document.
    ap.add_argument("--per-agent", type=int, default=1, help="documents per reader agent (default 1)")
    ap.add_argument("--model", help="show only this calibrated model")
    ap.add_argument("--effort", help="show only this effort level")
    a = ap.parse_args()
    opts = {k: v for k, v in (("model", a.model), ("effort", a.effort)) if v}

    missing = [p for p in a.paths if not os.path.exists(p)]
    if missing:
        die2(f"ERROR: no such path: {', '.join(missing)}")
    files = [os.path.join(d, f) for d in a.paths if os.path.isdir(d) for f in sorted(os.listdir(d))
             if f.endswith((".md", ".txt", ".pdf"))] + [p for p in a.paths if not os.path.isdir(p)]
    if not files:
        die2("ERROR: no .md, .txt or .pdf documents found in those paths.")
    toks = [tokens(f) for f in files]
    # A PDF that yields no text is broken, encrypted or a scan; pricing it at $0.00 and exiting 0
    # tells the user the run is free. A small .md or .txt is simply small, and is left alone.
    empty = [f for f, t in zip(files, toks) if f.endswith(".pdf") and t < 20]
    if empty:
        die2("ERROR: no extractable text in: " + ", ".join(os.path.basename(f) for f in empty)
             + "\nAn estimate of $0.00 would be meaningless — the file is corrupt, encrypted, or a "
               "scan with no text layer. `lacuna lit read` would fail on it too.")
    k = max(1, a.per_agent)
    total = sum(toks)
    groups = [toks[i:i + k] for i in range(0, len(toks), k)]
    biggest = max((sum(g) for g in groups), default=0) + STARTUP
    waves = -(-len(groups) // POOL)
    print(f"{len(files)} documents, ~{total:,.0f} tokens; {len(groups)} reader(s) at {k} per reader"
          + (f", run {POOL} at a time in {waves} waves" if waves > 1 else "")
          + f" (largest reader context ~{biggest:,.0f} tokens)")
    if biggest > HAIKU_TIER:
        print(f"  ! a reader exceeds {HAIKU_TIER:,} tokens: Haiku's long-context price (5x) applies; lower --per-agent")
    known = [m for m in CAL if isinstance(CAL[m], dict)]
    if "model" in opts and opts["model"] not in known:
        die2(f"ERROR: no calibration for model {opts['model']!r}. Calibrated models: {', '.join(known)}.\n"
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
            # Readers run in a pool of POOL at a time, so N groups take ceil(N / POOL) waves.
            waves = -(-len(groups) // POOL)
            mins = c["minutes_per_paper"] * max(len(g) for g in groups) * waves
            cells.append(f"${usd:7.2f} {mins:4.0f}m")
        print(f"{m:15s} " + " ".join(cells))
    new_ctx = IDEATION["notes_tok"] * len(files) + IDEATION["context_tok"]
    planner = lacuna_config.model("planner")
    pr, concrete = lacuna_config.prices(), lacuna_config.resolve(planner)
    _, _, w1h, _, out_price = next((v for k, v in pr.items() if k in concrete), (0, 0, 0, 0, 0))
    if not out_price:
        print(f"\n! no price for the planner model {planner!r} in the price table; cannot estimate the "
              f"synthesis step. Add it to {os.path.join(lacuna_config.DATA, 'pricing.json')}.")
    else:
        ide = new_ctx * w1h / 1e6 + IDEATION["out_tok"] * out_price / 1e6
        print(f"\nsynthesis over {len(files)} notes with {planner}: ~${ide:.2f} "
              f"(+{new_ctx / 1000:.0f}k context that every later turn in that session re-reads)")
    warn = lacuna_config.CONFIG.get("budget_warn_usd") or 0
    over = [(m, e) for m in models for e in efforts if CAL.get(m, {}).get(e)
            and total / 1000 * CAL[m][e]["usd_per_ktok"] > warn] if warn else []
    if over:
        cheap = min(over, key=lambda me: CAL[me[0]][me[1]]["usd_per_ktok"])
        print(f"\n!  {len(over)} of these combinations cost more than budget_warn_usd (${warn:.2f}). "
              f"Show the estimate and get approval before running one. Cheapest over the line: "
              f"{cheap[0]} at {cheap[1]} effort.")
    print(f"\nCalibration: {CAL_SOURCE} ({CAL_PATH}).")
    print(f"Recalibrate from your own runs: {lacuna_config.path('ledger')}. Prices retrieved "
          f"{lacuna_config.price_meta()['retrieved']}; list price, not your bill.")


if __name__ == "__main__":
    main()
