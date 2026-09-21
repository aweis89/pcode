# Conversation tree navigation

Use `/tree` while the agent is idle to browse and fork the current conversation.
This follows the user/assistant selection model of
[pi-coding-agent's session tree](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/tree.md).

The picker is laid out like `/resume`: the tree on the left, and on the right a
Conversation pane showing the full branch through the selected row, root to
leaf. Moving the selection scrolls that pane so the selected prompt or response
sits at the top, with what led to it above and what followed below. Below a
selected row the pane follows the active branch where the tree forks.

- **↑ / ↓:** move through the tree. PageUp / PageDown and Ctrl+U / Ctrl+D scroll
  through longer trees.
- **Tab:** focus the Conversation pane; ↑ / ↓, PageUp / PageDown, and Ctrl+U /
  Ctrl+D scroll it. Tab again returns to the tree.
- **Enter on a user prompt:** restore the context **before** that turn and place
  its original prompt in the editor. Edit and send it to create another branch.
- **Enter on an assistant response:** restore the context **after** that turn.
  Send a new message to continue from there.
- **Enter on Conversation start:** select empty context within the same session.
- **Escape / Ctrl+C:** close the picker without changing context or the draft.

The picker starts on the active position, marked `← active`. It shows every branch
in depth-first order. Messages on the same path stay aligned; indentation increases
only where the conversation splits into branches, not with every message or turn.
Existing descendants are never deleted when you select an ancestor or submit a
different continuation. For example:

```text
Conversation start
user: Explain the failing test
assistant: The parser rejects empty input
├─ user: Fix the parser
│  assistant: Updated the parser
│  user: Run the tests
│  assistant: Tests pass
└─ user: Instead, change the test
   assistant: Updated the test ← active
```

Select either assistant response to return to that branch. Select either user
prompt to try another version. `/tree` itself makes no model requests and runs no
tools; it does not generate summaries of the branch you leave.

## What changes when navigating

The model's history, task plan, and saved-session transcript/tool replay follow the
selected path, not the most recently written branch. `/tools` also shows calls on
the selected path. Earlier terminal scrollback remains visible; a branch separator
and recent selected-path messages clarify which context is now active. Usage and
completed-turn counters remain **session totals**, including other branches.

Navigation operates at **turn boundaries**, not individual tool calls. A turn
includes the user prompt and the entire assistant/tool loop. Failed or interrupted
turns are marked and continue from their last safe checkpoint (or their ancestor
when no checkpoint exists). Pending tool calls are never treated as completed.
Interrupted tool effects remain in the diagnostic ledger; navigation neither
replays tools nor marks unresolved effects as completed.

**Switching context does not undo file edits, shell commands, network requests, or
other tool effects.** All branches share the current workspace. If you need to
restore files, use your version-control workflow separately. MCP enablement stays
as currently configured; navigation does not reconnect disabled servers.

`/tree` is unavailable while a turn is running or prompts are queued. Cancel with
Ctrl+C or wait for completion before navigating.

## Persistence

With normal session saving, branches and the selected position survive restart,
even if you navigate without sending another message. `/resume` chooses a saved
session; `/tree` navigates within it. Existing saved sessions appear as a linear
tree automatically. Structured histories (including tool calls/results) continue
to use Harness's native safe checkpoints. Tree links and selection events live in
the private, append-only session journal.

With `--no-save`, the tree exists only in memory and disappears on exit. Browsing
an empty conversation does not create session files. `/new` starts a separate tree
and keeps the previous saved session available through `/resume`. The offline
preview has no model conversation to navigate.
