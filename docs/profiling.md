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

## Replay previous real sessions without rerunning tools

After `make install`, use the standalone benchmark command:

```sh
# Current renderer, three passes per session (newest sessions first).
pcode-benchmark --recent 5 --repeat 3

# Compare incremental parsing with experimental end-only rendering.
pcode-benchmark --recent 5 --repeat 3 --render-mode both

# Choose a specific session or a copied journal. Selectors can be repeated.
pcode-benchmark --replay latest
pcode-benchmark --replay SESSION_ID --session-dir /path/to/sessions
pcode-benchmark --journal /path/to/transcript.jsonl

# Drill into an expensive attempt and collect a function profile.
pcode-benchmark --replay SESSION_ID --start-turn 4 --max-turns 1 \
  --profile /tmp/pcode-replay --profile-cpu

# Compare display choices explicitly, without changing saved preferences.
pcode-benchmark --recent 5 --no-show-thinking
pcode-benchmark --recent 5 --command-scrollback
```

From the repository, `uv run pcode-benchmark …` or
`uv run python -m pcode.profile_benchmark …` runs the same command. Existing
synthetic `--kind` / `--lines` cases still work.

### What is replayed

The benchmark reads the original `transcript.jsonl` records and passes decoded
display events through **the same presentation dispatch used by live pcode**.
It exercises Markdown, visible/hidden thinking, tool-result projection, error and
edit rendering, and retained transcript recording. Rich renders to a counting /
discarding sink; conversation contents never appear in the benchmark output.

No provider or agent is constructed, no tool is called, no session is opened for
writing or recovery, and no SQLite store is opened. There are no model requests or
charges. Local file reads can still update filesystem access timestamps. Session
metadata and journals are read-only; replay creates no locks or new session files.

- A session's readable byte prefix is frozen when it is opened, so new appends to
  an active session do not change later passes. SHA-256 checks detect in-place
  changes between passes; use a closed session or copy for reproducible comparisons.
- All saved attempts/branches are replayed in append order, **not just the final
  selected branch**. Tree selections and compaction checkpoints are counted as
  skipped metadata, not deserialized or rendered. `--start-turn` / `--max-turns`
  refer to journal attempt ordinals, including retries, not necessarily user turns.
  Records outside the requested range are scanned but not rendered.
- Text/thinking completion markers retain live fallback/deduplication behavior.
  Failed, cancelled, restarted, and unfinished attempts flush partial output.
  Retry callbacks, their notices, and exact user prompt grouping are not recorded,
  so attempt boundaries are an approximation of those lifecycle details.
- Defaults are width 80, dark palette, thinking shown, edits shown, command
  scrollback off, 20-line error/command limits. Local preferences and terminal
  background detection are bypassed. The original session did not record all its
  display settings; set the benchmark flags to match the desired comparison.
- Transient streamed edit/command previews, keystrokes, resize, CPR, spinners,
  live prompt/task-widget redraws, model decoding, persistence writes, and child
  process work are **not reconstructed**. These require a live profile. Saved
  events are processed immediately with a flush after each event, without the
  original waits or the live writer's 30 Hz batching.

This measures **current-code CPU cost on historical input**, not the CPU consumed
when the original session ran. Fast replay often occupies one core; that alone is
not evidence of an interactive CPU problem.

### Interpreting replay results

One JSON row is printed per session/mode/pass. It contains no prompt, answer,
tool payload, model/workspace name, session ID, or source path. `session` is a
one-based index in the selected list. Important fields:

- `render_cpu_seconds`: process CPU spent in presentation dispatch and terminal
  output flushes. `render_wall_seconds` is the corresponding elapsed time.
- `cpu_seconds` / `wall_seconds`: replay totals including journal reads, decoding,
  validation, hashing, and benchmark bookkeeping. Subtract render CPU from total
  CPU to estimate that overhead; it is **not** production persistence cost.
- `event_counts`, `turns`, and `top_turns`: coverage and the ten most expensive
  attempts, identified only by ordinal. Per-attempt `characters` counts text in
  both delta and completion records, so it is not a unique-output or token count.
- `journal_sha256` and `snapshot_bytes`: workload identity/size. Compare only rows
  with the same fingerprint, settings, interpreter, and instrumentation modes.
- `skipped_records`: metadata, out-of-range records, malformed records, unknown
  event kinds/versions, invalid event payloads, or oversized records. Records over
  8 MiB are skipped with bounded reader memory. Non-metadata skips mean incomplete
  coverage, not success at reproducing every saved event.

