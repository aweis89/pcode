"""Discover assistant assets as context, rather than asking the model to do it."""

from dataclasses import dataclass, field

from pydantic_ai_harness.repo_context import AgentContextInventory, RepoContext

# Harness 0.31 exposes no public scanner; reuse its metadata-only scan so the
# inventory stays consistent with the upstream tool. Covered by integration tests.
from pydantic_ai_harness.repo_context._inventory import scan_assets


@dataclass
class AutomaticRepoContext(RepoContext):
    """Preserve instruction loading while providing a per-run asset snapshot."""

    expose_inventory_tool: bool = field(default=False, init=False)
    _inventory_context: str | None = field(default=None, init=False, repr=False, compare=False)

    _inventory: AgentContextInventory | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def startup_summary(self) -> list[str]:
        """Describe the startup snapshot without displaying instruction bodies."""
        self.get_instructions()
        files = self._files() if self.autoload_instructions else []
        lines = []
        if files:
            lines.append(
                "Loaded repository instructions: "
                + ", ".join(self._label(file.path) for file in files)
            )
        assert self._inventory is not None
        if self._inventory.roots:
            lines.append(
                "Discovered configuration (paths only; contents not loaded, hooks not run):"
            )
            for root in self._inventory.roots:
                lines.append(f"  {root.root}/")
                lines.extend(f"    Skill: {path}" for path in root.skills)
                lines.extend(f"    Agent: {path}" for path in root.agents)
                if root.settings:
                    lines.append(f"    Settings/hooks: {root.settings}")
        return lines

    def get_instructions(self) -> str:
        instructions = super().get_instructions()
        if self._inventory_context is None:
            inventory = scan_assets(self.workspace_dir, self.asset_roots)
            # Absent directories are not useful prompt content.
            inventory.roots = [root for root in inventory.roots if root.exists]
            self._inventory = inventory
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
