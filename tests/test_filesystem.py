"""pcode's file tools: unconfined paths on a stable workspace base.

Harness does the work (`root_dir='/'`, relative paths from the workspace); these
pin the contract pcode relies on, through the tools Coder actually registers.
"""

import asyncio
from functools import partial
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace
from pydantic_ai_harness.filesystem import (
    FileChangeRequestEvent,
    FileReadEvent,
    FilesSearchedEvent,
    FileWrittenEvent,
)

from pcode.workspace_filesystem import WorkspaceFileSystem


class Bound:
    """A toolset's direct methods, bound to one local workspace."""

    def __init__(self, workspace: Path, **options):
        self.toolset = WorkspaceFileSystem(**options).get_toolset()
        self.backend = LocalWorkspaceBackend(workspace)

    def __getattr__(self, name):
        return partial(getattr(self.toolset, name), workspace=self.backend)


@pytest.fixture
def paths(tmp_path):
    # Neither hidden ancestors nor external traversal should hide ordinary files.
    workspace = tmp_path / ".hidden" / "workspace"
    external = tmp_path / ".hidden" / "external"
    workspace.mkdir(parents=True)
    external.mkdir()
    (workspace / "inside.txt").write_text("inside marker\n")
    (external / "outside.txt").write_text("outside marker\n")
    (workspace / "alias").symlink_to(external, target_is_directory=True)
    return workspace, external


def test_the_whole_host_is_reachable_from_a_workspace_base(paths):
    workspace, _ = paths
    filesystem = WorkspaceFileSystem()
    assert filesystem.root_dir == "/"
    # Protected-file rules match at any depth, since paths are relative to `/`.
    assert all(pattern.startswith("**/") for pattern in filesystem.read_only_patterns)
    # Copying Coder's capability keeps its settings.
    copied = WorkspaceFileSystem.from_filesystem(WorkspaceFileSystem(max_read_lines=7))
    assert copied.max_read_lines == 7 and copied.root_dir == "/"


def test_external_file_lifecycle_and_conflicts(paths):
    workspace, external = paths
    fs = Bound(workspace)

    async def run():
        target = external / "new" / "sample.txt"
        with pytest.raises(ModelRetry, match="does not exist"):
            await fs.write_file(str(target), "first")
        target.parent.mkdir()
        await fs.write_file(str(target), "first")
        assert "first" in await fs.read_file("../external/new/sample.txt")
        await fs.edit_file(str(target), "first", "second")
        assert "second" in await fs.read_file("alias/new/sample.txt")
        with pytest.raises(ModelRetry, match="[Hh]ash"):
            await fs.write_file(str(target), "lost", expected_hash="stale")
        assert target.read_text() == "second"
        assert "inside marker" in await fs.read_file("inside.txt")

    asyncio.run(run())


@pytest.mark.parametrize("style", ["absolute", "parent", "symlink"])
def test_external_walkers_return_reusable_paths(paths, style):
    workspace, external = paths
    fs = Bound(workspace)
    (external / ".ignored.txt").write_text("outside hidden")
    selected = {"absolute": str(external), "parent": "../external", "symlink": "alias"}[style]

    async def run():
        listed = (await fs.list_files(path=selected)).splitlines()
        (found,) = [line for line in listed if line.endswith("outside.txt")]
        assert not any(".ignored" in line for line in listed)
        # A returned path, relative to the workspace, reads the same file back.
        assert "outside marker" in await fs.read_file(found)
        searched = await fs.grep("outside", path=selected)
        assert searched.splitlines()[0].endswith("outside.txt:1:outside marker")
        # Default traversal stays workspace-local, not host-wide.
        assert "outside.txt" not in await fs.list_files()
        assert await fs.grep("inside") == "inside.txt:1:inside marker"

    asyncio.run(run())


@pytest.mark.parametrize(
    "relative", [".env", ".env.test", ".git/config", "demo.key", "secrets.txt"]
)
@pytest.mark.parametrize("location", ["workspace", "external", "alias"])
@pytest.mark.parametrize("depth", ["", "nested"])
def test_protected_writes_apply_at_any_depth(paths, relative, location, depth):
    workspace, external = paths
    parent = workspace if location == "workspace" else external
    target = parent / depth / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic fixture")
    requested = str(target)
    if location == "alias":
        requested = str(Path("alias") / depth / relative)
    fs = Bound(workspace)

    async def run():
        with pytest.raises(ModelRetry, match="protected"):
            await fs.write_file(requested, "replacement")
        with pytest.raises(ModelRetry, match="protected"):
            await fs.edit_file(requested, "fixture", "replacement")

    asyncio.run(run())
    assert target.read_text() == "synthetic fixture"


def test_a_symlink_to_a_protected_file_is_protected(paths):
    workspace, external = paths
    protected = external / ".env"
    protected.write_text("synthetic fixture")
    (workspace / "innocent.txt").symlink_to(protected)
    fs = Bound(workspace)

    async def run():
        with pytest.raises(ModelRetry, match="protected"):
            await fs.write_file("innocent.txt", "replacement")

    asyncio.run(run())
    assert protected.read_text() == "synthetic fixture"


def test_denied_patterns_apply_to_canonical_external_targets(paths):
    workspace, external = paths
    denied = str(external / "outside.txt").lstrip("/")
    fs = Bound(workspace, denied_patterns=[denied])

    async def run():
        with pytest.raises(ModelRetry, match="denied"):
            await fs.read_file("alias/outside.txt")
        with pytest.raises(ModelRetry, match="denied"):
            await fs.read_file(str(external / "outside.txt"))
        assert "outside.txt" not in await fs.list_files(path=str(external))
        assert "inside marker" in await fs.read_file("inside.txt")

    asyncio.run(run())


def test_symlink_loop_is_recoverable(paths):
    workspace, external = paths
    (external / "loop").symlink_to(external / "loop")
    fs = Bound(workspace)

    async def run():
        with pytest.raises(ModelRetry):
            await fs.read_file(str(external / "loop"))
        assert "loop" not in await fs.list_files(path=str(external))

    asyncio.run(run())


def test_events_locate_their_files_from_the_filesystem_root(paths):
    workspace, external = paths
    toolset = WorkspaceFileSystem().get_toolset()
    events = []

    class Context:
        tool_call_id = "call-1"
        tool_name = None
        tool_manager = None
        workspace = Workspace(LocalWorkspaceBackend(paths[0]))

        async def emit(self, event):
            events.append(event)

    async def run():
        ctx = Context()
        await toolset._read_file_tool(ctx, "alias/outside.txt")
        await toolset._write_file_tool(ctx, str(external / "new.txt"), "new")
        await toolset._grep_tool(ctx, "marker", path=str(external))
        await toolset._read_file_tool(ctx, "inside.txt")
        # A miss is an answer, not traversal evidence: no event.
        assert "not found" in await toolset._read_file_tool(ctx, str(external / "missing"))

    asyncio.run(run())
    # Change requests are pre-mutation events, not successful traversal evidence.
    events = [event for event in events if not isinstance(event, FileChangeRequestEvent)]
    assert [type(e) for e in events] == [
        FileReadEvent,
        FileWrittenEvent,
        FilesSearchedEvent,
        FileReadEvent,
    ]
    # A file reached through a symlink keeps the name it was reached by.
    assert [Path(e.root_dir) / e.path for e in events] == [
        workspace / "alias" / "outside.txt",
        external / "new.txt",
        external,
        workspace / "inside.txt",
    ]
    assert all(not Path(e.path).is_absolute() for e in events)
    assert events[0].content_hash and events[1].content_hash
