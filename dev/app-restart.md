# Restarting the terminal app around pcode (SIGUSR2)

A tool that restarts the terminal emulator (or reloads a tmux/kitty layout) can
keep every pcode session alive across it: send each pcode terminal process
`SIGUSR2`, wait for it to exit, then relaunch `pcode -c <session id>` in the new
pane. That command attaches to the host now running the session.

## Contract

Signal: `SIGUSR2` to the interactive terminal process (`pcode`, not
`pcode.host`; hosts ignore it). From the very start of `pcode.cli.main` the
signal never kills the process: until the editor runs it is only noted
(`cli.remember_app_restart`) and acted on once it does; `PreviewApp.run_async`
handles it through the end of its exit cleanup, then puts the noting handler
back for the rest of the exit. Non-interactive modes (`--print`, `config`)
just note and ignore it.

What it does (`PreviewApp.request_app_restart` / `_app_restart`):

1. Waits for startup to finish (the agent runtime, or the connection to a host).
2. Then, under the same lock `/detach` and `/restart` take:
   - **Session running in this terminal, saved:** moves it into a new host on
     the pcode installed now, as `/detach` does. Mid-turn is fine: the turn
     stops at its checkpoint (compaction finishes first, side questions are
     cancelled), queued messages carry over in order, and the host resumes
     with `/continue`. Background jobs move with it.
   - **Session not saved yet (no first prompt), or a UI preview:** nothing to move.
   - **Hosted, host on older code:** restarts the host (`/restart`, interrupting
     a busy one) and attaches to the successor.
   - **Hosted, host current:** nothing to do.
3. Exits. Popups and nested prompts are cancelled first, so they cannot hold the
   exit. Any host is left running (detached, never stopped).

When it exits: only after the successor host has written its entry and this
terminal has connected to it (`wait_for_host`), so `pcode -c <id>` run after the
exit finds it registered and listening rather than starting a copy.

How long that takes: usually a second or two, but there is no fixed bound.
A turn that is compacting its context is waited for rather than thrown away,
and a hosted restart allows its host up to 300 s to let go (`INTERRUPT_TIMEOUT`)
and its successor as long again to come up; a first prompt sent just before the
signal gets up to 10 s to start saving the session. So the exit can take more
than 300 s. Callers should wait for the process to exit, not SIGKILL it after a
short timeout: a kill mid-move can leave the session stopped with its turn
interrupted (`pcode -c <id>` then `/continue` recovers it) and drop messages
that were queued.

What moves with the session: the interrupted turn (continued with `/continue`
once the new host is connected, never before, so a host that fails to come up
never runs it alongside this process), queued messages and slash commands,
sent to the host in the order they were typed, the moment it is connected and
before anything else in the move can fail. Queued commands the terminal runs
itself (`/help`, `/attach`, `/new PROMPT`, ...) are not run: the exit would
cancel them, so the warning names them instead. A command already running when
the signal lands is not waited for (a popup could hold it forever). A message whose turn had not reached the journal
yet goes back to the front of the queue rather than being continued. Commands
queued in a host that restarts mid-turn are not run by its successor, which
names them instead; should the successor fail to start, the error the restart
reports names what was queued.

If the move or restart fails: the error is shown, and the process still exits
the same way. A failed local move does not resume the interrupted turn or run
queued messages here on the way out; the session is saved, and the usual exit
line says how to continue it.

Exit status: 0 in every case above (a normal exit, with the usual
"keeps running in the background" or "Continue with" line printed last).

A second `SIGUSR2` while one is being handled is ignored.
