"""`lacuna doctor` — check the environment, create the data directory, say exactly what is missing.

Every check prints the command that fixes it. Nothing large is installed without being asked:
the PDF fallback and equation parsers are several GB of models and are opt-in flags.
"""
import argparse, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lacuna_config

OK, WARN, BAD = "ok  ", "note", "MISS"
CONFIG_TEMPLATE = {
    "_comment": "lacuna configuration. Delete any key to fall back to the shipped default; "
                "see docs/extending.md for the full schema. Environment variables LACUNA_* "
                "override this file.",
    "models": dict(lacuna_config.DEFAULTS["models"]),
    "effort": dict(lacuna_config.DEFAULTS["effort"]),
    "after": {"read": "", "synth": "", "verify": ""},
    "budget_warn_usd": lacuna_config.DEFAULTS["budget_warn_usd"],
    "idle_minutes": lacuna_config.DEFAULTS["idle_minutes"],
}
# One venv per parser: their dependency trees genuinely collide. Keys match prep_md.py's VENVS.
EXTRAS = {
    "pdf":       dict(pkg="pymupdf4llm", mod="pymupdf4llm", venv="venv",
                      why="the default parser, ~4 s/document", blocking=True),
    "fallback":  dict(pkg="docling", mod="docling", venv="venv-docling",
                      why="structured fallback for documents the default parser mangles, ~73 s/document",
                      blocking=False),
    "equations": dict(pkg="marker-pdf", mod="marker", venv="venv-marker",
                      why="LaTeX-fidelity parser; downloads several GB of models on first use",
                      blocking=False),
}
rows = []


def say(state, what, detail=""):
    rows.append(state)
    print(f"  [{state}] {what}" + (f"\n         {detail}" if detail else ""))


def venv_has(py, module):
    if not os.path.exists(py):
        return False
    r = subprocess.run([py, "-I", "-c", f"import {module}"], capture_output=True)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for k in EXTRAS:
        ap.add_argument(f"--install-{k}", action="store_true", help=f"create a venv and install {EXTRAS[k]['pkg']}")
    a = ap.parse_args()

    print(f"lacuna doctor\n  plugin  {lacuna_config.ROOT}\n  data    {lacuna_config.DATA}\n")

    print("environment")
    v = sys.version_info
    say(OK if v >= (3, 9) else BAD, f"python {v.major}.{v.minor}.{v.micro}",
        "" if v >= (3, 9) else "python 3.9 or newer is required")
    claude = shutil.which("claude")
    say(OK if claude else BAD, f"claude CLI {claude or 'not on PATH'}",
        "" if claude else "the literature pipeline runs headless `claude -p` agents; install Claude Code first")
    say(OK if shutil.which("tmux") else WARN, f"tmux {shutil.which('tmux') or 'not installed'}",
        "" if shutil.which("tmux") else "only needed for `lacuna split`, the two-pane results viewer")
    try:
        import rich  # noqa: F401
        say(OK, "rich (results pane renders markdown)")
    except ImportError:
        say(WARN, "rich not installed",
            "the results pane falls back to plain text, which works fine\n"
            f"         fix (optional): {sys.executable} -m pip install rich")
    meta = lacuna_config.price_meta()
    say(OK, f"price table retrieved {meta['retrieved']}" + (" (overridden locally)" if meta["overridden"] else ""),
        "list prices, not your bill — see docs/LIMITS.md")

    print("\ndata directory")
    for name in ("", "cache", "books"):
        d = os.path.join(lacuna_config.DATA, name) if name else lacuna_config.DATA
        existed = os.path.isdir(d)
        os.makedirs(d, exist_ok=True)
        say(OK, f"{d or lacuna_config.DATA}" + ("" if existed else "   (created)"))
    cfg_path = os.path.join(lacuna_config.DATA, "config.json")
    if os.path.exists(cfg_path):
        say(OK, f"{cfg_path}", "yours; left untouched")
    else:
        with open(cfg_path, "w") as f:
            json.dump(CONFIG_TEMPLATE, f, indent=2)
        say(OK, f"{cfg_path}   (written with the shipped defaults)")
    cal_src = os.path.join(lacuna_config.ROOT, "scripts", "lit", "calibration.json")
    cal_dst = os.path.join(lacuna_config.DATA, "calibration.json")
    if not os.path.exists(cal_dst) and os.path.exists(cal_src):
        shutil.copy(cal_src, cal_dst)
        say(WARN, f"{cal_dst}   (copied from the shipped calibration)",
            "estimates come from someone else's runs until you have your own; `lacuna lit estimate` says which it used")
    elif os.path.exists(cal_dst):
        say(OK, f"{cal_dst}")

    print("\nPDF parsers (only needed to read PDFs; markdown and text input work without them)")
    for key, e in EXTRAS.items():
        # the default parser's interpreter is configurable; the optional ones live beside it
        py = lacuna_config.path("python") if key == "pdf" else os.path.join(lacuna_config.DATA, e["venv"], "bin", "python")
        if getattr(a, f"install_{key}"):
            vdir = os.path.dirname(os.path.dirname(py))
            if not os.path.exists(py):
                print(f"  creating {vdir} ...")
                subprocess.run([sys.executable, "-m", "venv", vdir], check=True)
            print(f"  installing {e['pkg']} into {vdir} (this can take a while) ...")
            if subprocess.run([py, "-m", "pip", "install", "-q", e["pkg"]]).returncode:
                say(BAD, f"{e['pkg']} install failed",
                    f"run it by hand to see why: {py} -m pip install {e['pkg']}")
                continue
        if venv_has(py, e["mod"]):
            say(OK, f"{e['pkg']}", e["why"])
        else:
            say(BAD if e["blocking"] else WARN, f"{e['pkg']} not installed",
                f"{e['why']}\n         fix: lacuna doctor --install-{key}")

    missing = rows.count(BAD)
    print(f"\n{len(rows) - missing}/{len(rows)} checks clean" + (f", {missing} blocking" if missing else ""))
    if missing:
        print("Fix the MISS lines above. `lacuna spend` works regardless — it only reads transcripts.")
    else:
        print("Try:  lacuna spend --days 7      (costs nothing; reads the transcripts you already have)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
