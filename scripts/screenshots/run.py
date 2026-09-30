"""Play screenshot scenes in tmux and save each shot as an SVG.

    uv run --no-sync python scripts/screenshots/run.py            # every scene
    uv run --no-sync python scripts/screenshots/run.py tree jobs  # some scenes

Each scene runs in its own tmux server, with HOME and the XDG directories
pointed at /tmp/pcode-demo, inside a small throwaway git repo there. Nothing
touches your real sessions, preferences or logins. See scene.py for the scene
format.
"""

import argparse
import contextlib
import importlib.util
import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from rich.console import Console
from rich.text import Text

HERE = Path(__file__).resolve().parent
SCENES = HERE / "scenes"
OUT = HERE.parents[1] / "docs" / "assets" / "screenshots"
DEMO_ROOT = Path("/tmp/pcode-demo")
TIMEOUT = 30
# What changes on a settled screen: spinner frames and running calls' clocks.
TICKING = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◜◠◝◞◡◟]|\d+(\.\d+)?s\b")

# status off: the pane gets the whole window, so SIZE is the screenshot size.
TMUX_CONF = """\
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*:Tc"
set -g status off
set -g history-limit 5000
"""

DEMO_FILES = {
    "README.md": "# acme-api\n\nA small orders service.\n",
    "pyproject.toml": '[project]\nname = "acme-api"\nversion = "0.3.0"\n',
    "acme/__init__.py": "",
    "acme/orders.py": (
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\n"
        "class Order:\n"
        "    id: int\n"
        "    items: list[float]\n"
        "    discount: float = 0.0\n\n\n"
        "def total(order: Order) -> float:\n"
        "    subtotal = sum(order.items)\n"
        "    return subtotal - order.discount\n"
    ),
    "tests/test_orders.py": (
        "from acme.orders import Order, total\n\n\n"
        "def test_total_applies_discount():\n"
        "    assert total(Order(1, [10.0, 30.0], discount=25)) == 30.0\n"
    ),
}


def demo_repo(path: Path) -> None:
    for name, content in DEMO_FILES.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    git = ["git", "-C", str(path), "-c", "user.name=Demo", "-c", "user.email=demo@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Initial commit"], check=True)


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"scene_{name}", SCENES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HERE))
    spec.loader.exec_module(module)
    return module


def svg(ansi: str, width: int, title: str) -> str:
    console = Console(
        record=True, width=width, file=io.StringIO(), force_terminal=True, color_system="truecolor"
    )
    for line in ansi.rstrip("\n").split("\n"):
        console.print(Text.from_ansi(line), no_wrap=True, overflow="crop")
    return console.export_svg(title=title)


class Pane:
    def __init__(self, env: dict, root: Path):
        self.server = "pcode-shots-" + uuid.uuid4().hex[:8]
        conf = root / "tmux.conf"
        conf.write_text(TMUX_CONF)
        self.base = ["tmux", "-L", self.server, "-f", str(conf)]
        self.env = env

    def __call__(self, *args: str) -> str:
        return subprocess.check_output([*self.base, *args], text=True, env=self.env)

    def screen(self, *, colors: bool = False) -> str:
        return self("capture-pane", "-p", *(["-e"] if colors else []), "-t", "shot:0.0")

    def wait(self, text: str) -> None:
        """Wait for `text`, which may wrap: any whitespace run matches any other."""
        deadline = time.monotonic() + TIMEOUT
        wanted = " ".join(text.split())
        while wanted not in " ".join((screen := self.screen()).split()):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{text!r} never appeared:\n{screen}")
            time.sleep(0.1)

    def still(self, quiet: float = 0.6) -> None:
        """Wait until the screen stops changing, ignoring spinners and clocks."""
        deadline = time.monotonic() + TIMEOUT
        previous, since = None, time.monotonic()
        while time.monotonic() < deadline:
            screen = TICKING.sub("", self.screen())
            if screen != previous:
                previous, since = screen, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return
            time.sleep(0.1)

    def kill(self) -> None:
        subprocess.run([*self.base, "kill-server"], capture_output=True, env=self.env)


def stop_jobs(root: Path) -> None:
    """Stop background jobs a scene started; they outlive the terminal by design.

    Each job's supervisor leads its own process group and names its job
    directory, under `root`, on its command line. A stop is SIGTERM to the group.
    """
    found = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True)
    for pid in map(int, found.stdout.split()):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)


def play(name: str, out: Path, *, text: bool = False) -> list[Path]:
    scene = load(name)
    width, height = getattr(scene, "SIZE", (100, 30))
    saved = []
    # A fixed, neutral path: pcode's banner prints the workspace in full, and a
    # default temp directory would put your username in the docs.
    # Resolved (macOS /tmp is a symlink), so pcode shows the repo as ~/acme-api.
    root = DEMO_ROOT.resolve()
    shutil.rmtree(root, ignore_errors=True)
    repo = root / "acme-api"
    repo.mkdir(parents=True)
    demo_repo(repo)
    try:
        env = {
            **os.environ,
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_STATE_HOME": str(root / ".local/state"),
            "XDG_DATA_HOME": str(root / ".local/share"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "COLORTERM": "truecolor",
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(HERE), os.environ.get("PYTHONPATH", "")) if p
            ),
        }
        env.pop("TMUX", None)
        env.pop("PROMPT_TOOLKIT_NO_CPR", None)
        pane = Pane(env, root)
        command = shlex.join([sys.executable, str(SCENES / f"{name}.py")])
        try:
            pane("new-session", "-d", "-s", "shot", "-x", str(width), "-y", str(height),
                 "-c", str(repo), command)  # fmt: skip
            pane.wait("❯")
            for step in scene.STEPS:
                kind, *args = step
                if kind == "type":
                    pane("send-keys", "-t", "shot:0.0", "-l", args[0])
                elif kind == "key":
                    pane("send-keys", "-t", "shot:0.0", *args)
                elif kind == "wait":
                    pane.wait(args[0])
                elif kind == "sleep":
                    time.sleep(args[0])
                elif kind == "shot":
                    pane.still()
                    path = out / f"{args[0]}.svg"
                    title = args[1] if len(args) > 1 else "pcode"
                    path.write_text(svg(pane.screen(colors=True), width, title))
                    if text:
                        print(f"--- {path.name}\n{pane.screen().rstrip()}")
                    saved.append(path)
                else:
                    raise ValueError(f"{name}: unknown step {step!r}")
        finally:
            pane.kill()
            stop_jobs(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenes", nargs="*", help="scene names (default: all)")
    parser.add_argument("--out", type=Path, default=OUT, help=f"output directory ({OUT})")
    parser.add_argument(
        "--text", action="store_true", help="also print each shot as plain text, for review"
    )
    args = parser.parse_args()
    names = args.scenes or sorted(p.stem for p in SCENES.glob("*.py"))
    args.out.mkdir(parents=True, exist_ok=True)
    for name in names:
        for path in play(name, args.out, text=args.text):
            print(path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path)


if __name__ == "__main__":
    main()
