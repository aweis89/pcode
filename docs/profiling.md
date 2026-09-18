# Resource profiling and optimization plan

Start with resource sampling while reproducing the problem:

```sh
pcode --profile /tmp/pcode-resources
# Use pcode normally, then /quit to finalize the report.
```

Use a **new directory** for each capture. Profiling is off by default: no monitor,
profiler hooks, or allocation tracing run without `--profile`. Sampling runs once
per second in a worker thread, plus at startup and shutdown. It writes directly
to disk rather than keeping the sample history in memory.

## Capture a focused trace

If pcode itself is consuming CPU, repeat a short representative workload with
function tracing. If its memory keeps growing, do a separate allocation capture:

```sh
pcode --profile /tmp/pcode-functions --profile-cpu
pcode --profile /tmp/pcode-memory --profile-memory

# Also works with resume and with the offline sample:
pcode --resume latest --profile /tmp/pcode-resume
pcode --demo --profile /tmp/pcode-demo --profile-cpu
```

Function and allocation tracing can substantially slow Python down and increase
memory use. Prefer separate runs; use uninstrumented runs or resource-only captures
for before/after timing, not the timings of deeply instrumented runs. In the
500-line list benchmark, resource-only capture took about 1.75 CPU seconds versus
1.71 without profiling; detailed CPU tracing took about 52 seconds. These are
workload-specific observations, not an overhead guarantee. The monitor's
own CPU/memory is included in the process measurements. There are no performance
thresholds enforced by this feature.

### Artifacts

| File | Contents |
| --- | --- |
| `summary.json` | Wall duration, process CPU seconds and average utilization, sampled peak RSS for pcode and its observed descendants, sample/error counts, tracing modes, Python version/platform |
| `resources.jsonl` | Elapsed time and per-PID CPU seconds, CPU percentage, RSS bytes, parent PID, and thread count at each sample |
| `cpu.txt` | With `--profile-cpu`: top 50 functions by cumulative and self time |
| `cpu.pstats` | With `--profile-cpu`: full Yappi CPU profile exported in standard `pstats` format for caller/callee analysis |
| `allocations.json` | With `--profile-memory`: top 50 **still-live** Python allocation locations at shutdown, counts, and bytes; samples/summary also include traced current/peak bytes |

CPU percentage uses **100% = one fully occupied core**, not the whole machine.
It can exceed 100% for multithreaded processes. The first observation of each PID
has `cpu_percent: null`; subsequent values use CPU-time deltas. Per-PID CPU seconds
are lifetime counters; the summary's process CPU seconds cover the capture only.

**Function timings use CPU time, not wall time.** Yappi measures per-thread CPU
and merges function statistics across Python threads, including worker-thread
backend initialization. Sleeping and network waiting are excluded. Cumulative time
includes callees; self time excludes them. Profiler overhead is included. Threads
running only native code and child processes are not function-attributed; compare
resource samples to detect work missing from the function profile.

Yappi is deliberate: the installed Python 3.14 `cProfile` observes multiple threads,
and supplying a per-thread CPU clock produced negative/corrupt timings. Yappi owns
separate thread call stacks and CPU clocks. Captures refuse to replace an already
running Yappi profiler or its existing results.

Inspect a trusted capture without extra packages:

```sh
cat /tmp/pcode-resources/summary.json
cat /tmp/pcode-functions/cpu.txt
python -m pstats /tmp/pcode-functions/cpu.pstats
# At the pstats prompt: sort cumulative, stats 30, callers _commit_blocks
```

### Scope, privacy, and failure behavior

- Capture starts after CLI parsing, before app construction, and ends after app
  cleanup. Initial Python/module imports and argument parsing are **not** captured.
  Deferred backend initialization is included in resource samples. Memory tracing
  sees allocations made after tracing starts, not all existing memory.
- RSS includes native memory; `tracemalloc` only sees traced Python allocations.
  A high traced peak does not identify its allocation site: the final snapshot
  contains surviving allocations, not objects already freed at the peak. RSS may
  stay high after objects are freed because allocators retain memory.
- Child processes are sampled, not function-profiled. Fast-exiting or reparented
  children may be missed. Shared pages can be double-counted when summing RSS.
  An independently running proxy/server is outside the process tree and needs its
  own profiling. `sampling_errors` includes disappeared/access-denied processes
  and monitor write failures; treat nonzero counts as incomplete coverage.
- Normal exit, Python exceptions, and handled interrupts finalize the report.
  Forced termination (`SIGKILL`, default `SIGTERM`, process crash) cannot finalize
  it; already-flushed JSONL samples remain available. No signal handlers are replaced.
  Output errors warn without replacing the app's exit status. A failed startup
  can leave a partial directory; choose a fresh path on retry.
- Output directories are private (0700), files 0600 on POSIX. Existing capture
  directories are rejected, including symlinks. No uploads or automatic retention:
  delete old captures yourself. JSONL grows with duration and child count.
