# Working in a repository

## Repository instructions (`AGENTS.md` / `CLAUDE.md`)

Both the main agent and explorer load workspace-local `CLAUDE.md` and `AGENTS.md`.
Two independent settings control additional discovery:

```sh
pcode config set repo_context_walk_up off   # Only workspace-local files at startup
pcode config set repo_context_walk_up on    # Also inherit ancestors (default)
pcode config set repo_context_nested pointer  # Notify the agent of nested files
pcode config set repo_context_nested contents # Inject nested instruction contents
pcode config set repo_context_nested off      # Disable nested discovery (default)
```

Use the same commands with `/config` inside pcode. These are saved global defaults,
read when an agent is created (including on launch, resume, or model replacement),
not live toggles for an existing agent. Restart pcode to reliably apply changes.
`pcode config unset KEY` restores that setting's built-in default. Disabling both
still loads workspace-local instructions; neither setting restricts explicit file
reads or removes instructions from saved conversation history.

**Upward walk:** When enabled, for a workspace under your home folder, discovery
stops at home (inclusive); elsewhere, it stops at the filesystem root. Paths are
resolved before walking, so symlinked workspaces inherit from their real ancestors.
A `.git` directory does not stop the walk. Instructions are loaded broadest-first,
workspace-last. Within each directory, `CLAUDE.md` comes before `AGENTS.md`; both
load when their contents differ. Harness deduplicates files by resolved path and
content, keeping the first occurrence. The startup banner lists the files actually
loaded without printing their bodies. Files are cached within each agent run and
reread for the next run.

**Nested discovery:** This works with the upward walk either on or off. After a
successful `read_file`, `list_files`, or `grep` tool call within the workspace,
pcode's Harness context adapter checks the accessed file's directory or the
selected search directory. `pointer` adds a
note telling the agent to read its instruction file if relevant; `contents` adds
the instruction body to the conversation. These notes do not change the startup
instruction prefix. Each directory is surfaced at most once per run. Unlike
startup loading, Harness selects only the first matching filename in that
directory (`CLAUDE.md` before `AGENTS.md`). It does not recursively scan the tree
or check intervening directories when jumping directly to a deeper file. Shell
commands, writes, and edits do not trigger this discovery.

The `.claude`, `.agents`, `.codex`, and `.grok` asset inventory remains
workspace-local and metadata-only; it does not load asset bodies or execute hooks.
It goes to the model rather than the startup banner, which already lists the
skill commands. Discovered instructions are sent to the selected model, so review
inherited and nested files when working in a shared directory tree.

## Skills as slash commands

Every `SKILL.md` found under those asset roots becomes a command, so a skill can
be invoked deliberately instead of hoping the model notices it. A skill in
`.claude/skills/cache-report/SKILL.md` is named after its directory:

```
/skill:cache-report check the last session
```

The command sends a normal message asking the model to read that file and follow
it, with anything you type after the command appended. It is queued like a typed
message, so send mode, steering, and Ctrl+C behave as usual. The skill body is
not preloaded into the prompt; the model reads the file with its own tools.

`skill_dirs` adds directories searched after the asset roots, so skills can live
outside the workspace:

```sh
pcode config set skill_dirs '~/.agents/skills:.agents/skills'   # default
pcode config set skill_dirs '~/.agents/skills:/opt/team/skills' # share a checkout
pcode config set skill_dirs ''                                  # asset roots only
```

Entries are separated by `:`, `~` expands to your home directory, and a relative
entry resolves against the workspace. Each directory is searched recursively for
`SKILL.md`. Asset roots are scanned first and a duplicated name keeps the first
match, so a workspace skill shadows a user-level one. Skills found outside the
workspace are referenced by absolute path.

Naming follows `skill_commands`: `prefix` gives `/skill:NAME` (default), `bare`
gives `/NAME`, `both` registers the bare name as an alias of the prefixed one,
and `off` registers nothing. A bare name that collides with a built-in command is
dropped, and the built-in wins. Discovery happens at launch, so add a skill (or
change this setting) and restart to pick it up. The startup banner lists the
commands that were registered. Only the frontmatter `description` is read at
launch, to label the completion menu.

