"""`lacuna doctor` — check the environment, create the data directory, say exactly what is missing.

Every check prints the command that fixes it. Nothing large is installed without being asked:
the PDF fallback and equation parsers are several GB of models and are opt-in flags.
"""
import argparse, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lacuna_config

OK, WARN, BAD = "ok  ", "note", "MISS"
# Intentionally minimal: anything written here overrides the shipped default forever, including
# defaults that later releases improve. `lacuna config` prints the full effective settings, and
# docs/extending.md documents every key.
CONFIG_TEMPLATE = {
    "_comment": "Lacuna settings. Add only what you want to change — anything set here overrides "
                "the shipped default permanently. `lacuna config` shows what is in effect and "
                "where each value came from. Full schema: docs/extending.md. Strict JSON: no "
                "comments, or the whole file is ignored.",
    "models": {},
}
# One venv per parser: their dependency trees genuinely collide. Keys match prep_md.py's VENVS.
EXTRAS = {
    "pdf":       dict(pkg="pymupdf4llm", mod="pymupdf4llm", venv="venv", probe=None,
                      why="the default parser, ~4 s/document — needed only to read PDFs", blocking=False),
    "fallback":  dict(pkg="docling", mod="docling", venv="venv-docling", probe=None,
                      why="structured fallback (with OCR) for documents the default parser cannot read, ~1 min/document",
                      blocking=False),
    "equations": dict(pkg="marker-pdf", mod="marker", venv="venv-marker",
                      # the symbols prep_md.py actually imports, not a bare `import marker`
                      probe="from marker.converters.pdf import PdfConverter\n"
                            "from marker.models import create_model_dict",
                      why="LaTeX-fidelity parser; ~1.3 GB venv plus ~1.8 GB of models on first use. Pages "
                          "that need OCR also need a llama.cpp binary on PATH (`brew install llama.cpp`); "
                          "born-digital PDFs do not, and this check cannot tell whether it is there",
                      blocking=False),
}
rows = []


def say(state, what, detail=""):
    rows.append(state)
    print(f"  [{state}] {what}" + (f"\n         {detail}" if detail else ""))


def venv_has(py, module, probe=None):
    """True when the venv has a usable install.

    `import marker` succeeds for ANY marker version, because it is a namespace package with no
    __init__.py — so a probe of the symbol actually used is the only check with teeth. On macOS's
    Python 3.9, `pip install marker-pdf` resolves to 0.2.17, which has no `marker.converters` at
    all, and the plain import still passed.
    """
    if not os.path.exists(py):
        return False
    code = probe or f"import {module}"
    r = subprocess.run([py, "-I", "-c", code], capture_output=True)
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
        # Deliberately almost empty. Writing every default here would freeze them: a later release
        # that improves a default would never reach anyone who had ever run `doctor`, and
        # `lacuna config` would report every value as an override the user never made.
        with open(cfg_path, "w") as f:
            json.dump(CONFIG_TEMPLATE, f, indent=2)
        say(OK, f"{cfg_path}   (created empty — defaults are not copied in, so later "
                f"improvements still reach you)")
    cal_src = os.path.join(lacuna_config.ROOT, "scripts", "lit", "calibration.json")
    cal_dst = os.path.join(lacuna_config.DATA, "calibration.json")
    if not os.path.exists(cal_dst) and os.path.exists(cal_src):
        shutil.copy(cal_src, cal_dst)
        say(OK, f"{cal_dst}   (copied from the shipped calibration)",
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
                r = subprocess.run([sys.executable, "-m", "venv", vdir],
                                   capture_output=True, text=True)
                if r.returncode != 0 or not os.path.exists(py):
                    say(BAD, f"could not create a virtualenv at {vdir}",
                        (r.stderr or r.stdout).strip()[-300:]
                        + "\n         on Debian/Ubuntu this usually means: apt install python3-venv")
                    continue
            print(f"  installing {e['pkg']} into {vdir} (this can take a while) ...")
            if subprocess.run([py, "-m", "pip", "install", "-q", e["pkg"]]).returncode:
                say(BAD, f"{e['pkg']} install failed",
                    f"run it by hand to see why: {py} -m pip install {e['pkg']}")
                continue
        if venv_has(py, e["mod"], e.get("probe")):
            say(OK, f"{e['pkg']}", e["why"])
        else:
            fix = os.path.join(lacuna_config.ROOT, "bin", "lacuna")
            say(BAD if e["blocking"] else WARN, f"{e['pkg']} not installed",
                f"{e['why']}\n         fix: {fix} doctor --install-{key}")

    missing, notes = rows.count(BAD), rows.count(WARN)
    print(f"\n{len(rows) - missing - notes}/{len(rows)} checks clean"
          + (f", {notes} optional piece(s) not installed" if notes else "")
          + (f", {missing} BLOCKING" if missing else ""))
    if missing:
        print("Fix the MISS lines above before anything will work.")
    elif notes:
        print("Nothing is blocking. The optional pieces above are only needed to read PDFs;")
        print("markdown and text input, and every cost tool, work without them.")
    print("Try:  lacuna spend --days 7      (costs nothing; reads the transcripts you already have)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
