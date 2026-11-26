# Plan: Session Inspector, Live Diffs, and Sidebar File Changes

Status: Draft plan (ready for implementation).

## Goal

Give Pcode.app a native right-hand **Session Inspector** for watching live session artifacts alongside the conversation, starting with **Changes (Diffs)** and extending cleanly to **Agents** and **Commands**, plus nested file changes in the session sidebar.

The core UI split:

- **Right-side Inspector (live / reference):** Things you watch or reference while steering the model — **Changes**, **Agents**, and **Commands**.
- **Popups / overlays (deferred):** Transactional or navigational views (`/tree`, `/config`, `/inspect`, `/copy`) stay out of scope here. See [Deferred](#deferred) for notes on `/tree`.

---

## 1. Layout & Responsive Geometry

The Inspector is a **full-height right column** spanning the entire pane height (to the right of both the native transcript and the docked composer / bottom shell `⌘J`). Diffs and logs get maximum vertical space, and the composer stays visually anchored under the conversation it controls.

```text
┌─ Sidebar ──────┬─ Transcript ───────────────────┬─ [Changes (4)] [Agents (1●)] [Cmds] ─┐
│ Open Sessions  │                                │  This Session ▾  All files (4) ▾ [⤢] │
│ ▾ fix-auth     │  Edited Auth.swift             │  ─────────────────────────────────── │
│     M Auth.sw… │  [View diff →]                 │  ▾ Auth.swift              +30 -8    │
│     M Login.s… │                                │    41   let token = ...              │
│   + 2 more…    │  Running pytest…               │  - 42 - return nil                   │
│                │                                │  + 42 + return refresh(token)        │
│ ▸ refactor-ui  │ ┌────────────────────────────┐ │  ─────────────────────────────────── │
│                │ │ composer                   │ │  ▸ Login.swift             +12 -4    │
└────────────────┴─┴────────────────────────────┴─┴──────────────────────────────────────┘
```

- **Wide window (≥ 1150pt):** True resizable side-by-side split (`Transcript | Inspector`). Because the transcript is native AppKit (`PCodeLiveTranscriptPane`), it reflows cleanly as the divider moves.
- **Narrow window (< 1150pt):** Automatically degrades into a slide-over drawer covering the transcript (matching `PCodeDrawer`'s overlay behavior, with `Esc` to dismiss) so neither pane gets crushed into an unreadable strip.
- **Temporary expand (`⤢` / `⌘⇧F` / double-click divider):** Expands the Inspector across the full main pane width for focused code review (enabling side-by-side diff), then snaps back to split.
- **Per-session state:** Each open session remembers whether its Inspector is open, its active tab, its split width, and its scroll position. Switching sessions in the sidebar swaps both transcript and Inspector together.
- **Coexistence with `/btw`:** `/btw` stays its own overlay drawer (`PCodeDrawer`) since it shares the composer and keyboard focus; the Inspector is strictly for observing and referencing session artifacts.

---

## 2. Changes Tab (Live Diff Viewer)

### Scopes

A compact scope switcher sits at the top of the Changes tab:

1. **This Session (default):** All edits made by *this* session, built from `PCodeHostEvent.editPreview` and `PCodeHostEvent.editCompleted` (plus history catch-up on session load/resume).
   - **Why default:** Accurately isolates what *this* session changed when multiple agents share a directory without worktrees, continues to work after commits, and works in non-git folders.
   - **Ordering:** Sorted by **most recently modified** so the latest edit is always at the top.
   - **Live streaming feel:** As `editPreview` and `editCompleted` events arrive, new or updated file diffs stream/animate in smoothly (pure Swift UI reveal/pulse on newly added hunks) so you can watch edits land in real time.
2. **Working Tree:** Async `git status --porcelain` + `git diff` + untracked files in the session's directory, refreshed on tool-completion events (debounced).
3. **Branch vs. Base:** Async `git diff <merge-base>...HEAD` + working tree when the session is on a feature branch or worktree.
4. **Single Edit:** Auto-selected when clicking a specific edit card in the transcript (shows a pill like `Turn 4 · Auth.swift` with a 1-click button back to `This Session`).

### Rendering & Ergonomics

- **Continuous scroll + sticky file headers (no 4th column):** All changed files render in one scrollable view with sticky collapsible headers (`▾ Auth.swift   +30 -8`), paired with a compact file-jump dropdown (`All files (4) ▾`) and `[` / `]` next/prev file keys. Clicking a file in the sidebar or dropdown scrolls smoothly to its header.
- **Unified by default; Side-by-side when expanded:** Split mode uses unified diff; expanded (`⤢`) or wide Inspector offers a 1-click unified / side-by-side toggle.
- **Syntax + intraline highlighting:** Reuses `PCodeSyntaxHighlighter` (Prism tokens + light/dark `PCodeSyntaxPalette`) and layers **intraline word-diff highlights** (stronger tint on the exact changed tokens).
- **Expandable context:** "Show 20 more lines" control between hunks when the underlying file is readable on disk.
- **Non-yanking updates:** If the user is scrolled down reading another file when a new edit lands, do not hijack scroll — pulse a subtle updated dot on the newly touched file. Offer an optional **"Follow"** pin button in the header to auto-scroll to each incoming edit.
- **Read-only in v1 + actions:**
  - **Reference in prompt (`⌘L` or floating button on selection):** Inserts `@path/to/file.swift:L42-L58` into the composer and focuses it.
  - **Open in Editor (`⌘O`):** Opens the file (at the selected/first-changed line when supported) via `$VISUAL` / `$EDITOR` (with line-number flags for `code`, `cursor`, `zed`, `subl`, `nvim`/`vim`), falling back to `NSWorkspace.shared.open`.
  - **Copy path:** Copies the relative (or full) file path.
  - Inline git mutations (revert/stage hunk) are deferred to avoid desyncing a running agent's file context.

---

## 3. Sidebar Nested File Changes

- **Default on**, toggleable via View menu and a small sidebar header toggle (`Show Changed Files`, persisted in `UserDefaults`).
- **Backed by the same per-session `This Session` changes store** (with a subtle `+N -M` stat badge on the session row).
- **Anti-bloat cap:** Shows up to **5–6 most-recently-edited files** under an expanded open session, followed by a `+ K more…` overflow row that opens the Changes tab.
- **Row layout:** Tiny `M`/`A`/`D` indicator + basename (with muted parent dir only when basenames collide) + right-aligned `+N -M`.
- **Click behavior:** Clicking a nested file under *any* open session switches to that session and opens the Inspector to **Changes**, scrolled to that file.

---

## 4. Agents & Commands Tabs (Interactive)

Both tabs reuse live events `PCodeHostClient` already receives (`toolStarted`, `toolSummary`, `commandOutput`, `parentCallID`, `isBackgroundJob`, `exitCode`):

### Agents Tab

- Lists delegated sub-agents (`delegate_task`) for the session with status badge (running pulse / waiting / done / failed), purpose, model, and elapsed time.
- Expanding or selecting a worker shows its live nested tool calls and final Markdown report (reusing transcript block views).
- **Interactive controls:**
  - **Stop** a running background worker.
  - **Send message** or **Answer `ask_parent` question** inline (routed through host RPC to `manage_worker`).
- Clicking a sub-agent block in the main transcript opens the Inspector to **Agents** focused on that worker.

### Commands Tab

- Lists running and recent shell tool calls and background jobs with command text, status/exit code, and elapsed time.
- Selecting a command tails its live `commandOutput` in a monospace scrollable view.
- **Interactive controls:**
  - **Kill** a running background job (reusing the existing `stop_jobs` RPC in `src/pcode/host.py`).
  - **Copy output** and **Reference output in prompt (`⌘L`)**.
- Clicking a running or completed command card in the transcript opens the Inspector to **Commands** focused on that call.

---

## 5. Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `⌘I` (or `⌘\`) | Toggle Inspector split |
| `⌥⌘1` / `⌥⌘2` / `⌥⌘3` | Switch Inspector tab (Changes / Agents / Commands) |
| `⌘⇧F` (or divider double-click) | Expand / restore Inspector full-width |
| `[` / `]` | Previous / next changed file (when Changes has focus) |
| `⌘L` | Reference selected diff lines or command output in composer |
| `⌘O` | Open active file (at line) in external editor |
| `Esc` | Dismiss Inspector when in narrow overlay mode |

*(Exact bindings will be checked against existing Ghostty/workspace shortcuts during Phase 0 so nothing collides.)*

---

## 6. Deferred (Out of Scope for This Plan)

- **Native `/tree` UI:** Rather than a modal popup, `/tree` may work better as a transcript overlay or secondary navigation sidebar that auto-scrolls the main transcript to the selected turn with inline actions like "Resume here" / "Fork". Holding off until the Inspector ships.
- **Other terminal popups:** `/config`, `/inspect`, `/copy`.
- **Inline git write actions:** Revert hunk, stage hunk, commit from UI.

---

## 7. Implementation & Parallelization Plan

Most of the work lives in **new, self-contained Swift files** under `app/macos/PCode/`. The main merge-conflict hotspot is `PCodeWorkspace.swift`, so we isolate interface contracts in Phase 0, fan out 4 independent tracks in parallel in Phase 1, and integrate into `PCodeWorkspace.swift` once in Phase 2.

```text
Phase 0 (Sequential: ~1 small PR)
  Freeze shared Swift models/protocols + host RPC gaps
       │
       ├──► Track A: Diff Parser, Intraline Highlighter & Diff Views (Swift)
       ├──► Track B: Session Changes Store & Git Diff Provider (Swift)
       ├──► Track C: Agents & Commands Tab Views (Swift)
       └──► Track D: External Editor + Composer Injection + Host RPCs (Swift + Python)
       │
       ▼
Phase 2 (Sequential: Workspace Integration)
  Inspector Split container, PCodeWorkspace wiring, Sidebar nested files, Transcript links
       │
       ▼
Phase 3 (Sequential: Verification & Polish)
  Swift unit tests, ui-loop visual verification, make app
```

### Phase 0 — Contracts & Plumbing (Sequential, Small)

Goal: Define the shared data contracts so Tracks A–D compile and test independently without touching `PCodeWorkspace.swift`.

- Add `PCodeInspectorModels.swift`:
  - `PCodeInspectorTab` (`changes`, `agents`, `commands`)
  - `PCodeDiffScope` (`session`, `workingTree`, `branch`, `singleEdit(callID: String)`)
  - `PCodeFileDiff`, `PCodeDiffHunk`, `PCodeDiffLine`, `PCodeIntralineSpan`
  - `PCodeInspectorAgentItem`, `PCodeInspectorCommandItem`
  - Callback protocol (`PCodeInspectorActions`: `insertIntoComposer(_:)`, `openInEditor(path:line:)`, `stopJob(_:)`, `manageWorker(...)`)
- Audit `src/pcode/host.py` for any missing RPCs needed by Track C/D (worker message/answer/stop, composer text insertion).

### Phase 1 — Parallel Feature Tracks (4 Independent Worktrees)

These four tracks touch disjoint files and can run concurrently:

- **Track A — Diff Engine & Views**
  - Files: `PCodeDiffParser.swift`, `PCodeDiffView.swift`, `Tests/PCodeDiffParserTests.swift`
  - Unified patch parser, intraline word-diff algorithm, `PCodeSyntaxHighlighter` integration, continuous-scroll view with sticky file headers, unified + side-by-side layouts, streaming reveal animation for incoming edits, line-range selection for `⌘L`, expandable context.
- **Track B — Changes Data Sources**
  - Files: `PCodeSessionChangesStore.swift`, `PCodeGitDiffProvider.swift`, `Tests/PCodeSessionChangesStoreTests.swift`
  - Folds `PCodeHostEvent.editPreview` / `editCompleted` / `history` into ordered per-session `PCodeFileDiff` state (merging/stacking repeated edits to the same file, tracking `lastModifiedAt` and `isStreaming`).
  - Async `git status --porcelain` / `git diff` / merge-base provider for Working Tree and Branch scopes, with debounced refresh and cancellation.
- **Track C — Agents & Commands Views**
  - Files: `PCodeInspectorAgentsView.swift`, `PCodeInspectorCommandsView.swift`, `Tests/PCodeInspectorTabsTests.swift`
  - State aggregators folding `toolStarted`/`toolSummary`/`commandOutput` into live agent & command models.
  - SwiftUI/AppKit views for sub-agent list, nested tool calls, Markdown report, Stop/Message/Answer controls; command list, live output tail, Kill/Copy/Reference controls.
- **Track D — External Editor, Composer Injection & Host RPCs**
  - Files: `PCodeExternalEditor.swift`, `Tests/PCodeExternalEditorTests.swift`, plus any small RPC additions in `src/pcode/host.py` & `tests/test_host.py`
  - `$VISUAL`/`$EDITOR` resolution with editor-specific line flags + `NSWorkspace` fallback.
  - Composer text-injection path (so `⌘L` inserts `@path:L10-L24` cleanly into the active session's editor without clobbering existing draft text).
  - Host RPCs for worker stop / message / answer if not already exposed.

### Phase 2 — Workspace & Sidebar Integration (Sequential)

Once Tracks A–D are merged:

- Add `PCodeInspectorSplit.swift`: responsive full-height right split (side-by-side ≥ 1150pt, slide-over drawer < 1150pt), segmented tab bar with live badges, expand toggle (`⤢`), keyboard shortcuts.
- Wire into `PCodeWorkspace.swift`:
  - Per-session `PCodeInspectorState` and `PCodeSessionChangesStore` attached to each open pane's host observer.
  - Nested file changes under open sessions in the left sidebar (5–6 file cap + `+ K more…` + View toggle).
  - Click-to-inspector links on edit, sub-agent, and command cards in `PCodeTranscriptBlocks.swift` / `PCodeLiveTranscriptPane.swift`.

### Phase 3 — Verification & Polish

- Run `swift test` in `app/macos` and targeted Python tests for any host RPC changes.
- Visual verification via `ui-loop` in `Pcode.app`:
  - Wide split vs. narrow overlay resize behavior and divider drag.
  - Live streaming edit animation in `This Session` mode.
  - Sidebar nested files + overflow row + cross-session click jump.
  - `⌘L` line-range reference into composer and `⌘O` open in editor.
  - Live Agents & Commands tabs with running/completed items.
- Merge to `master` and install with `make app` from the main checkout.
