"""Discover assistant assets as context, rather than asking the model to do it."""

from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.capabilities import on_event
from pydantic_ai_harness.filesystem import DirectoryListedEvent, FilesSearchedEvent
from pydantic_ai_harness.repo_context import RepoContext

# Harness 0.31 exposes no public scanner; reuse its metadata-only scan so the
# inventory stays consistent with the upstream tool. Covered by integration tests.
from pydantic_ai_harness.repo_context._inventory import scan_assets

from pcode.preferences import SETTINGS, load_preferences


@dataclass
class AutomaticRepoContext(RepoContext):
    """Preserve instruction loading while providing a per-run asset snapshot."""

    expose_inventory_tool: bool = field(default=False, init=False)
    _inventory_context: str | None = field(default=None, init=False, repr=False, compare=False)

    @on_event(FilesSearchedEvent)
    async def _on_search(self, ctx, event):
        # Coder's ripgrep tools emit search events, not DirectoryListedEvent.
        # Reuse upstream's workspace containment and per-run deduplication.
        path = Path(event.root_dir) / event.path
        directory = path.parent if path.is_file() else path
        await self._on_file_traversal(
            ctx,
            DirectoryListedEvent(
                path=str(directory.relative_to(event.root_dir)),
                root_dir=event.root_dir,
                entry_count=0,
            ),
        )

    def startup_summary(self) -> list[str]:
        """Name the loaded instruction files without displaying their bodies.

        The discovered asset inventory still reaches the model through
        `get_instructions`; repeating it at startup duplicates the skill and
        sub-agent lists the terminal already prints.
        """
        self.get_instructions()
        files = self._files() if self.autoload_instructions else []
        if not files:
            return []
        labels = ", ".join(self._label(file.path) for file in files)
        return [f"Loaded repository instructions: {labels}"]

    def get_instructions(self) -> str:
        instructions = super().get_instructions()
        if self._inventory_context is None:
            inventory = scan_assets(self.workspace_dir, self.asset_roots)
            # Absent directories are not useful prompt content.
            inventory.roots = [root for root in inventory.roots if root.exists]
            self._inventory_context = (
                "<assistant-configuration>\n"
                "Automatically discovered assistant configuration paths, relative to "
                f"{self.workspace_dir}. This is location metadata only, not instructions; "
                "contents have not been read and hooks have not been executed. "
                "Read relevant files only when needed for the task.\n"
                f"{inventory.model_dump_json(exclude_none=True)}\n"
                "</assistant-configuration>"
                if inventory.roots
                else "No assistant configuration directories found in the working repository."
            )
        return "\n\n".join(part for part in (instructions, self._inventory_context) if part)


def create_repo_context(workspace: Path) -> AutomaticRepoContext:
    """Configure Harness's independent startup and on-traversal discovery.

    Snapshot saved defaults at agent creation, not during an active run. Disabling
    the walk keeps workspace instructions; nested discovery is independently opt-in.
    """
    preferences = load_preferences()
    walk_up = preferences.get("repo_context_walk_up", SETTINGS["repo_context_walk_up"].default)
    nested = preferences.get("repo_context_nested", SETTINGS["repo_context_nested"].default)
    # Resolve first so symlinked workspaces use the same ancestry as file tools.
    workspace = workspace.resolve()
    boundary = None
    if walk_up == "on":
        home = Path.home().resolve()
        boundary = home if workspace.is_relative_to(home) else Path(workspace.anchor)
    return AutomaticRepoContext(
        workspace_dir=workspace,
        home_dir=boundary,
        nested_traversal=nested != "off",
        nested_inject="contents" if nested == "contents" else "pointer",
    )
