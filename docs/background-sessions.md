# Background sessions

A session can run in a *session host*: a headless pcode process that owns the
conversation (the agent, its tools, the journal) while the terminal only draws
it. The terminal can then leave, switch to another session, or close, and the
work carries on. This is a preview: the core loop works, and a number of
commands are not wired through yet (see [what works](#what-works-in-a-hosted-session)).

```sh
pcode --host                     # start this session in a host and attach to it
pcode config set session_host on # make that the default
pcode --hosts                    # list running hosts
pcode --attach                   # reattach to the newest host in this repository
pcode --attach 3f9c              # ...or to one by host or session ID prefix
```

Inside a hosted session:

- `/switch` opens a picker over every running host, with what each one is doing.
  Enter shows that session in this terminal; `n` (or Ctrl+N) starts a new one;
  `x`, pressed twice, stops one. A turn you switch away from keeps running.
- `/switch HOST` goes straight to one by host or session ID prefix.
- `/switch new` starts a new session and switches to it. `/switch new PROMPT`
  starts one working on PROMPT and leaves it in the background; this terminal
  stays where it is.
- `/resume` resumes a saved conversation in a host of its own, or shows it where
  it is already running.
- `/stop` ends this session's host and quits. Plain quitting (Ctrl+D, `/quit`, or
  closing the terminal) only detaches.

The footer counts the other running sessions and how many are working, and a
note appears when one of them finishes a turn.

## How it works

Each host is its own process, started by the terminal that asked for it, so it
inherits that terminal's environment (direnv credentials, `PATH`, tool
versions) exactly as a local session would. One session crashing or hanging
does not touch the others. A host listens on a Unix socket; a terminal that
attaches is sent the conversation so far and then every event as the turn
produces it, so switching to a session mid-turn picks the turn up where it is,
streaming text and running commands included.

A new host started with the `worktree` setting on makes its own worktree, the
same as a local session, and `/switch new` starts from the main checkout so the
new session never shares yours. A host tidies its worktree when it stops, as a
local session does on exit, without asking: unmerged work is kept with a note in
the host's log.

Hosts keep running until stopped. Their sockets, status files, and logs live in
`~/.local/state/pcode/hosts/` (`PCODE_HOST_DIR` overrides it; a Unix socket path
is limited to about 100 bytes, so keep it short).

## What works in a hosted session

Prompts, streaming, tools, edits, the plan, steering, Ctrl+C, skills, and
`/status`, `/diffs`, `/help`, `/config`, and the display commands
(`/theme`, `/syntax`, `/redraw`, `/show-*`) all work.

Not yet: `/model`, `/effort`, `/compact`, `/resend`, `/tree`, `/btw`, `/mcp`,
`/jobs`, `/workers`, `/tools`, `/links`, `/new` (use `/switch new`), `/worktree`,
and extension commands. They are refused with a note rather than acting on nothing.
MCP servers marked enabled start in the host with their saved sign-ins; one that
needs a browser sign-in cannot be enabled from a hosted session yet.

A host started by an older pcode keeps running that code until it stops. A
terminal on a different protocol version is refused with a message saying so.
