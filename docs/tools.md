# Tools

## Tool permissions

**Live mode enables actual Coder file edits and shell tools. There is no approval
UI.** pcode does not implement its own permission model and does not use prompt
text as a safety control. Permission management is out of scope: run pcode inside
a sandboxing wrapper (a container, VM, or an OS sandbox such as `sandbox-exec`
or `bwrap`) when you need enforcement, and otherwise use a trusted repository and
a safe working environment.

File tools accept absolute paths anywhere the OS permits, including external
worktrees and temporary directories. Relative paths (including `..`) always use
the selected workspace as their base, even after a shell command changes its
working directory. `list_files` and `grep` default to the workspace and return
workspace-relative paths, including `../` paths for external results. Protected
patterns such as `.git/*`, `.env`, `.env.*`, `*.pem`,
`*.key`, and `**/secrets*` remain read-only through file tools at any depth.

The built-in `worker` sub-agent is general-purpose: it can inspect, edit files,
run commands and tests, and use the same extension tools, native/local web tools,
and currently enabled MCP servers as the main agent. It inherits the repository
instructions, file protections, and extension guardrails, not a separate read-only
policy. Shell commands can read or modify anything the OS allows, including files
protected by file tools. Repository instruction discovery remains scoped to the
selected workspace and its configured ancestors, not every external path the
tools can access.

Use `delegate_task` with `agent_name="worker"` and a self-contained task. Each has
fresh conversation context and its own plan; the parent's conversation is not
copied. An optional `purpose` of a few words labels the delegation in the task
widget and scrollback; the sub-agent never sees it. Recursive delegation is
disabled. Each task counts its own requests apart from the parent's, with no
request cap or execution timeout: its tool calls show under the parent's, and
Ctrl+C cancels it with the turn. There is **no concurrency cap by default**
(`worker_concurrency=0`). Set a positive `worker_concurrency` to
limit built-in workers per session, then `/reload` to apply it. When a cap is set,
additional calls wait for a slot before creating a checkout or running a worker.
Provider limits and machine resources still apply. Specialized extension
delegates keep their own tool configuration rather than automatically gaining
the worker's tools.

### Worker worktrees

Worker isolation is **opt-in**: `worker_isolation` defaults to `off`. Enabling
`worktree` alone still creates session worktrees, but workers keep sharing their
parent's live workspace, including uncommitted files, just as before.

To opt in for this workspace:

```sh
pcode config project set worktree on
pcode config project set worker_isolation on
```

`delegate_task` accepts `workspace_mode="auto"` (the default), `"isolated"`, or
`"shared"`. Auto isolates built-in workers only when **both `worktree=on` and
`worker_isolation=on`** are effective for the active workspace; otherwise it uses
shared mode. Both preferences are checked at each delegation, so no reload is
needed for this gate. A linked session checkout or the CLI's one-launch
`--worktree` override does not enable worker isolation. Explicit `isolated` is
rejected unless both settings are on; a tool argument cannot bypass the opt-in.
Explicit `shared` is always available, including for investigations of uncommitted
files. Shared mode is not read-only; coordinate concurrent edits and Git commands.

Turning `worker_isolation` off stops new isolated delegations. It does not remove
existing task worktrees: their management tools and cleanup protections remain
available so outstanding results can still be integrated or explicitly discarded.

For isolated editing:

```python
delegate_task(
    agent_name="worker",
    task="Implement the parser fix, add tests, and commit the result.",
    workspace_mode="isolated",
)
```

pcode creates a unique `task-<id>` branch and sibling checkout under `.worktrees/`,
starting at the **parent's current commit**, not mainline. The parent must be a
checkout root on a branch, with no tracked changes or Git operation in progress.
Commit a checkpoint first; pcode never stashes or creates hidden commits.
Untracked files are not copied. Existing trusted worktree setup hooks provision
dependencies and local configuration before the worker starts. Setup has its own
15-minute budget; cancellation terminates and reaps its process group rather
than leaving provisioning running in the background.

File tools, shell, repository context, and loaded extensions are rebuilt against
the child checkout. Extension setup runs again with the child's workspace, and
its close hooks run when the child ends. Extensions can distinguish worker setup
with `pcode.is_worker`; shared session resources such as the browser remain owned
by the parent. Enabled runtime MCP tools remain shared services; they are not
worktree sandboxes. Isolated workers have a separate job registry. Job-completion
notices go to that worker at its next model request, not to the parent or other
workers. Remaining child shell jobs are terminated before recording the result.
Worktrees do not isolate ports, databases, credentials, or OS permissions.

The tool returns a persistent artifact containing `task_id`, parent path and
branch, base and result commits, child branch and path, observed dirty state,
status, and the worker's summary. Commit and cleanliness metadata come from Git;
verification claims in the summary remain worker-reported. Cancellation, setup
failure or timeout, or worker failure preserves the checkout and record.
A dead owning process is recognized as a failed task when records are read.

