Build a polished, highly customizable terminal agent harness in Python, using **Pydantic AI / Pydantic AI Harness for the agent runtime**, **prompt_toolkit for interactive input**, and **Rich for permanent terminal output**.

The primary UX goal is a coding-agent experience in the spirit of Pi, Claude Code, and Codex CLI, while remaining highly extensible and provider-neutral.

The most important architectural requirement is:

> **The conversation transcript must live in the terminal's normal scrollback buffer.**

This is specifically intended to work naturally with tmux copy mode, tmux search, ordinary terminal selection, scrollback, shell history workflows, and terminal resizing.

Do **not** build the primary conversation UI as a full-screen TUI, Textual app, virtual scrollback widget, or application-owned viewport.

Research the latest Pydantic AI, Pydantic AI Harness, prompt_toolkit, and Rich APIs before implementing. Prefer current public APIs and idiomatic integrations over assumptions in this document.

## Core architecture

Use three cleanly separated layers:

```text
Terminal UI
    │
    │ events / commands
    ▼
Agent Runtime
    │
    ▼
Pydantic AI + Pydantic AI Harness
```

### 1. Agent layer

Use Pydantic AI as the underlying agent framework.

Where useful, use the official Pydantic AI Harness capabilities rather than reimplementing coding-agent primitives such as:

* filesystem access
* shell execution
* repository context
* subagents
* planning
* context management
* MCP
* skills
* memory
* web/search capabilities

Keep the actual Pydantic `Agent` definition independent of the terminal UI.

It should eventually be possible to run the same agent through:

* this terminal interface
* a headless runner
* tests
* `clai`, where compatible
* potentially an HTTP/API frontend

Do not couple UI components directly to Pydantic AI's internal event classes if an intermediate application event model would provide a cleaner boundary.

### 2. Runtime layer

Create an application runtime that sits between Pydantic AI and the UI.

Define a small, explicit event protocol along the lines of:

```python
TextDelta
ThinkingDelta

ToolStarted
ToolOutput
ToolFinished
ToolFailed

SubagentStarted
SubagentOutput
SubagentFinished

UsageUpdated

ApprovalRequested

RunStarted
RunFinished
RunFailed
RunCancelled

ContextUpdated
```

These are examples, not a mandatory exact schema. Adapt them after examining current Pydantic AI/Harness event APIs.

The runtime should translate framework-specific events into stable application events.

Similarly, UI actions should become runtime commands rather than manipulating the agent directly.

The runtime should eventually be capable of supporting:

* cancellation
* steering/follow-up input
* queued prompts
* sessions
* context compaction
* model switching
* agent switching
* subagents
* approvals
* checkpoints / rewind
* usage and context tracking

Do not try to implement every advanced feature in the first pass. Design the boundaries so they can be added cleanly.

## Terminal architecture

This is the most important part.

Use:

```text
Rich
    → permanent transcript output

prompt_toolkit
    → mutable interactive input UI
```

### Permanent vs mutable pixels

Follow this rule strictly:

> **Rich owns permanent pixels. prompt_toolkit owns mutable pixels.**

Once an assistant message, completed tool invocation, completed diff, error, or other transcript item is finalized, print it normally into stdout so it becomes ordinary terminal history.

Do not repaint historical transcript content.

Do not maintain an application-level scrollback implementation.

Do not reserve a terminal scroll region and manually keep a persistent bottom bar pinned using DECSTBM or custom cursor gymnastics.

Do not build our own terminal emulator compatibility layer.

Avoid custom ANSI cursor positioning whenever prompt_toolkit or Rich can perform the job.

This requirement exists specifically to avoid the class of terminal bugs involving:

* incorrect word wrapping
* cursor desynchronization
* ghost rows
* stale redraws
* deferred terminal wrapping
* resize corruption
* emoji / wide-character cell width
* different VT implementations
* tmux interaction problems

### prompt_toolkit responsibility

Use prompt_toolkit for the interactive prompt.

It should eventually support:

* multiline editing
* cursor movement
* history
* reverse history search
* completion
* slash-command completion
* file/path completion
* model/agent completion
* syntax-aware or visually pleasant editing where practical
* configurable keybindings
* bracketed paste
* Ctrl+C behavior
* Ctrl+D / exit handling
* editing while the agent is running where feasible
* queued prompts or steering later

Keep prompt_toolkit in **non-full-screen mode during normal conversation**.

The transcript above the prompt must remain ordinary terminal output.

### Rich responsibility

Use Rich for finalized output:

* Markdown
* syntax highlighted code
* tool-call summaries
* diffs
* tables
* errors
* warnings
* usage information
* subagent results
* headings/separators

Prefer restrained, readable output over elaborate decoration.