`--repeat` creates fresh presentation state for each pass within one process;
library/OS caches remain warm. Retained transcript/output cycles are collected
between passes, outside measurement, so one pass does not inherit another's
transcript or cleanup cost. `both` alternates mode order across repeats to
reduce ordering bias. Use medians, not one pass. For cold-start comparisons, run
separate processes too. Errors emit only their exception class and the session
index, continue with other sessions, and exit nonzero.

With `--profile`, the new private directory contains a separate capture for every
pass, e.g. `session-1-streamed-1/cpu.txt`. Detailed CPU profiling can be much slower;
first locate an expensive session/attempt without it. Profiles retain code paths
as described above. Workload hashes and timing reports are not a guarantee of
anonymization; keep real captures local and inspect before sharing.

**`end-only` is a benchmark experiment, not a production optimization.** It disables
incremental block parsing but still renders at existing message/thinking/tool/turn
finish boundaries. It delays visible output and can change Markdown spacing or
cross-block interpretation. Differences in `rendered_characters` are expected;
its speedup is an opportunity estimate, not evidence of equivalent UI behavior.

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

### Real-session replay findings

A local replay of **95 saved sessions**, 249 recorded attempts, and 99,168 display
and attempt-boundary events covered about 35.4 MB of journals. Width was 80,
thinking shown, command scrollback off. Each mode ran three times with fresh
presentation state, alternating mode order, and garbage collection outside timing.
No malformed, unknown, invalid, or oversized records were skipped; only tree /
compaction metadata was excluded. No session contents or identifiers are published.

Sums of per-session median CPU times:

| Component | Normal streamed rendering | Experimental end-only |
| --- | ---: | ---: |
| Presentation and Rich rendering | 3.42 s | 2.20 s |
| Whole replay, including journal decoding/validation | 3.84 s | 2.61 s |

That is about **36% less rendering CPU**, but only about **1.2 CPU seconds saved
across the whole archive**. Repeated parsing is a real optimization opportunity,
not sufficient evidence that Markdown explains sustained resource usage. Headless
replay excludes the live prompt layout and transient previews, which may dominate
while the app is running. Real-session profiles also show Rich wrapping/highlighting
and text sanitization; optimizing only the Markdown parser cannot remove those.
These are local workload observations, not cross-machine performance guarantees.

A separate **uninstrumented** idle A/B check used fresh offline processes in real
tmux (100×32), waited three seconds after prompt readiness, then measured process
CPU for 15 seconds. Three trials per variant, in alternating order, gave a median
**4.04% of one core with periodic refresh versus 0.31% with refresh disabled**.
The normal variant ranged from 3.18–5.25%; the disabled variant from 0.30–0.31%.
Disabling refresh was an isolated runtime experiment, not a committed behavior
change; it does not validate active animation. This is about a **92% reduction in
idle CPU** in that setup. Resource/function profiling adds its own CPU cost, so
these figures were measured externally without either profiler enabled.

### Optimization order

1. **Stop idle periodic redraws.** The prompt currently enables a spinner refresh
   timer even when nothing is running. An offline real-tmux function profile showed
   about 235 redraw calls over an 18-second idle capture, dominated by prompt_toolkit
   layout/width allocation, not Markdown. Replace unconditional refresh with an
   activity-aware tick: preserve active spinners and tool elapsed times, but let
   idle input/state/resize events drive rendering. Do not permanently disable
   refresh in production. Validate idle CPU and active animation separately in real
   tmux, including CPR, resize, completion, cancellation, and model initialization.
2. **Avoid redundant layout and parsing work while active.** Cache unchanged preview
   layout/wrapping per render or content/width/theme revision, rather than calculating
   it separately in multiple height/content callbacks. Coalesce Markdown boundary
   checks and track fenced-block state so long containers are not reparsed on every
   newline. The end-only comparison is a ceiling experiment, not the implementation:
   keep streamed visibility, exact chunk-boundary behavior, and cancellation flushes.
   Target near-linear scaling on the synthetic list benchmark and lower CPU on real
   journals; keep `test_transcript.py` and real-tmux streaming/preview regressions.
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

Idle redraw work and repeated parsing are measured; preview caching, persistence,
and history changes still need targeted before/after tests. No production rendering,
persistence, or history optimization is included in the replay tooling change.
Use a live profile of the high-resource workload to distinguish active preview
rendering, backend work, and external processes before applying the remaining fixes.

## Profiler references

The implementation uses Yappi 1.7.6's public `start(profile_threads=True)`, CPU
clock, `convert2pstats`, and cleanup APIs, verified in the installed package.
See [Yappi clocks](https://github.com/sumerc/yappi/blob/master/doc/clock_types.md)
and the [API reference](https://github.com/sumerc/yappi/blob/master/doc/api.md).
