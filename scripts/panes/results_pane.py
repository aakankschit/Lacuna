"""Results pane: a scrollable, auto-refreshing markdown view of the file Claude writes results to.

usage: results_pane.py [FILE | DIR] [--once]   (a DIR follows its newest .md file)
flags: --once render once and exit (for a non-interactive shell)
keys:  j/k or arrows scroll · space/b page · g/G top/bottom · n/p newer/older file · q quit

Renders with `rich` when it is installed, and falls back to plain text when it is not: this is the
only part of the plugin that wants a third-party package, and it is optional.
"""
import os, select, shutil, signal, sys, termios, time, tty
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import lacuna_config
    DEFAULT_DIR = lacuna_config.path("results")
except Exception:
    DEFAULT_DIR = os.environ.get("LACUNA_RESULTS") or os.path.expanduser("~/.claude/results")

try:
    from rich.console import Console
    from rich.markdown import Markdown
    HAVE_RICH = True
except ImportError:                 # plain text is less pretty and still perfectly readable
    HAVE_RICH = False

    class Markdown(str):
        """Stand-in so the render path does not branch: rich renders markdown, we pass it through."""

        def __new__(cls, text, *a, **k):
            return str.__new__(cls, text)

    class Console:
        def __init__(self, *a, width=None, **k):
            self.width = width or shutil.get_terminal_size().columns

        def print(self, obj, *a, **k):
            print(str(obj))

        def capture(self):
            return _Capture(self)


class _Capture:
    """Mimics rich's Console.capture() for the plain-text fallback."""

    def __init__(self, console):
        self.console = console
        self._buf = []

    def __enter__(self):
        self._real, self.console.print = self.console.print, lambda o, *a, **k: self._buf.append(str(o))
        return self

    def __exit__(self, *exc):
        self.console.print = self._real
        return False

    def get(self):
        return "\n".join(self._buf)
POLL = 0.4
INV, DIM, RST = "\033[7m", "\033[2m", "\033[0m"