Make Rich rendering theme-aware and terminal-friendly.

Do not create output wider than the available terminal width.

Wrapping must prefer word boundaries.

## Streaming

Streaming needs special care because streamed text is not permanent until it has stabilized.

Find a clean way to display streaming assistant output without turning the entire transcript into an application-controlled viewport.

Possible approaches include prompt_toolkit's output/application primitives or a small temporary live region.

The important invariant is:

> Once a response or logical output block is finalized, commit it normally to terminal scrollback and stop repainting it.

Avoid continually rewriting arbitrary historical lines.

The prompt must remain usable and visually stable while streaming.

Correctness and terminal stability are more important than fancy typewriter animations.

Do not implement artificial token-by-token smoothing unless there is a compelling reason later.

## Temporary interactive UIs

Some interactions genuinely benefit from menus or richer temporary interfaces.

Examples:

* `/model`
* `/agent`
* `/session`
* `/resume`
* `/rewind`
* `/mcp`
* `/skills`
* diff approval
* permission approval
* subagent selection
* settings

For these, it is acceptable to temporarily use a prompt_toolkit full-screen application or modal-style interface.

The lifecycle should be approximately:

```text
normal transcript mode
        ↓
suspend ordinary prompt
        ↓
temporary picker / TUI
        ↓
close temporary UI
        ↓
restore normal terminal
        ↓
continue transcript mode
```

The temporary UI must not destroy or replace tmux scrollback.

Keep these components separate from the normal conversation renderer.

## Commands

Implement a command registry rather than a giant command dispatcher.

Start with a small useful set, likely:

```text
/help
/model
/agent
/clear
/new
/sessions
/resume
/compact
/context
/tools
/mcp
/quit
```

Exact commands can evolve.

Commands should expose metadata such as:

* name
* aliases
* description
* argument completion
* handler
* whether allowed during a run

This registry should drive both execution and slash-command completion.

Plugins should eventually be able to add commands.

## Extensibility

High customization is a primary objective.

Design explicit extension points for:

* tools
* Pydantic AI capabilities
* MCP servers
* commands
* renderers
* runtime event listeners
* hooks
* agent definitions
* model providers
* subagents
* approval policies
* keybindings
* themes
* session storage
* context processors

Prefer ordinary Python interfaces/protocols and registration mechanisms over a heavy plugin framework initially.

Avoid global monkey patches where possible.

Avoid requiring forks of dependencies.

## MCP

MCP should integrate primarily at the agent/runtime layer, not in the terminal UI.

Do not dump every MCP tool schema permanently into UI-specific code.

Use Pydantic AI/Harness's current MCP abstractions where appropriate.

Keep room for:

* per-agent MCP configuration
* lazy MCP activation
* MCP management commands
* tool filtering
* permissions
* dedicated MCP context-gathering subagents if useful later

## Subagents

Treat subagents as first-class runtime entities.

The UI should be able to display concise lifecycle information such as:

```text
● researcher started
● researcher: searching repository...
✓ researcher finished
```

Do not create a permanently pinned subagent panel in the initial architecture.

Completed subagent activity should become normal transcript content.

We can add optional richer displays later.

## Approvals and safety

Design an approval abstraction for operations that may require confirmation.

Examples:

* shell commands
* file writes
* destructive actions
* MCP tools
* external side effects

Approval policy should be configurable independently of individual tool implementations.

Support future modes such as:

```text
ask
allow
deny
allow-read-only
project-scoped
```

Interactive approval dialogs can use temporary prompt_toolkit UI.

## Session model

Keep session persistence separate from presentation.

A session should capture enough structured information to reconstruct agent state, not merely terminal text.

Plan for:

* creating sessions
* listing sessions
* resuming
* branching/forking
* checkpoints
* rewind
* model metadata
* token/context metadata

Use a sane durable representation and avoid insecure arbitrary deserialization.

Do not overbuild this in the first implementation milestone.

## Checkpoints and rewind

Design the runtime so `/rewind` can eventually restore a previous conversation/checkpoint cleanly.

Do not implement rewind as terminal manipulation.

It should operate on structured agent/session state.

Possible future semantics:

```text
/rewind
    show recent checkpoints

/rewind 2
    rewind two turns

/fork
    branch current session
```

Investigate what Pydantic AI/Harness already exposes before inventing our own machinery.

## Project structure

Choose a clean package structure based on actual implementation needs.

A rough direction could be:

```text
src/<project>/
    agent/
        definitions.py
        capabilities.py

    runtime/
        runtime.py
        events.py
        commands.py
        sessions.py
        approvals.py

    ui/
        app.py
        prompt.py
        output.py
        streaming.py
        completion.py
        keybindings.py

        pickers/
            model.py
            agent.py
            session.py

    commands/
        registry.py
        builtin.py

    config/
        models.py
        loader.py

    main.py
```

