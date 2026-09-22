# Resource profiling and optimization plan

Start with resource sampling while reproducing the problem:

```sh
pcode --profile
# Use pcode normally, then /quit to finalize the report.
python -m pcode.profiling            # List captures, newest last.
python -m pcode.profiling DIR        # Read one.
```

Bare `--profile` writes to a new timestamped directory under
`~/.local/state/pcode/profiles` (`PCODE_PROFILE_DIR` or `XDG_STATE_HOME`
relocate it) and keeps the newest 20 of those, deleting older ones on the next
launch. `--profile DIR` still names its own directory, which is never pruned and
must be **new** for each capture.

Profiling is off by default: no monitor, profiler hooks, or allocation tracing
run unless asked for. Sampling runs once per second in a worker thread, plus at
startup and shutdown. It writes directly to disk rather than keeping the sample
history in memory.

Every sample measures pcode and the descendants it already knows about, but the
process tree is only re-walked every five seconds: finding descendants means
reading every process on the machine, which costs roughly a hundred times more
than measuring the known ones and would otherwise make an always-on capture the
largest thing in its own report.

## Capture every session

Anecdotal slowness usually happens in an ordinary session nobody thought to
profile. Turn capture on by default instead:

```sh
pcode config set profile resources   # Sampling only; off, resources, cpu, memory
pcode --no-profile                   # Skip the capture for one run.
```

`cpu` and `memory` apply the matching tracer to **every** session and are much
slower; leave the default on `resources` and reach for a tracer on a run that
reproduces the problem. Captures accumulate under the state directory until
pruning removes them, and they record source filenames and function names, so
treat an always-on default as local debug output rather than something to leave
running on a shared machine.

## Capture a focused trace

If pcode itself is consuming CPU, repeat a short representative workload with
function tracing. If its memory keeps growing, do a separate allocation capture:

```sh
pcode --profile /tmp/pcode-functions --profile-cpu
pcode --profile /tmp/pcode-memory --profile-memory

# Also works with a continued session and with the offline sample:
pcode --continue --profile /tmp/pcode-resume
pcode --theme-preview --profile /tmp/pcode-preview --profile-cpu
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
| `summary.json` | Wall duration, process CPU seconds and average utilization, sampled peak RSS for pcode and its observed descendants, sample/error/interval counts, tracing modes, activity totals, Python version/platform |
| `resources.jsonl` | Elapsed time, the current activity, and per-PID CPU seconds, CPU percentage, RSS bytes, parent PID, and thread count at each sample |
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

### Working cost versus idle cost

Each sample names the activity in progress, and `summary.json` carries per-label
wall/CPU totals plus `idle_seconds` / `idle_cpu_seconds` for everything outside a
span. Today one span exists, `turn`: it opens when a turn starts streaming and
closes when the consumer has rendered the last event, so it covers model waiting,
tool execution, and display work for that turn. Everything else — sitting at the
prompt, editing a draft, background metadata polling — is unattributed.

That split answers the first question a resource complaint raises: whether the
CPU goes to work you asked for or to a session doing nothing. CPU here is
whole-process, not per-task: overlapping spans each see the whole process, so
their CPU seconds can sum to more than the process spent, and a turn's CPU
includes any background thread running beside it. `idle_*` is clamped at zero.

### Reading a capture

`python -m pcode.profiling DIR` prints a digest: how the capture ended, duration,
process CPU and share of one core, peak RSS, the activity/idle split, peak thread
count, the five child processes with the most CPU, and which tracer artifacts
exist. It reads only what the capture wrote, so a killed session still reports.

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
  children may be missed, including any that both starts and exits between two
  tree walks (`descendant_scan_seconds` in the summary).
  Shared pages can be double-counted when summing RSS.
  An independently running proxy/server is outside the process tree and needs its
  own profiling. `sampling_errors` includes disappeared/access-denied processes
  and monitor write failures; treat nonzero counts as incomplete coverage.
- Normal exit, Python exceptions, and handled interrupts finalize the report.
  Forced termination (`SIGKILL`, default `SIGTERM`, process crash) cannot finalize
  it, but `summary.json` is rewritten atomically after every sample, so a killed
  session still leaves the totals up to its last sample, marked `"complete":
  false` and missing the tracer artifacts. Already-flushed JSONL samples remain
  available. No signal handlers are replaced.
  Output errors warn without replacing the app's exit status. A failed startup
  can leave a partial directory; choose a fresh path on retry.
- Output directories are private (0700), files 0600 on POSIX. Existing capture
  directories are rejected, including symlinks. Nothing is uploaded. Only
  automatically named captures are pruned, and only by name and count, never by
  age or size: a directory you named is yours to delete. JSONL grows with
  duration and child count.
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

# The biggest journals, which is where long lists and tables live.
pcode-benchmark --largest 3 --command-scrollback

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

### Live replay through the real editor

The offline replay leaves out the part that dominates a real session: every
scrollback write erases and repaints the prompt_toolkit editor and live panel,
and the spinner repaints them again between writes. On a saturated stream that
was ~65% of process CPU where Markdown rendering was ~5%. `--live` plays one
journal through the real turn path in the current terminal, paced by the
journal's timestamps, and reports process CPU, wall time, editor renders, and
scrollback flushes when the last turn ends:

```sh
# Real time, as the session originally streamed (gaps capped at 2 s).
pcode-benchmark --live --replay SESSION_ID