class Pane:
    def __init__(self, target):
        self.dir_mode = os.path.isdir(target)
        self.target = target
        self.pinned = None          # file chosen with n/p in dir mode
        self.path, self.mtime, self.lines, self.offset = None, 0, [], 0
        self.dirty = True

    def files(self):
        fs = [os.path.join(self.target, f) for f in os.listdir(self.target) if f.endswith(".md")]
        return sorted(fs, key=os.path.getmtime, reverse=True)

    def current(self):
        if not self.dir_mode:
            return self.target
        if self.pinned and os.path.exists(self.pinned):
            return self.pinned
        fs = self.files()
        return fs[0] if fs else None

    def render(self):
        cols, rows = shutil.get_terminal_size()
        path = self.current()
        at_bottom = self.lines and self.offset >= max(0, len(self.lines) - (rows - 1))
        if path != self.path:
            self.offset, at_bottom = 0, False
        self.path = path
        if not path or not os.path.exists(path):
            self.lines, self.mtime = [], 0
        else:
            self.mtime = os.path.getmtime(path)
            text = open(path, errors="ignore").read()
            con = Console(width=max(20, cols - 1), force_terminal=True, color_system="256")
            with con.capture() as cap:
                con.print(Markdown(text, code_theme="monokai", hyperlinks=True))
            self.lines = cap.get().rstrip("\n").split("\n")
        if at_bottom:
            self.offset = max(0, len(self.lines) - (rows - 1))
        self.dirty = True

    def draw(self):
        cols, rows = shutil.get_terminal_size()
        body = rows - 1          # one row is reserved for the header, which must never scroll
        self.offset = max(0, min(self.offset, max(0, len(self.lines) - body)))
        out = ["\033[H"]
        name = os.path.basename(self.path) if self.path else "no results yet"
        when = datetime.fromtimestamp(self.mtime).strftime("%H:%M:%S") if self.mtime else "--:--:--"
        pos = f"{self.offset + 1}-{min(len(self.lines), self.offset + body)}/{len(self.lines)}" if self.lines else "0/0"
        mode = "pinned" if (self.pinned or not self.dir_mode) else "newest"
        head = f" RESULTS · {name} · updated {when} · {pos} · {mode} "
        keys = " j/k space/b g/G n/p q "
        out.append(INV + (head + " " * max(0, cols - len(head) - len(keys)) + keys)[:cols] + RST + "\n")
        if not self.lines:
            msg = "Waiting for results. Claude writes the elaborated results here; its live work stays in the other pane."
            pad = "\n" * max(0, body // 2 - 1)
            out.append(pad + DIM + msg.center(cols)[:cols] + RST)
            out.append("\033[J")
        else:
            # No trailing newline after the final row. Header + `body` rows is exactly the height
            # of the pane, so one more newline scrolls the terminal by a line every redraw, which
            # pushed the header off the top and left the last row blank.
            shown = self.lines[self.offset:self.offset + body]
            out.append("\n".join(ln + "\033[K" for ln in shown))
            out.append("\033[J")
        sys.stdout.write("".join(out).rstrip("\n"))
        sys.stdout.flush()
        self.dirty = False

    def key(self, k):
        rows = shutil.get_terminal_size()[1] - 1
        moves = {"j": 1, "\x1b[B": 1, "k": -1, "\x1b[A": -1, " ": rows, "\x1b[6~": rows, "b": -rows, "\x1b[5~": -rows,
                 "d": rows // 2, "u": -rows // 2}
        if k in moves:
            self.offset += moves[k]
        elif k == "g":
            self.offset = 0
        elif k == "G":
            self.offset = len(self.lines)
        elif k in ("n", "p") and self.dir_mode:
            fs = self.files()
            if fs:
                i = fs.index(self.path) if self.path in fs else 0
                i = max(0, min(len(fs) - 1, i + (-1 if k == "n" else 1)))
                self.pinned = None if i == 0 else fs[i]
                self.render()
        elif k == "f":
            self.pinned = None
            self.render()
        else:
            return
        self.dirty = True


def main():
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return
    once = "--once" in argv
    positional = [a for a in argv if not a.startswith("-")]
    if len(positional) > 1:
        print(f"ERROR: expected one FILE or DIR, got {len(positional)}: {positional}", file=sys.stderr)
        sys.exit(2)
    target = os.path.expanduser(positional[0]) if positional else DEFAULT_DIR
    if not os.path.exists(target):
        if target.endswith(".md"):
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        elif not positional:
            os.makedirs(target, exist_ok=True)      # the configured results dir: fine to create
        else:
            # Creating a directory named after the user's typo is never what they meant.
            print(f"ERROR: no such file or directory: {target}\n"
                  f"Give an existing directory, or a path ending in .md to follow a file that does "
                  f"not exist yet.", file=sys.stderr)
            sys.exit(2)
    pane = Pane(target)
    if once or not sys.stdin.isatty() or not sys.stdout.isatty():
        pane.render()                      # no terminal of its own: print once and exit
        if not once:
            print("Note: no interactive terminal here, so this is a one-off render.", file=sys.stderr)
            print("For the live pane, run it in its own split (Ghostty \u2318D), not inside Claude Code.", file=sys.stderr)
        print("\n".join(pane.lines) if pane.lines else f"(no content yet: {pane.path or target})")
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    signal.signal(signal.SIGWINCH, lambda *_: pane.render())
    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        tty.setcbreak(fd)
        pane.render()
        last_check = 0
        while True:
            if pane.dirty:
                pane.draw()
            r, _, _ = select.select([sys.stdin], [], [], POLL)
            if r:
                k = os.read(fd, 8).decode(errors="ignore")
                if k == "q":
                    break
                pane.key(k)
            if time.time() - last_check >= POLL:
                last_check = time.time()
                path = pane.current()
                if path != pane.path or (path and os.path.exists(path) and os.path.getmtime(path) != pane.mtime):
                    pane.render()
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
