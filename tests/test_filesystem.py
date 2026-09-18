import asyncio
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai_harness.filesystem import (
    DirectoryListedEvent,
    FileChangeRequestEvent,
    FileReadEvent,
    FileWrittenEvent,
)

from pcode.workspace_filesystem import WorkspaceFileSystem


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


def test_external_file_lifecycle_and_conflicts(paths):
    workspace, external = paths
    fs = WorkspaceFileSystem(workspace).get_toolset()

    async def run():
        target = external / "new" / "sample.txt"
        with pytest.raises(ModelRetry, match="Use create_directory first"):
            await fs.write_file(str(target), "first")
        await fs.create_directory(str(target.parent))
        await fs.write_file(str(target), "first")
        assert "first" in await fs.read_file("../external/new/sample.txt")
        await fs.edit_file(str(target), "first", "second")
        assert "second" in await fs.read_file("alias/new/sample.txt")
        with pytest.raises(ModelRetry, match="[Hh]ash"):
            await fs.write_file(str(target), "lost", expected_hash="stale")
        assert target.read_text() == "second"
        assert "type: file" in await fs.file_info(str(target))
        assert "inside marker" in await fs.read_file("inside.txt")

    asyncio.run(run())


@pytest.mark.parametrize("style", ["absolute", "parent", "symlink"])
def test_external_walkers_return_reusable_absolute_paths(paths, style):
    workspace, external = paths
    fs = WorkspaceFileSystem(workspace).get_toolset()
    (external / ".ignored.txt").write_text("outside hidden")
    (external / "dangling").symlink_to(external / "missing")
    selected = {"absolute": str(external), "parent": "../external", "symlink": "alias"}[style]

    async def run():
        expected = str(external / "outside.txt")
        listing = await fs.list_directory(selected)
        found = await fs.find_files("*.txt", path=selected)
        searched = await fs.search_files("outside", path=selected, include_glob="*.txt")
        assert listing == f"{expected}  (15 bytes)"
        assert found == expected
        assert searched == f"{expected}:1:outside marker"
        assert "outside marker" in await fs.read_file(found)
        assert await fs.search_files("marker", path=expected) == f"{expected}:1:outside marker"
        # Default traversal stays workspace-local, not host-wide.
        assert await fs.find_files("*.txt") == "inside.txt"
        assert await fs.search_files("inside") == "inside.txt:1:inside marker"

    asyncio.run(run())


def test_external_globs_hidden_entries_and_limits(paths):
    workspace, external = paths
    source = external / "src"
    source.mkdir()
    for name in ("one.py", "two.py", ".hidden.py"):
        (source / name).write_text("match\nmatch\n")
    fs = WorkspaceFileSystem(
        workspace, max_list_results=1, max_find_results=1, max_search_results=1
    ).get_toolset()

    async def run():
        assert "truncated at 1 entries" in await fs.list_directory(str(source))
        assert "truncated at 1 matches" in await fs.find_files("*.py", path=str(source))
        result = await fs.search_files("match", path=str(external), include_glob="src/*.py")
        assert result == f"{source}/one.py:1:match\n[... truncated at 1 matches]"
        with pytest.raises(ModelRetry, match="absolute"):
            await fs.find_files(str(source / "*.py"), path=str(external))
        with pytest.raises(ModelRetry, match="Invalid regex"):
            await fs.search_files("[", path=str(external))

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
    fs = WorkspaceFileSystem(workspace).get_toolset()

    async def run():
        with pytest.raises(ModelRetry, match="protected"):
            await fs.write_file(requested, "replacement")
        with pytest.raises(ModelRetry, match="protected"):
            await fs.edit_file(requested, "fixture", "replacement")

    asyncio.run(run())
    assert target.read_text() == "synthetic fixture"


def test_patterns_authorize_canonical_external_targets(paths):
    workspace, external = paths
    fs = WorkspaceFileSystem(
        workspace, allowed_patterns=["*.txt"], denied_patterns=[str(external / "outside.txt")]
    ).get_toolset()

    async def run():
        with pytest.raises(ModelRetry, match="denied"):
            await fs.read_file("alias/outside.txt")
        assert await fs.list_directory(str(external)) == "(empty directory)"
        assert await fs.find_files("*", path=str(external)) == "No matches found."
        assert await fs.search_files("marker", path=str(external)) == "No matches found."
        assert "inside marker" in await fs.read_file("inside.txt")

    asyncio.run(run())


def test_symlink_loop_is_recoverable(paths):
    workspace, external = paths
    (external / "loop").symlink_to(external / "loop")
    fs = WorkspaceFileSystem(workspace).get_toolset()

    async def run():
        with pytest.raises(ModelRetry, match="symlink loop"):
            await fs.read_file(str(external / "loop"))
        assert str(external / "loop") not in await fs.list_directory(str(external))

    asyncio.run(run())


def test_external_events_identify_actual_targets(paths):
    workspace, external = paths
    fs = WorkspaceFileSystem(workspace).get_toolset()
    events = []

    class Context:
        async def emit(self, event):
            events.append(event)

    async def run():
        ctx = Context()
        await fs._read_file_tool(ctx, "alias/outside.txt")
        await fs._write_file_tool(ctx, str(external / "new.txt"), "new")
        await fs._list_directory_tool(ctx, str(external))
        await fs._read_file_tool(ctx, "inside.txt")
        with pytest.raises(ModelRetry):
            await fs._read_file_tool(ctx, str(external / "missing"))

    asyncio.run(run())
    # Change requests are pre-mutation events, not successful traversal evidence.
    events = [event for event in events if not isinstance(event, FileChangeRequestEvent)]
    assert [type(e) for e in events] == [
        FileReadEvent,
        FileWrittenEvent,
        DirectoryListedEvent,
        FileReadEvent,
    ]
    assert [Path(e.root_dir) / e.path for e in events] == [
        external / "outside.txt",
        external / "new.txt",
        external,
        workspace / "inside.txt",
    ]
    assert all(not Path(e.path).is_absolute() for e in events)
    assert events[2].entry_count == 2
    assert events[0].content_hash
    assert events[1].content_hash
    assert events[3].root_dir == str(workspace)


def test_external_symlink_metadata_and_protected_target(paths):
    workspace, external = paths
    (workspace / "file-link").symlink_to(external / "outside.txt")
    protected = external / ".env"
    protected.write_text("synthetic fixture")
    (workspace / "innocent.txt").symlink_to(protected)
    fs = WorkspaceFileSystem(workspace).get_toolset()

    async def run():
        assert f"symlink_target: {external}" in await fs.file_info("alias")
        assert f"symlink_target: {external}/outside.txt" in await fs.file_info("file-link")
        with pytest.raises(ModelRetry, match="protected"):
            await fs.write_file("innocent.txt", "replacement")

    asyncio.run(run())
    assert protected.read_text() == "synthetic fixture"
