# Background sessions: plan and status

Working notes for moving every interactive session into a session host. The
user-facing page is [Background sessions](background-sessions.md); this one
tracks where the work stands and what is left. Update the checklists in the
same commit as the change.

## Decisions

- Session logic moves out of the terminal UI into a controller that runs in the
  host. The terminal renders and sends intents; it holds no conversation.
- Once commands work in a host, background is the only interactive mode:
  `--host`, `--no-host`, and `session_host` go away. `--print` may stay in-process.
- Finished, failed, or waiting background sessions raise a macOS notification
  through the terminal (OSC 9, which Ghostty shows by default).
- A host with no terminal attached, no turn running, and no running jobs stops
  itself after an idle timeout. The journal keeps the conversation; `/resume`
  brings it back.

## Status

All four phases are done: every interactive session with a model runs in a
host. `SessionController` owns the whole session (queues, send modes, turns,
commands, MCP, side questions, jobs) and runs in the host behind `HostView`;
the terminal attaches over protocol 2 with a `RemoteController` and runs only
its own commands (`TERMINAL_COMMANDS`). `--no-host` and `--print` still run a
controller in-process, with `PreviewApp` as its view.

The app-level tests run both ways: `make test` in-process, `make test-socket`
with each session moved into a host over a real socket (`tests/socket_transport.py`).
Running them hosted found and fixed: a resumed conversation that never reached a
terminal attached while the host loaded it (`pcode --continue`, `/restart`); a
hung context-metadata request that kept the host from starting its loops; Ctrl+C
during host startup not dropping typed-ahead prompts and commands, and terminal
commands typed then (`/theme`) not running; side questions not showing until their
first streamed word, nor as summarized or merged; `/tools` not listing the calls
still running.

Driving the real terminal against a real model (a private tmux, the user's settings
copied, `tmp/host-spike-e1d54951/ui_drive.py`) checked every popup over a live host:
`/status`, `/model` and Ctrl+L, `/btw $MODEL` completion and the viewer opening
itself, `/jobs` reading the host's jobs and stopping one, `/tree`, `/switch new`,
Ctrl+^ both ways (vi normal mode too), Ctrl+D then `--attach`, `/restart`, `/stop`.
It found three more: an answer read in the viewer showed as ready again after
switching back (the host now keeps `read`); `/restart` drew the conversation under
"Resumed …" instead of its own note; and stopping an idle host told its terminals
"Run cancelled".

Left: merge the user docs into [Sessions and recovery](sessions.md).

## Target architecture

```text
terminal (pcode)                          session host (python -m pcode.host)
┌───────────────────────────┐  intents   ┌───────────────────────────────────┐
│ editor · footer · popups  │ ─────────▶ │ SessionController                 │
│ Transcript (scrollback)   │            │   queue · send modes · steering   │
│ Activity (live panel)     │ ◀───────── │   turns (AgentRuntime) · cancel   │
│ view commands (/theme …)  │  updates   │   compaction · model · effort     │
└───────────────────────────┘            │   MCP · /btw · jobs and wake-ups  │
                                         │   worktree · idle stop · notices  │
                                         └───────────────────────────────────┘
```

- **Intents** (terminal to host): `submit(text, mode)`, `cancel(policy)`,
  `command(name, argument)`, `query(name, args)` for popup data.
- **Updates** (host to terminal): runtime events, notices, the running turn's
  start and end, and a `state` snapshot for the live panel and footer (busy,
  queued messages, status, prompt row, model, effort, context, MCP, jobs).
- The controller talks to a `SessionView` interface. In-process the terminal
  implements it directly; in a host it serializes to the socket and the
  terminal applies each update to its own `Transcript` and `Activity`.

Decisions made while building it:

- Event rendering stays in the terminal. The controller calls `turn_started`,
  `turn_event` (one runtime event), `turn_retry`, `turn_ended`; the terminal owns
  the event-derived panel state (tools, workers, plan, previews). The controller
  owns the session fields of `Activity` (busy, status, queue, prompt row, jobs
  rows); in a host those assignments are mirrored to the terminals in order.
- The terminal calls the controller in three ways only: fire-and-forget intents
  (`submit`, `command`, `cancel`), and awaited `call(name, ...)` for popup data
  and small immediate actions (stop a job from the jobs browser). Anything else
  is a command. The controller calls the view fire-and-forget, except dialogs,
  which it awaits (`await view.choose_model(options)`).
- Every slash command goes through the controller's command queue, so commands
  and prompts keep today's order. A command the controller does not own is
  handed back to the terminal that sent it and awaited. A hosted terminal runs
  its own commands (`TERMINAL_COMMANDS`) at once instead, so `/quit` and `/switch`
  work while the host is busy or gone; they no longer wait behind the session's.
- Hosted, `/quit` detaches and `/resume` opens the other session in a host of its
  own, so neither is refused while a turn runs, as they are in-process.

## Phases

### Phase 1: controller in-process (no behavior change)

Move what `run_async`'s nested functions do today into `pcode.controller.SessionController`, with
`PreviewApp` as its view. Land it in slices, each keeping `make test-all` green.

- [x] Define `SessionView`: the transcript and activity calls session logic makes, with
      serializable arguments only. Started: it lists what the controller calls so far and
      grows with each slice. The controller reaches what has not moved yet (shell-wait
      policy, side questions, redraw) through callables passed to it.
- [x] Prompt queue, generations, and its live-panel mirror: `controller.PromptQueue`
- [x] Send modes (steering, queue, interrupt) and `submit`: `SessionController.submit`,
      `command`
