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

Phase 0 is done: the spike and an opt-in demo (`pcode --host`, `/switch`,
`/stop`, `--attach`, `--hosts`). See [what works](background-sessions.md#what-works-in-a-hosted-session).
In the demo the terminal still runs all session logic itself, against
`RemoteRuntime`, a stand-in that forwards `stream` to the host. That is why a
dozen commands are refused there, and it is what the refactor replaces.

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

## Phases

### Phase 1: controller in-process (no behavior change)

Move what `run_async`'s nested functions do today into `pcode.controller.SessionController`, with
`PreviewApp` as its view. Land it in slices, each keeping `make test-all` green.

- [ ] Define `SessionView`: the transcript and activity calls session logic makes, with
      serializable arguments only.
- [x] Prompt queue, generations, and its live-panel mirror: `controller.PromptQueue`
- [ ] Send modes (steering, queue, interrupt) and `submit`
- [ ] `clear_queue`, `cancel`, busy accounting
- [ ] Turn loop (`consume`), `run_live`, shell `!commands` (`run_shell`)
- [ ] History tasks (`/compact`, side-thread summary) and MCP tasks
- [ ] Job watching and wake-ups (`watch_jobs`)
- [ ] Side questions (`/btw`) lifecycle

### Phase 2: session commands and popup data in the controller

- [ ] `/compact`, `/autocompact`, `/resend`, `/new`
- [ ] `/model`, `/effort` (the picker stays in the terminal; the choice is an intent)
- [ ] `/mcp`, skill MCP enabling, `/login`, `/logout`
- [ ] `/tree` navigation, `/btw` and its viewer, `/workers`, `/jobs`, `/tools`, `/links`, `/diffs`
- [ ] `/reload` and extension commands (their handlers run where the extension is loaded)
- [ ] `/worktree`

### Phase 3: controller over the socket

- [ ] Protocol v2 carrying intents, queries, and updates; the host runs the controller
- [ ] Terminal-side client replaces `RemoteRuntime`; delete `HOSTED_COMMANDS`
- [ ] Run the app-level tests against both the in-process and socket transports

### Phase 4: background only

- [ ] Every interactive launch spawns a host; drop `--host`, `--no-host`, `session_host`
- [ ] `--print` decision (in-process or host)
- [ ] Merge the docs into [Sessions and recovery](sessions.md)

### Small items (any time)

- [x] macOS notifications (OSC 9) when a background session finishes, fails, or needs
      input; one notification per event even with several terminals open. "Needs
      input" waits for something that asks for it (none of pcode's tools do yet).
- [x] "Finished, not yet seen" state; sort it first in `/switch`; count it in the footer
- [x] `/switch -` for the previous session (a key is still open)
- [x] Idle stop (`session_host_idle_minutes`, default 60)
- [x] `/restart`: stop the host and resume the same session on current code
- [x] Mark hosts running older code in `/switch` and `--hosts`; `--stop-hosts all|stale`
- [x] `/stop` asks about merging the worktree in the terminal, as a local exit does
- [x] Tab progress (OSC 9;4) while a turn runs (any session, local or hosted)
- [ ] Background job wake-ups in hosts (falls out of Phase 1)

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
- `make test-all` before merging anything that touches the turn loop, the footer, or the prompt.
- End to end with the real model: `pcode --host`, a one-line prompt, `/switch new PROMPT`,
  Ctrl+D, `--hosts`, `--attach`, `/stop`. The spike scripts that did this live outside the
  repository, in the untracked `tmp/host-spike-e1d54951/`.