Do not follow this mechanically if research suggests a better structure.

The important boundary is:

```text
agent framework
≠ runtime
≠ terminal presentation
```

## Testing

Terminal correctness needs strong automated testing.

Build the project so the majority of behavior can be tested without launching a real interactive terminal.

Test:

* runtime event conversion
* command registry
* prompt completion
* keybindings where practical
* session behavior
* rendering functions
* terminal-width handling
* Unicode/wide characters
* word wrapping
* resize behavior
* interruption/cancellation
* temporary picker lifecycle

Use prompt_toolkit's testing facilities where appropriate.

Add a small PTY-based integration suite for behaviors that cannot be validated headlessly.

Important regression cases should include:

* narrow terminals
* terminal resize during streaming
* tmux execution
* long words
* emojis / double-width characters
* multiline input
* Ctrl+C during generation
* large tool output
* Markdown tables
* code blocks
* nested tool/subagent activity

Do not rely solely on snapshot tests of ANSI strings.

## tmux

tmux compatibility is a first-class requirement.

Validate manually and preferably in integration tests that:

* normal conversation appears in tmux scrollback
* tmux copy-mode works
* tmux search sees earlier assistant output
* terminal resizing inside tmux behaves correctly
* exiting the application leaves previous transcript visible
* temporary full-screen pickers restore correctly
* mouse/selection behavior is not unnecessarily hijacked
* no persistent scroll-region state is left behind

## Initial implementation sequence

Work incrementally.

### Milestone 1

Build the smallest complete vertical slice:

```text
Pydantic AI agent
    ↓
runtime event adapter
    ↓
streaming terminal output
    ↓
prompt_toolkit input
    ↓
Rich finalized transcript
```

It should support:

* one configurable model
* interactive prompt
* streaming assistant response
* basic tool execution
* Ctrl+C cancellation
* Markdown output
* ordinary tmux scrollback
* clean exit

This milestone should already feel good to use.

### Milestone 2

Add:

* command registry
* `/model`
* `/help`
* `/context`
* command completion
* persistent input history
* configurable keybindings

### Milestone 3

Add:

* sessions
* `/resume`
* `/new`
* context compaction
* MCP
* skills
* subagent lifecycle rendering

### Milestone 4

Add richer interaction where it provides real value:

* approval UI
* model/session pickers
* checkpoints
* `/rewind`
* session forks
* interactive diffs
* plugin API

Do not prematurely build all milestones before validating Milestone 1 interactively.

## Design principles

Optimize for:

1. terminal correctness
2. tmux-native behavior
3. low architectural coupling
4. extensibility
5. provider neutrality
6. understandable code
7. testability
8. minimal bespoke terminal machinery

Prefer composition over inheritance.

Prefer public dependency APIs.

Prefer simple event-driven boundaries.

Prefer a small implementation we understand over a feature-rich framework abstraction we have to fight.

Use Pydantic models where they add real value, particularly configuration and structured runtime data, but do not model every internal object merely because Pydantic is available.

Avoid unnecessary abstraction until at least two consumers justify it.

## Non-goals

Do not initially build:

* an IDE
* a file-tree-centric interface
* a permanent split-pane TUI
* a virtual terminal
* a custom Markdown engine
* a custom line editor
* a custom ANSI layout framework
* a persistent manually managed bottom bar
* a proprietary agent loop duplicating Pydantic AI
* a huge plugin framework
* elaborate animations

The terminal, tmux, prompt_toolkit, Rich, Pydantic AI, and Pydantic AI Harness should each do the jobs they are already good at.

## Final goal

The finished experience should feel like a native terminal coding agent:

```text
$ agent

Using GPT-5.6 Sol · code-agent

> investigate why our deployment rollout is hanging

I'll inspect the rollout configuration and controller state.

● Read deploy/rollout.yaml
● Search "progressDeadlineSeconds"
● Shell kubectl get rollout ...

The rollout is waiting because...

> fix it

● Edit deploy/rollout.yaml
  + progressDeadlineSeconds: 900

Updated. The change...

> _
```

Everything above the current input prompt should simply be normal terminal history that tmux understands.

At the same time, internally the project should be structured well enough that advanced features like MCP, subagents, rewind, sessions, approvals, plugins, and alternative frontends can grow without requiring a redesign.

Start by researching the latest relevant APIs and inspecting similar open-source implementations for useful ideas. Then create a concise implementation plan, make the architectural decisions that need to be made, and proceed with Milestone 1. Do not stop merely because this document leaves implementation details open; investigate, choose sensible solutions, and adapt as you learn.
