"""Offline streaming benchmark: python -m pcode.profile_benchmark --help."""

import argparse
import asyncio
import json
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.profiling import profile_session
from pcode.ui import CursorSafeOutput, TerminalOutput


class CountingOutput:
    """Discard rendered content so the benchmark sink cannot look like a leak."""

    def __init__(self):
        self.characters = 0

    def write(self, text: str) -> int:
        self.characters += len(text)
        return len(text)

    def flush(self) -> None:
        pass


async def stream_workload(kind: str, lines: int, width: int) -> int:
    sink = CountingOutput()
    terminal = DummyOutput()
    terminal.get_size = lambda: Size(rows=24, columns=width)
    app = SimpleNamespace(output=CursorSafeOutput(terminal), invalidate=lambda: None)
    output = TerminalOutput(Console(file=sink, color_system=None, width=width), app)
    if kind == "fence":
        output.delta("```python\n")
    for index in range(lines):
        if kind == "prose":
            text = f"Plain paragraph number {index}.\n\n"
        elif kind == "list":
            text = f"- List item number {index}.\n"
        else:
            text = f"value_{index} = {index}\n"
        output.delta(text)
        await output.flush()
    if kind == "fence":
        output.delta("```\n")
    output.finish()
    await output.flush()
    return sink.characters


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("prose", "list", "fence"), default="list")
    parser.add_argument("--lines", type=positive_int, default=500)
    parser.add_argument("--width", type=positive_int, default=80)
    parser.add_argument("--profile", type=Path, metavar="DIR")
    parser.add_argument("--profile-cpu", action="store_true")
    parser.add_argument("--profile-memory", action="store_true")
    args = parser.parse_args()
    if (args.profile_cpu or args.profile_memory) and args.profile is None:
        parser.error("--profile-cpu and --profile-memory require --profile DIR")
    capture = (
        profile_session(args.profile, cpu=args.profile_cpu, memory=args.profile_memory)
        if args.profile
        else nullcontext()
    )
    with capture:
        started = time.perf_counter()
        cpu_started = time.process_time()
        characters = asyncio.run(stream_workload(args.kind, args.lines, args.width))
        result = dict(
            kind=args.kind,
            lines=args.lines,
            width=args.width,
            rendered_characters=characters,
            wall_seconds=time.perf_counter() - started,
            cpu_seconds=time.process_time() - cpu_started,
            profiled=args.profile is not None,
            cpu_tracing=args.profile_cpu,
            memory_tracing=args.profile_memory,
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