The parent alone has these management tools:

- `list_task_worktrees()` recovers this checkout's task records, including after
  restarting. Records are local JSON files in the shared Git directory, not
  tracked repository files.
- `integrate_task(task_id)` merges a completed result into its recorded **parent
  branch**, never directly into mainline. Both checkouts must be clean, including
  untracked files, and the worker's branch and commit must still match its result.
  A per-parent lock prevents overlapping lifecycle operations. Integration overrides
  squash/no-commit merge options and verifies the result is committed in the parent
  before reporting success. Conflicts remain in the parent: resolve them, commit,
  then retry integration. Review the diff before integration and run combined
  checks afterward.
- `discard_task(task_id, confirm=False)` removes an inactive task's checkout and
  branch. Unintegrated or dirty work requires `confirm=True`, which the agent is
  instructed to use only after explicit user approval. Running tasks cannot be
  discarded. This confirmation argument is not a separate approval UI.

`/worktree list` labels child checkouts with their owner and status.
`/worktree clean` preserves active and unfinished tasks and their parent checkouts. Clean,
successfully integrated children can be cleaned against their parent's history;
mainline need not contain the result yet. Generic worktree merge commands refuse
task branches, so a child cannot accidentally be integrated into mainline. Moving
a task checkout is unsupported: generic lifecycle commands preserve it and explain
how to restore its recorded path before integration or cleanup.

Specialized extension delegates retain shared-workspace semantics even when
`worktree=on`; explicit isolation is only supported for the built-in worker.
Programmatic callers supplying raw extension capability lists must use shared
mode or pass the rebindable `load_extensions(...).capabilities` collection.