## One git worktree per session

Several sessions editing one checkout trample each other: one session's
`git checkout` or `stash` eats another's uncommitted edits. pcode can give each
session its own worktree and make that the workspace, so file tools, the shell,
repository instructions, and the saved session all point there. The model needs
no instructions and relative paths cannot land in the mainline by mistake.

```sh
pcode --worktree                      # .worktrees/pcode-<session-id-prefix>, branch of the same name
pcode --worktree fix-thing            # .worktrees/pcode-fix-thing
pcode config project set worktree on  # default for this repository (committed in .pcode/)
pcode config set worktree on          # default for every git repository
pcode --no-worktree                   # stay in the current checkout this once
```

The worktree lives under `.worktrees/` in the primary checkout (added to
`.git/info/exclude`, so `git status` stays clean without touching `.gitignore`)
and branches from the mainline's current branch, reusing a branch of that name if
one exists. Session worktrees and their branches are always prefixed `pcode-`, so
`git worktree list` and `git branch` show which ones pcode made; `make worktree`
style invocations of `python -m pcode.worktree` use the name as given. Starting pcode inside an existing worktree, outside git, or with
`--continue` never creates another one. Resuming a session (`pcode -c`, or
`pcode -C .worktrees/NAME -c` from elsewhere) lands back in its worktree because
the workspace is what the session saved.

Git cannot install dependencies or copy untracked config, so after checkout pcode
runs two optional scripts inside the new worktree, each with `PCODE_MAIN`,
`PCODE_WORKTREE`, and `PCODE_BRANCH` set:

| Script | Runs |
| --- | --- |
| `~/.config/pcode/worktree-setup` | always (for what every repo needs: `direnv allow`, copying `.envrc`) |
| `<repo>/.pcode/worktree-setup` | only in a trusted repository (launch prompt, or `project_extensions on`), since it is code shipped with the repo |

An executable script runs directly (give it a shebang); anything else runs
through `sh`. A non-zero exit aborts the launch and removes the half-made
worktree. This repository's own script symlinks the shared `tmp/` cache and runs
`uv sync`, because the editable install records an absolute `src/` path and a
shared `.venv` would silently import the other checkout.

Inside the session, `/worktree` shows the branch and what is unmerged,
`/worktree merge` merges the mainline branch into the worktree (so conflicts are
resolved there, never in the mainline checkout) and then fast-forwards the
mainline, `/worktree finish` does that and then removes the worktree and its
branch and quits, `/worktree remove` deletes the directory once it is merged
and clean, and `/worktree list` shows every worktree. Nothing is ever forced.

`/worktree clean` sweeps up the leftovers: every other worktree of the
repository with nothing uncommitted, nothing untracked, and nothing the mainline
branch lacks is removed along with its branch. Anything else is listed with the
reason it was kept, so the command cannot lose work. It runs from the mainline
checkout too (`make worktree-clean`), which is usually where the pile is
visible. A worktree someone locked with `git worktree lock` is skipped.

When a merge stops on conflicts it says which files, and `/worktree resolve`
hands them to the model: it gets the branch names and the conflicted paths and
is asked to resolve each so both sides survive, run the tests, and commit the
merge (never abort it). Run `/worktree merge` or `finish` again afterwards. The
merge is never started for you, and the model is never asked without you typing
the command; a conflict at exit prints the resume command and that same hint.

Leaving a session tidies its own worktree (one pcode made, prefixed `pcode-`;
hand-made ones are only reported):

| State on exit | What happens |
| --- | --- |
| Untouched: clean, nothing unmerged | Removed with its branch, no question. A session that never had a turn is deleted too; otherwise it is repointed at the mainline so `pcode -c` still works. |
| Committed but unmerged | `worktree_exit`: `ask` (default) prompts `Merge and remove the worktree? [Y/n]`; `merge` does it silently; `keep` leaves it. A merge that conflicts or cannot fast-forward keeps everything and prints how to resume. |
| Uncommitted changes | Kept, with the resume command. Committing on your behalf at exit is not pcode's call. |

`--print` has nobody to ask, so it only does the untouched cleanup. Merging
never pushes; push from the mainline when you are ready.