- [x] `clear_queue`, `cancel`, busy accounting (`refresh_busy`, `turn_ended`)
- [x] Turn loop (`consume`), `run_live`, shell `!commands` (`run_shell`), job
      exit reporting and wake prompts. `PreviewApp.run_live` stays as a thin
      wrapper for tests that drive one turn.
- [x] History tasks (`/compact`, side-thread summary) and MCP tasks
- [x] The command loop (`consume_commands`); the terminal runs what it hands back
- [x] Job watching and wake-ups (`watch_jobs`), and `/jobs`
- [x] Side questions (`/btw`) lifecycle

### Phase 2: session commands and popup data in the controller

- [x] `/compact`, `/autocompact`, `/resend` (`/new` goes with the session lifecycle)
- [x] `/model`, `/effort` (the picker is a view method the controller awaits)
- [x] `/mcp` and skill MCP enabling
- [x] `/login`, `/logout`, `/new`, in-process `/resume`
- [x] `/tree` navigation, `/btw` and its viewer, `/jobs`. `/workers`, `/tools`, `/links`,
      `/diffs` stay in the terminal: they read what it rendered.
- [x] `/reload`, `/extensions`, extension commands and skills
- [x] `/worktree`

### Phase 3: controller over the socket

How it works (protocol 2, `pcode.rpc` on the host socket):

- The host runs a `SessionController` whose view is `HostView`. Fire-and-forget
  view calls go to every attached terminal and into the attach buffer; popups and
  handed-back commands are requests to the terminal whose command is running.
- The host's `Activity` is a `MirroredActivity`: an assignment to a session field
  sends `state(changes)`. `session_changed()` makes the host send
  `session_state(...)` (model, effort, context, session directory, commands and
  their completions) when it differs from what was last sent.
- The terminal's controller is a `RemoteController`: intents are notifications,
  data is `await query(name, ...)`. Its `runtime` is a `HostedSession` that reads
  the host's journal with `SessionJournal.read`, so replay, `/tree`, `/tools`,
  `/diffs` and `/links` work from the same files.
- Attaching: `hello`, then `welcome` with the session state, the session fields
  of `Activity`, the side questions, the journal offset where the running turn
  began, and every view call since then. The terminal replays the journal to that
  offset and applies the calls.


- [x] Protocol v2 carrying intents, queries, and updates; the host runs the controller
- [x] Terminal-side client replaces `RemoteRuntime`; delete `HOSTED_COMMANDS`
- [x] Run the app-level tests against both the in-process and socket transports
      (`make test-socket`; tests whose hosted behavior differs on purpose are marked
      `in_process`)

### Phase 4: background only

- [x] Every interactive launch with a model spawns a host (`session_host` now defaults
      on). `--no-host` stays as the escape hatch; the suite defaults it off for tests
      that drive `main()` in-process.
- [x] `--print` stays in-process: one prompt, no terminal to come back to
- [ ] Merge the docs into [Sessions and recovery](sessions.md)

### Small items (any time)

- [x] macOS notifications (OSC 9) when a background session finishes, fails, or needs
      input; one notification per event even with several terminals open. "Needs
      input" waits for something that asks for it (none of pcode's tools do yet).
- [x] "Finished, not yet seen" state; sort it first in `/switch`; count it in the footer
- [x] `/switch -` for the previous session, and Ctrl+^ for it
- [x] Idle stop (`session_host_idle_minutes`, default 60)
- [x] `/restart`: stop the host and resume the same session on current code
- [x] Mark hosts running older code in `/switch` and `--hosts`; `--stop-hosts all|stale`
- [x] `/stop` asks about merging the worktree in the terminal, as a local exit does
- [x] Tab progress (OSC 9;4) while a turn runs (any session, local or hosted)
- [x] Background job wake-ups in hosts (the host runs `watch_jobs`)

## Traps found so far

- Unix socket paths are capped at 104 bytes on macOS. pytest's `tmp_path` is too long, so
  tests put hosts under a short `/tmp` directory (`PCODE_HOST_DIR`).
- asyncio's stream line limit defaults to 64 KiB; one tool result is larger. Readers use
  `LINE_LIMIT`.
- `EditPreview` has a `kind` field, so events are encoded as `{"kind", "fields"}`, not spread
  beside the name the way the journal writes them.
- A running turn is partly in the journal (settled steps) and partly not (streaming text,
  previews, live command output). A snapshot reads the journal only up to where the turn
  began (`SavedSession.records(end)`) and sends the turn from the host's buffer.
- Cancel-then-prompt races: the host must not clear a queue while a cancelled turn unwinds,
  and a terminal must detach its inbox only on the `turn_finished` of the prompt it sent.
- A `Mock` runtime in tests answers every attribute, so feature checks on the runtime use
  `is True` rather than truthiness.
- `/tmp` resolves to `/private/tmp`; compare resolved workspaces.
- Hosts inherit the environment of the terminal that spawned them. One shared daemon could
  not: `os.environ` is per process, and pcode reads credentials from it.

## Checking changes

- `tests/test_session_host.py`: wire format, host logic, and the terminal switching away
  mid-turn and back, all in-process over a real socket.
- `make test-socket`: the whole fast suite with each session in a host
  (`--transport socket`). A test asserting on the terminal's `activity` right after
  its fake runtime acts has to wait for the value there; see `AGENTS.md`.
- `make test-all` (fast suite both ways, then tmux) before merging anything that touches
  the turn loop, the footer, or the prompt.
- End to end with the real model: `pcode --host`, a one-line prompt, `/switch new PROMPT`,
  Ctrl+D, `--hosts`, `--attach`, `/stop`. The spike scripts that did this live outside the
  repository, in the untracked `tmp/host-spike-e1d54951/`.
