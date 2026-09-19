"""Offline streaming benchmark: python -m pcode.profile_benchmark --help."""

import argparse
import asyncio
import gc
import json
import platform
import sys
import time
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.profiling import profile_session
from pcode.replay_benchmark import EndOnlyOutput, JournalSnapshot, ReplaySettings, replay_journal
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


def benchmark_output(width: int, *, mode: str = "streamed"):
    sink = CountingOutput()
    terminal = DummyOutput()
    terminal.get_size = lambda: Size(rows=24, columns=width)
    app = SimpleNamespace(output=CursorSafeOutput(terminal), invalidate=lambda: None)
    output_type = EndOnlyOutput if mode == "end-only" else TerminalOutput
    output = output_type(Console(file=sink, color_system=None, width=width), app)
    return output, sink


async def stream_workload(kind: str, lines: int, width: int) -> int:
    output, sink = benchmark_output(width)
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
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--kind", choices=("prose", "list", "fence"))
    source.add_argument("--replay", action="append", metavar="SESSION", help="ID/prefix or latest")
    source.add_argument("--recent", type=positive_int, metavar="N", help="Replay N newest sessions")
    source.add_argument("--journal", type=Path, action="append", help="Replay explicit JSONL files")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--repeat", type=positive_int, default=1)
    parser.add_argument(
        "--render-mode", choices=("streamed", "end-only", "both"), default="streamed"
    )
    parser.add_argument("--show-thinking", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--command-scrollback", action="store_true")
    parser.add_argument("--start-turn", type=positive_int, default=1)
    parser.add_argument("--max-turns", type=positive_int)
    parser.add_argument("--lines", type=positive_int, default=500)
    parser.add_argument("--width", type=positive_int, default=80)
    parser.add_argument("--profile", type=Path, metavar="DIR")
    parser.add_argument("--profile-cpu", action="store_true")
    parser.add_argument("--profile-memory", action="store_true")
    args = parser.parse_args()
    if (args.profile_cpu or args.profile_memory) and args.profile is None:
        parser.error("--profile-cpu and --profile-memory require --profile DIR")
    if args.replay or args.recent or args.journal:
        _replay(args, parser)
        return
    if (
        args.repeat != 1
        or args.render_mode != "streamed"
        or args.session_dir is not None
        or args.start_turn != 1
        or args.max_turns is not None
        or not args.show_thinking
        or args.command_scrollback
    ):
        parser.error("replay options require --replay, --recent, or --journal")
    args.kind = args.kind or "list"
    with capture(args, args.profile):
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


def capture(args, directory):
    return (
        profile_session(directory, cpu=args.profile_cpu, memory=args.profile_memory)
        if directory
        else nullcontext()
    )


def _replay(args, parser):
    from pcode.sessions import list_sessions, resolve_session, session_root

    root = args.session_dir or session_root()
    try:
        if args.journal:
            journals = args.journal
        elif args.recent:
            journals = [
                root / info.id / "transcript.jsonl" for info in list_sessions(root)[: args.recent]
            ]
        else:
            journals = [
                resolve_session(selector, root) / "transcript.jsonl" for selector in args.replay
            ]
        if not journals:
            parser.error("no saved sessions found")
        if args.profile:
            args.profile.mkdir(mode=0o700, parents=True, exist_ok=False)
    except (OSError, ValueError) as error:
        parser.error(
            f"Cannot prepare replay ({type(error).__name__}); check source and output paths"
        )
    modes = ("streamed", "end-only") if args.render_mode == "both" else (args.render_mode,)
    settings = ReplaySettings(
        show_thinking=args.show_thinking,
        command_scrollback=args.command_scrollback,
        start_turn=args.start_turn,
        max_turns=args.max_turns,
    )
    failed = False
    for index, path in enumerate(journals, 1):
        try:
            with closing(JournalSnapshot(path)) as snapshot:
                for repeat in range(1, args.repeat + 1):
                    # Reduce first-mode cache/order bias in comparison runs.
                    ordered_modes = modes if repeat % 2 else modes[::-1]
                    for mode in ordered_modes:
                        # Transcript/output callbacks form cycles. Collect the previous
                        # pass outside the next capture, after dropping its local refs.
                        gc.collect()
                        result = _replay_pass(args, snapshot, settings, index, repeat, mode)
                        print(json.dumps(result), flush=True)
        except Exception as error:
            # Journal contents and even exception messages may contain private data.
            print(json.dumps(dict(session=index, error=type(error).__name__)), flush=True)
            failed = True
        finally:
            gc.collect()
    if failed:
        parser.exit(1, "One or more replays failed; no session data was modified.\n")


def _replay_pass(args, snapshot, settings, index, repeat, mode):
    # Keep renderer ownership inside one pass so no strong reference survives
    # into the next pass, including when decoding/rendering raises an exception.
    output, sink = benchmark_output(args.width, mode=mode)
    directory = args.profile / f"session-{index}-{mode}-{repeat}" if args.profile else None
    with capture(args, directory):
        result = asyncio.run(replay_journal(snapshot, output, settings))
    return dict(
        schema_version=1,
        scope="saved_display_events",
        python=platform.python_version(),
        platform=sys.platform,
        session=index,
        repeat=repeat,
        render_mode=mode,
        width=args.width,
        show_thinking=args.show_thinking,
        command_scrollback=args.command_scrollback,
        start_turn=args.start_turn,
        max_turns=args.max_turns,
        rendered_characters=sink.characters,
        profiled=args.profile is not None,
        cpu_tracing=args.profile_cpu,
        memory_tracing=args.profile_memory,
        **result,
    )


if __name__ == "__main__":
    main()