# Ten times faster, with a function profile of the whole run.
pcode-benchmark --live --replay SESSION_ID --speed 10 \
  --profile /tmp/pcode-live --profile-cpu

# Unattended, from a script: the terminal is the UI, so the JSON goes to a file.
tmux new-session -d -x 100 -y 40 \
  'pcode-benchmark --live --largest 1 --speed 10 --result /tmp/live.json'
```

A repaint is a full prompt_toolkit layout pass over the bottom block. Measured
on a silent turn (spinner only) it was 3.9 ms, 6.2 ms with a task panel, of
which ~45% was `VSplit._divide_widths`/`HSplit._divide_heights` growing the
children one cell at a time; `layout_speed.py` replaces those with an exact
closed form, bringing a repaint to ~2.5/3.5 ms. What remains is the container
walk and control rendering, spread thinly. The other lever is frame rate: the
panel repaints at the rate of the fastest spinner on screen (`dots`, 80 ms,
during a model turn).

No model or tool runs; the journal's events are handed to the same code a live
turn uses. `--speed 0` drops the pacing and mostly measures the flush loop's
batching, so compare speeds against each other rather than against offline
replay. `cpu_fraction` is process CPU over wall time: the share of one core the
session would have cost at that speed. Keystrokes, resizes, and the original
tool waits beyond the gap cap are not reproduced.

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

### Implemented prompt optimizations

The first two prompt optimizations are now production behavior, not runtime
monkeypatches. Markdown rendering is unchanged.

1. **Activity-aware refresh.** Idle prompts have no animation timer. Input, resize,
   and application state changes still invalidate normally. A redraw while busy,
   showing a running prompt, or showing running tools schedules the next animation
   tick. Settling/cancelling work stops it; application shutdown owns task cleanup.
   This also covers backend initialization before a prompt exists. Merely changing
   prompt_toolkit's `refresh_interval` at runtime would not work: its refresh loop
   captures the original interval when it starts.
2. **Bounded preview caching.** Frame/preview allocation, task rows, and queue rows
   are reused within one redraw and terminal size, then discarded. Editor, menu,
   task, queue, and CPR changes therefore cannot inherit an old layout. One preview
   body is cached across redraws by content, width, command/edit kind, and syntax
   theme. Titles and tail-height allocation remain outside that cache. Hiding or
   clearing previews releases the retained body on the next redraw.

#### Before/after measurements

Local macOS arm64 / Python 3.14.0 measurements compared the code before these
changes (`ea48f26`) with the new production implementation, without profiling.
These are workload-specific CPU results, not overall resource or memory claims.

**Idle, real tmux:** fresh offline processes, 100×32, three seconds of warmup after
readiness, then 15 seconds measured externally with process CPU counters. Three
trials per version, interleaved old/new/new/old/old/new:

| Metric | Before | After |
| --- | ---: | ---: |
| Median CPU, percentage of one core | 3.02% | 0.31% |
| Trial range | 2.37–3.35% | 0.25–0.36% |

That is **about 90% less idle CPU** with activity-aware refresh, rather than the
older experiment that disabled animation unconditionally. Background metadata
polling still runs, so idle CPU is not zero. Real-tmux tests separately verify
spinner changes and advancing tool elapsed times during a quiet provider pause,
then resize, cancellation, and draft input.

**Active layout microbenchmark:** 32 rows, five tasks, a running prompt, and a
synthetic command preview containing 160 lines of roughly 100 characters each.
Sixty actual Application redraws after a warmup redraw; median of three passes,
measuring process CPU. The changing variant appends one output line before every
redraw. Imports and prompt construction are outside the timing.

| Preview workload | Width | Before CPU | After CPU | Reduction |
| --- | ---: | ---: | ---: | ---: |
| Unchanged body | 40 | 5.328 s | 0.129 s | 97.6% |
| Unchanged body | 100 | 5.249 s | 0.217 s | 95.9% |
| Body changes every redraw | 40 | 5.953 s | 0.370 s | 93.8% |
| Body changes every redraw | 100 | 5.718 s | 0.463 s | 91.9% |

Previously, height/visibility/content callbacks repeatedly sanitized and wrapped
that entire output within the same redraw. Tests now assert one body computation
for an unchanged preview and one task-row calculation per redraw in the measured
layout, plus invalidation on content, width, theme, and visibility changes.

This is a headless prompt-layout microbenchmark, **not end-to-end active-turn CPU**
and not the historical journal replay. It excludes real terminal I/O, provider
work, persistence, and subprocesses. Real CPR/height behavior is covered separately
by the tmux command/edit-preview regressions. No real provider requests or
historical tool execution were needed. Local scripts and raw measurements are in
`tmp/replay-benchmarks/` (`idle_production.py`, `idle-production.jsonl`,
`layout_cpu.py`, and `layout[-changing]-{before,after}.jsonl`); they are gitignored.

### Remaining optimization TODO

1. **Markdown rendering, explicitly deferred.** Coalesce boundary checks and
   investigate fenced-block state so long containers are not reparsed on every
   newline. The end-only comparison remains an experiment, not the implementation:
   preserve streamed visibility, chunk-boundary behavior, and cancellation flushes.
   Target near-linear scaling on the synthetic list benchmark and lower CPU on real
   journals; keep transcript and real-tmux streaming regressions.
2. **Separate persistence cost from rendering.** Use the same synthetic model
   stream with saving on/off, varying delta count and history size. Measure journal
   append count, CPU, event-loop delay, bytes written, and resume time. Candidates:
   batching ordered delta records, deduplicating unchanged status events, avoiding
   repeated journal scans. Keep durable tool boundaries and crash/branch recovery.
3. **Measure retained memory over settled turns.** Compare RSS and Python current
   bytes over increasing turn counts, with and without saving and compaction.
   Investigate full-history copies in unsaved conversation-tree nodes and retry
   checkpoints, large coalesced thinking strings, and entry-count-only transcript
   retention. A byte budget or shared immutable history needs explicit eviction /
   branching semantics, not a blind cache or truncation fix.
4. **Profile the responsible process.** If child CPU/RSS dominates, identify its
   PID locally and investigate that command or managed proxy separately. Changing
   pcode's renderer will not fix an expensive external process.

Idle redraw and command-preview layout savings are measured above. Markdown,
persistence, and history optimizations remain unimplemented. Use a live profile
of the high-resource workload to distinguish remaining preview/rendering work,
backend work, and external processes before applying the remaining fixes.

## Profiler references

The implementation uses Yappi 1.7.6's public `start(profile_threads=True)`, CPU
clock, `convert2pstats`, and cleanup APIs, verified in the installed package.
See [Yappi clocks](https://github.com/sumerc/yappi/blob/master/doc/clock_types.md)
and the [API reference](https://github.com/sumerc/yappi/blob/master/doc/api.md).