Harness is pinned to upstream commit
[`12bce878da99bca61a5d8d798bff0a3bc93bd153`](https://github.com/pydantic/pydantic-ai-harness/commit/12bce878da99bca61a5d8d798bff0a3bc93bd153),
which is newer than the 0.31.0 release. The pin is a direct dependency, so both
`uv sync` and `make install` use it. Coder supplies `read_file`, `write_file`,
`edit_file`, `list_files`, `grep`, and `shell`; pcode adds planning, the worker,
and optional web search. `list_files` and `grep` use the bundled ripgrep and
respect ignore rules. Edits support either one replacement pair or a
`replacements` array, validated before a single write. Anthropic models mangle
that nested array often enough to matter, and the tool retry budget is what
absorbs it: see [retries](sessions.md#retries-and-resend).

`shell` accepts unrestricted commands: treat it as arbitrary code execution as
the invoking user. Files and code returned by tools are sent to the selected
model.

### Shell jobs

Every command runs the same way: detached, under its own supervisor, writing a
combined stdout/stderr log. What varies is only whether the model waits for it.
A command that is still running is a **job** with an id (`j1`, `j2`, …), which
is what makes the rest of the behaviour describable.

- `shell(command)` waits and returns the output with an `[j1 · exit 0 · 1.4s]`
  status line. A command that finished carries no handles: there is nothing
  left to come back to. The exit code is the shell's, so `make test | tail`
  reports `exit 0` when make fails; the model is told to read the output rather
  than trust the code for a piped command.
- `shell(command, background=True)` returns a job handle immediately, for when
  the model has independent work to do. A background or long-running call also
  carries a short `purpose` ("running the end-to-end suite"), because that job
  is shown away from the call that made it: in a later notice, or in the jobs
  row for minutes. Quick foreground commands have no purpose: you read them
  next to their own output, so a label would only repeat the command. The
  purpose leads the Tasks/Tools row, the `/tools` entry and the command block's
  header; the command itself is never dropped, and the `$` line stays literal
  enough to copy and run.
- A wait that ends before the command does — it exceeded `timeout` (270 seconds
  maximum), or you typed a follow-up — returns a job handle instead. **The
  command is not killed.**
- `wait_for_job("j1")` blocks on a job without re-running it.
  `until_output="listening on"` waits for readiness instead of exit, which is
  what a server that never exits needs.
- `job_output("j1")` reads progress, `stop_job("j1")` stops the job and
  everything it started, `list_jobs()` lists them.

Job exits are **delivered**, not polled for: a finished job is reported to the
model before its next request, and printed to the terminal while you are idle.
A job whose result the model already collected with `wait_for_job` or
`job_output` is not reported again: its exit is printed where that call
settled, and the model gets no second notice.
A failed job's notice carries the last 2 KB of its output, so the model can
usually act without a `job_output` round trip. The model is instructed never
to `sleep` waiting for a command.

If the model ends its turn with a background job still running, nothing would
make the next request, so the job's exit **wakes it**: the notice starts a
turn on its own, shown as a `◈ Job finished` row rather than a quoted prompt.
Only jobs the model launched and holds a handle for do this, never one you
stopped or one adopted from an earlier pcode. `pcode config set job_wake off`
turns it off; the model then hears at your next message instead.

While a job runs with nothing waiting on it, a row under the spinner (or under
the editor, while idle) shows it: `⟳ j3 · running the e2e suite · 1m42s`. The
row disappears when the job finishes, including failures. While the model
waits on a job, the wait's own row names it instead, with the job's purpose and
command: `⧗ Wait · 45.2s · j3 · running the e2e suite · make e2e`. Its completion goes
to scrollback at the end of the turn, or immediately while idle, using the
normal `Run` presentation with a `background` label, job id, and elapsed time.
The same `show_commands` and `tool_error_scrollback` settings control captured
output as for foreground commands. Only three live rows fit; additional running
jobs fold into a `… N more jobs (/jobs)` line. `/jobs watch j3` pins the job's
output tail into the command preview, whatever `show_commands` says;
`/jobs unwatch` releases it, and it clears itself when the job ends.

Routine `wait_for_job` and `job_output` results stay in `/tools`, not scrollback:
they inspect an existing job rather than run another command. This includes a
wait that returns early and a read that reports a nonzero command exit. Errors
in the helper itself, such as an unknown job id, still appear in scrollback.

A follow-up you type while the model waits on a command ends the wait, not the
command; the job keeps running under its id. In send mode `steering` the tool
returns the handle and your message rides the very next model request, instead
of sitting in the queue until the wait times out. In `interrupt` mode the turn
is cancelled and the wait abandoned the same way. Ctrl+C means stop working,
so it also stops the command the turn was waiting on. A job the model
explicitly backgrounded survives all of these, because nothing was waiting on
it.

Nothing in the prompt makes the model finish a job before replying, so a
follow-up such as “write the release notes in the meantime” can steer the same
turn into other work while a build continues, without `/btw`.

Jobs outlive the turn, the conversation, and pcode itself. Use
[`/jobs`](commands.md#offline-preview-and-commands) to browse them and read each one's log
(the last 128 KiB, with the same redaction as the preview), and **Ctrl+K** there,
`/jobs stop ID`, or `/jobs stop all` to stop one. A stop is `SIGTERM` to the
job's whole process group, so a server can release its port; whatever is still
there two seconds later gets `SIGKILL`. Logs of finished jobs are dropped on
exit and the oldest are evicted after 50; a running job keeps its log, because
the command is still writing to it. `--no-save` does not disable these logs.

Job logs and a registry record live under `$XDG_STATE_HOME/pcode/jobs/<pid>/`
(default `~/.local/state/pcode/jobs`). When pcode exits with jobs running, the
record stays, and the next pcode to start **adopts** them: they appear in
`/jobs` with fresh ids and `adopted from an earlier pcode`, and can be read,
watched, and stopped like any other. Finished orphans are only logs nobody
will read; they are removed with the dead record.

### Reviewing how the shell was used

`make shell-report` (or `uv run python scripts/shell_report.py <id> -v`) reads
a saved session's step store, which holds the shell results the model actually
saw; the transcript keeps only a display projection of large ones. It counts
calls per tool, background jobs, handles and notices, and flags the patterns
the job model exists to remove: a `sleep` between turns, an `exit 0` over
failure text because a pipe hid the status, a foreground wait that ran to the
ceiling, and `cd <workspace> &&` prefixes or `cat`/`sed`/`grep` reads the file
tools would serve. The default output is counts only; `-v` lists each call with
a clipped, redacted command line.

## Web search

The coder can search the web and read pages. Search and page fetching are
separate because provider-native search returns snippets only; reading
documentation needs the fetch either way. Each picks the best backend available:

| | Search | Fetch a URL |
| --- | --- | --- |
| Model has a native tool (Anthropic, OpenAI; not Meridian) | provider runs it server-side | Anthropic runs it server-side |
| `EXA_API_KEY` set | Exa `web_search` | Exa `get_page` |
| Otherwise | DuckDuckGo `web_search` | HTTP fetch `get_page`, converted to Markdown |

Native tools are billed by the provider per search; the Exa key is read by the
Exa client and never passed to the model. Anthropic encrypts each native result
to the account that ran the search, so resuming a session under a different
login cannot replay them; see [Retries](sessions.md#retries-and-resend) for what
pcode does about it. Search returns up to five results;
page retrieval returns up to 10,000 characters. Queries, URLs, and returned
content go to whichever backend is in use, reach the model, and can be saved in
session history. The worker inherits the same web policy and tools.

```sh
pcode config set web_search local   # Never advertise native tools to the model
pcode config set web_search off     # No web tools at all
pcode config unset web_search       # Back to auto
```

`local` is the escape hatch for an endpoint that rejects server-side tools.
Changes apply on `/reload` or the next launch. This is all one bundled
extension, `web_research`; copy `src/pcode/extensions/web_research.py` to
`~/.config/pcode/extensions/web_research.py` to change backends, limits, or
instructions, or leave its `setup` empty to remove the tools.

## Browser (per conversation)

`/browser launch` gives the model your installed Chrome, through Harness's
[Playwright tools](https://pydantic.dev/docs/ai/harness/playwright/): navigate,
click, type, snapshot, screenshot, and the rest, plus `browser_open()`, which
brings the window to the front, and `browser_tabs()`. When a page needs you to
sign in, the model leaves it on screen and asks; log in there and tell it when
you are done. A `browser` sub-agent shares the same window, so a multi-step
task can run without every page landing in the main context. `/browser off`
quits that Chrome and removes the tools; a fresh pcode starts with them off.

Chrome is started by pcode with a debugging port and its own profile under
`~/.local/state/pcode/chrome`, apart from your everyday one, and driven over CDP.
That is what lets Google and similar sign-in pages accept it: Playwright's own
Chromium launches flagged as automated and they refuse it. The profile persists,
so a site you log in to once stays logged in for later pcode sessions; delete
the directory to forget everything. Set `PCODE_BROWSER_CHROME` to pick the
binary. With no Chrome installed it falls back to Playwright's Chromium,
downloaded on first use.

`/browser attach` joins Chrome, Chromium, or Microsoft Edge you already have
open instead, logins included, so nothing needs signing in to. The model works in a tab of its own,
and `browser_tabs()` shows it what you have open, so "check my email" finds the
mail tab and opens that site rather than guessing. Chrome only exposes itself
once remote debugging is on: the first `/browser attach` opens
`chrome://inspect/#remote-debugging` in your Chrome for you to flip the switch,
then run it again. (Starting Chrome with `--remote-debugging-port` works too.)
pcode searches the standard Chrome, Chromium, and Edge profile directories on
macOS and Linux for `DevToolsActivePort`. For Edge, enable remote debugging in
Edge before attaching; the automatic setup-page shortcut still opens Chrome.
Set `PCODE_BROWSER_CDP_URL` to choose a specific endpoint, or
`PCODE_BROWSER_PORT_FILE` for a custom profile's port file. These overrides take
precedence over discovery. Tab listing uses the browser's CDP connection, so it
also works when the debugging endpoint has no HTTP `/json/list` route. Tab-listing
connection failures are reported as a tool result rather than aborting the turn.
pcode opens its own tab there and closes it on `/browser off`, never quitting
your browser. This is the higher-risk mode: the model can act as every account
that browser is signed in to.

| `/browser …` | Does |
| --- | --- |
| `launch` | Open pcode's own Chrome window, with its own persistent logins |
| `attach` | Join Chrome, Chromium, or Edge you have open, your logins included |
| `off` | Close the browser (or pcode's tab in yours) and remove the tools |
| `status` | Show which browser is in use and where it is |

The window is visible and localhost is reachable, since a dev server is the
usual target. The trade-off of turning it on at all: any page the model reads
can tell it to act with your login, and nothing enforces otherwise beyond you
watching the window. This is the bundled `browser` extension; a user file of the
same name replaces it.

## Code mode (opt-in)

[Code mode](https://pydantic.dev/docs/ai/harness/code-mode/) replaces individual
tool calls with a single sandboxed Python snippet, so the model can fan out
lookups with `asyncio.gather`, filter results, and return only what matters
without a model turn per dependent batch.

```sh
pcode config set code_mode on   # Applies on next launch
pcode config unset code_mode    # Back to plain tool calling
```

Only read-only lookups are sandboxed: `read_file`, `list_files`, `grep`,
`read_tool_result`, `web_search`, and `get_page`. Edits, plan updates, the
persistent shell, and delegation keep issuing their own tool calls, so diffs,
command previews, and the plan panel still show what happened rather than an
opaque snippet that did it. A `run_code` call is displayed by the calls the
snippet makes and its size (`grep · read_file ×2 · 12 lines`); the snippet
itself is visible in the tool-call inspector.

The snippet also streams into the pinned preview box as the model writes it,
titled `Preparing code · not yet run`, in the same place edit diffs and command
output appear. Only complete lines are shown, the box clears the moment the
snippet is dispatched, and the text never enters the transcript — it is a
pending argument, not a result. `/show-edits off` hides it along with edit previews.

Snippets run in the Monty sandbox with no host filesystem or environment of their
own: pcode passes no `mount` or `os_access`, so the only way out is the sandboxed
tools, which enforce the same workspace rules as ever. Harness caps each snippet
at 30 seconds and 256 MiB of heap.