- No prompts, tool arguments/results, environment values, process command lines,
  frame locals, source lines, or heap contents are deliberately recorded. **Source
  filenames and function names are recorded** and can disclose local directory or
  account names. Inspect artifacts before sharing, and never commit real captures.
  `--no-save` controls conversations, not explicitly requested profiling output.

## Reproducible offline streaming benchmark

This runs the real `TerminalOutput` Markdown streaming/rendering path with a dummy
terminal and a counting/discarding output sink. It does not initialize a provider,
read saved sessions, or make network requests. Each line is delivered as one chunk
and output is flushed after each chunk. Width defaults to 80 columns.

```sh
uv run python -m pcode.profile_benchmark --kind list --lines 500
uv run python -m pcode.profile_benchmark --kind list --lines 1000
uv run python -m pcode.profile_benchmark --kind prose --lines 1000
uv run python -m pcode.profile_benchmark --kind fence --lines 1000
uv run python -m pcode.profile_benchmark --kind list --lines 500 \
  --profile tmp/list-profile --profile-cpu
```

Each run prints a JSON row with wall/CPU seconds, workload dimensions, rendered
character count, and instrumentation flags. Run each baseline at least three times
in separate processes and compare medians. Keep dependencies, machine, width,
workload, and tracing modes the same. This is a component benchmark, **not** a
measurement of real terminal repainting, CPR, provider latency, tool execution,
session persistence, or a long-lived conversation's memory.

## Initial findings

Measured locally on macOS arm64, Python 3.14.0, at width 80, with three fresh
**uninstrumented** processes per case. Values below are median process CPU seconds;
wall time was within 0.02 seconds of CPU time. Imports happen before timing.

| Stream | 250 lines | 500 lines | 1,000 lines |
| --- | ---: | ---: | ---: |
| Separate prose paragraphs | 0.089 | 0.179 | 0.346 |
| One growing Markdown list | 0.442 | 1.712 | 6.850 |
| One fenced code block | 0.071 | 0.186 | 0.575 |

The list costs about **4× as much CPU when its size doubles**, versus roughly 2×
for separate paragraphs. `TerminalOutput._commit_blocks` constructs `Markdown`
from the entire unfinished container on each newline. The list benchmark's call
profile records 125,750 paragraph parses for 500 items, consistent with parsing
1 + 2 + … + 500 items, then the final render. This is a demonstrated component
hotspot, not proof that it accounts for all resource usage in a real session.

### Optimization order

1. **Stop reparsing growing Markdown containers on every newline.** First prototype
   coalesced boundary checks and explicit fenced-block state, then handle lists and
   quotes without invalid early commits. Target near-linear scaling in the 250 /
   500 / 1,000-line benchmark, preserving exact static-versus-streamed output,
   cancellation flushes, and chunk-boundary independence. Run `test_transcript.py`
   and real-tmux thinking/streaming regressions before shipping any change.
2. **Measure actual idle and active terminal rendering.** Capture 60 seconds idle,
   60 seconds streaming, and a large command/edit preview at fixed pane dimensions.
   Compare process CPU and function-call counts. Candidates: repeated
   `preview_layout()` work per redraw, constant spinner/resize wakeups, and Git
   branch polling. Cache unchanged layout work or gate refreshes only if measured;
   preserve real-tmux CPR, resize, and editing regressions.
3. **Separate persistence cost from rendering.** Use the same synthetic model
   stream with saving on/off, varying delta count and history size. Measure journal
   append count, CPU, event-loop delay, bytes written, and resume time. Candidates:
   batching ordered delta records, deduplicating unchanged status events, avoiding
   repeated journal scans. Keep durable tool boundaries and crash/branch recovery.
4. **Measure retained memory over settled turns.** Compare RSS and Python current
   bytes over increasing turn counts, with and without saving and compaction.
   Investigate full-history copies in unsaved conversation-tree nodes and retry
   checkpoints, large coalesced thinking strings, and entry-count-only transcript
   retention. A byte budget or shared immutable history needs explicit eviction /
   branching semantics, not a blind cache or truncation fix.
5. **Profile the responsible process.** If child CPU/RSS dominates, identify its
   PID locally and investigate that command or managed proxy separately. Changing
   pcode's renderer will not fix an expensive external process.

Items 2–5 remain hypotheses or measurement tasks. No rendering, persistence, or
history optimization is included in the profiling change itself. A representative
capture of the high-resource live session is still needed to prioritize beyond
the confirmed Markdown scaling problem.

## Profiler references

The implementation uses Yappi 1.7.6's public `start(profile_threads=True)`, CPU
clock, `convert2pstats`, and cleanup APIs, verified in the installed package.
See [Yappi clocks](https://github.com/sumerc/yappi/blob/master/doc/clock_types.md)
and the [API reference](https://github.com/sumerc/yappi/blob/master/doc/api.md).
