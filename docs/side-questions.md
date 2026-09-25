# Side questions (`/btw`)

`/btw QUESTION` asks a question about what the model is doing **while it is doing
it**. The question runs as a second, parallel request against the same context;
the turn in flight is not interrupted, cancelled, or steered, and the question
never enters the conversation.

```text
❯ refactor the parser and make the tests pass
  ⠋ Running shell · uv run pytest
❯ /btw why did you pick a recursive descent parser?
  ◈ Side question asked beside the conversation…
  ◈ Side answer ready (why did you pick a recursive descent parser?). Opening it.
```

While a side question runs it has its own spinner row in the live panel above
the editor, below the turn's, in the muted shade: the question, what the model
is doing for it, and how long it has taken. Up to three show; more fold into a
count that `/btw` expands.

The viewer opens by itself as soon as an answer is ready, since the point of a
side question is reading the answer while the turn is still running. A bare
`/btw` opens it at any other time. While an answer is still arriving, the popup
streams it.

- **↑ / ↓:** move through the questions, or scroll the answer when it has focus.
- **Tab:** switch between the question list and the answer pane.
- **c:** copy the selected answer to the clipboard as raw markdown (also
  works mid-stream, taking what has arrived so far).
- **Ctrl+K:** stop every running side question, keeping the records.
- **Enter / Escape / Ctrl+C:** close the popup and restore the editor draft.

To keep the answers out of the way until you ask for them, turn auto-open off:

```
pcode config set btw_auto_open off   # Default on; applies immediately
```

With it off, a ready answer only prints its transcript notice and waits for a
bare `/btw`. Auto-open never interrupts a popup or command already using the
terminal — it queues behind it — and it does nothing when the viewer is already
open, because an open viewer follows new answers on its own.

The footer counts side questions that are `running` and answers that are
`ready` (settled but not yet opened). Ctrl+C at the prompt stops running side
questions only when nothing else is in flight, so an interrupt aimed at the turn
never throws away the side question as well.

## What a side question can and cannot do

A side question runs on the **conversation's own agent**: the same model,
instructions, tool definitions, enabled MCP servers and model settings as the
turn beside it (unless you [choose another model](#choosing-the-model)). Its requests therefore start with the exact prefix the
conversation has already sent, so the provider's prompt cache covers everything
but the question itself. The instructions telling the model it is answering a
side question travel inside the question message for the same reason; putting
them in the system prompt would change the prefix and re-bill the whole
conversation.

Tools work as they do in a turn. The model can read files, search the web, use
MCP tools, and run shell commands or edit files if the question calls for it,
with the usual permission checks. The exceptions are the tools that change the
conversation itself: the plan (`write_plan`, `add_task`, `update_task_status`
and the rest; `read_plan` is fine) and delegation (`delegate_task`,
`integrate_task`, `discard_task`). They stay declared, since removing them would
break the cache, but a call is refused with a result telling the model the tool
is unavailable in a side question, and it carries on answering. A side question
is a question; if the answer implies work, send it as a normal message.

Nothing about a side question joins the conversation:

- no conversation-tree node, so `/tree`, `/resend` and forking never see it;
- no session-journal record, so resuming the session does not replay it;
- no change to the model's history, so the next real turn is unaffected.

What it does share is the context it was asked against and the session's token
totals: the request really happened, so `/status` counts it.

## Choosing the model

Leading `$` words pick the model a side question runs on; the rest of the line
is the question.

```text
❯ /btw $anthropic:claude-sonnet-5 is this migration safe?
❯ /btw $meridian:claude-opus-5-5 $openai-codex:gpt-6-astra second opinions on the plan?
```

Typing `$` in a `/btw` line completes model names from the same catalog as the
`/model` picker, matching any part of the name (`$opus` finds
`anthropic:claude-opus-…`). It works for each `$` word in the leading run; once
the question starts, `$` is ordinary text, and it never completes in a normal
prompt or `!` shell mode.

- **No `$`:** the conversation's model, sharing its prompt cache as described
  above. Naming the conversation's own model is the same thing.
- **Another model:** the same agent, tools, history and framing, but the
  model's own settings: its defaults and its saved `/effort`, never the
  conversation model's. It starts **without the conversation's cache**, so its
  first request pays full price for the whole conversation. It also runs under
  a conversation id of its own, so a Meridian session for the main conversation
  is never moved by it.
- **Several models:** one side question per model, started together. Each has
  its own row, answer, error and `errors.log` entry, and one failing does not
  affect the others. Rows, the viewer list, and the ready notices carry a short
  model label (the name without its provider, unless two would look the same).
  Repeated models collapse to one, and at most 4 models can be named at once.

A name that cannot be resolved (unknown provider, missing credentials or SDK)
fails the whole `/btw` command before any side question starts. `/btw $MODEL`
with no question is an error too.

## Which context it sees

A side question is asked against the newest **settled** prefix of the request in
flight — what the model is working with right now, not the state before the turn
started. Providers reject a history whose tool calls have no results, so the
prefix stops at the last point where the conversation was balanced: a tool call
that has not returned yet is not included, and neither is the assistant text
streaming beside it.

Side questions are bounded: 12 model requests and 300 seconds each. They are not
retried, and they do not survive exiting pcode.

When one fails or times out, the viewer shows the error and the traceback is
appended to the session's `errors.log`, the same file failed turns write to,
under a `run aside <id>` header with the question. A stopped side question
writes nothing.

## Parallel work and `/tree`

`/tree` opens while a turn is running, but only to read: the header says
`read-only while working` and Enter does not switch context. Switching context
replaces the history the running turn is about to write back, so a checkout
during a turn would silently lose. Browse the tree now, fork when the turn ends,
and use `/btw` to ask about a branch in the meantime.

Several side questions can run at once, each with the context available when it
was asked. What is **not** yet possible is running two conversation turns in
parallel — from `/tree` or anywhere else. One turn at a time is assumed
throughout: the conversation tree records events against a single "recording"
cursor, the runtime keeps one history, plan store, request checkpoint and shell,
the session journal is a single append-only stream, and the transcript and
activity widget present one stream of tool and text events. `/btw` is the useful
slice that fits those constraints, because its answer lands in its own surface
instead of the conversation and it cannot touch the plan or start workers.
A shell command it runs is still a real job in the shared job list, though, so
prefer questions that only need reading while a turn is editing the same files.
