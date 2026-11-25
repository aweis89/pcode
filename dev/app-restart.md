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

If the move or restart fails: the error is shown, and the process still exits
the same way. A failed local move does not resume the interrupted turn or run
queued messages here on the way out; the session is saved, and the usual exit
line says how to continue it.

Exit status: 0 in every case above (a normal exit, with the usual
"keeps running in the background" or "Continue with" line printed last).

A second `SIGUSR2` while one is being handled is ignored.
