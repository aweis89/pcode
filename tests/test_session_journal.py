import json
import os

import pytest

from pcode.sessions import SavedSession, SessionBusy, SessionError, SessionJournal


def turn(session, run_id, prompt, answer, parent=None):
    session.append("turn_started", run_id=run_id, parent_id=parent, prompt=prompt)
    session.append("TextDelta", run_id=run_id, text=answer)
    session.append("Message", run_id=run_id, markdown=answer)
    session.append("turn_completed", run_id=run_id)


def summary(tree):
    return tree.active, {
        identity: (node.parent, node.prompt, node.status, node.response)
        for identity, node in tree.nodes.items()
    }


@pytest.fixture
def writer(tmp_path):
    session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    yield session
    session.close()


def test_read_while_the_writer_holds_the_lock(writer):
    turn(writer, "a", "first", "one")
    turn(writer, "b", "second", "two", parent="a")
    reader = SessionJournal.read(writer.directory)
    assert type(reader) is SessionJournal
    assert reader.info == writer.info
    assert summary(reader.tree) == summary(writer.tree)
    assert list(reader.transcript_records()) == list(writer.transcript_records())
    assert list(reader.tool_events()) == list(writer.tool_events())
    assert reader.latest_plan() == writer.latest_plan() == []


def test_refresh_follows_appends_and_waits_for_a_whole_line(writer):
    turn(writer, "a", "first", "one")
    reader = SessionJournal.read(writer.directory)
    assert reader.refresh() is False

    turn(writer, "b", "second", "two", parent="a")
    assert reader.refresh() is True
    assert summary(reader.tree) == summary(writer.tree)
    assert reader.tree.nodes["b"].status == "completed"
    assert [r["prompt"] for r in reader.transcript_records() if r["kind"] == "turn_started"] == [
        "first",
        "second",
    ]
    assert reader.refresh() is False

    line = json.dumps({"kind": "turn_started", "run_id": "c", "parent_id": "b", "prompt": "3"})
    path = writer.directory / "transcript.jsonl"
    with path.open("ab") as file:
        file.write(line.encode()[:20])
    assert reader.refresh() is False
    assert "c" not in reader.tree.nodes
    with path.open("ab") as file:
        file.write(line.encode()[20:] + b"\n")
    assert reader.refresh() is True
    assert reader.tree.nodes["c"].parent == "b"
    assert reader.tree.active == "c"


def test_refresh_rereads_the_manifest(writer):
    reader = SessionJournal.read(writer.directory)
    writer.info.turns = 3
    writer.save_info()
    assert reader.refresh() is True
    assert reader.info.turns == 3
    assert reader.refresh() is False


def test_read_takes_no_lock_and_writes_nothing(writer):
    turn(writer, "a", "first", "one")
    directory = writer.directory
    before = {
        path.name: (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.iterdir()
    }
    os.chmod(directory, 0o755)
    reader = SessionJournal.read(directory)
    reader.refresh()
    assert not hasattr(reader, "lock") and not hasattr(reader, "store")
    assert (directory.stat().st_mode & 0o777) == 0o755
    assert {
        path.name: (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.iterdir()
    } == before
    # The writer still owns the session: it can append, and nobody else can open it.
    turn(writer, "b", "second", "two", parent="a")
    with pytest.raises(SessionBusy):
        SavedSession(directory, writer.info)
    assert reader.refresh() is True
    assert "b" in reader.tree.nodes


def test_read_works_after_the_writer_closes(tmp_path):
    session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    turn(session, "a", "first", "one")
    session.close()
    reader = SessionJournal.read(session.directory)
    assert reader.tree.nodes["a"].response == "one"
    # A reader leaves the session free for an owner to open.
    SavedSession.open(session.info.id, tmp_path / "sessions").close()


def test_read_refuses_a_symlinked_directory(writer, tmp_path):
    link = tmp_path / "sessions" / "link"
    link.symlink_to(writer.directory)
    with pytest.raises(SessionError):
        SessionJournal.read(link)


def test_read_skips_torn_and_foreign_lines_like_the_writer(writer):
    turn(writer, "a", "first", "one")
    path = writer.directory / "transcript.jsonl"
    with path.open("ab") as file:
        file.write(b"[1, 2]\nnot json\n")
    turn(writer, "b", "second", "two", parent="a")
    reader = SessionJournal.read(writer.directory)
    assert list(reader.records()) == list(writer.records())
    assert summary(reader.tree) == summary(writer.tree)


@pytest.mark.parametrize("how", ["truncate", "replace"])
def test_a_rewritten_journal_rebuilds_the_tree(writer, how):
    turn(writer, "a", "first", "one")
    turn(writer, "b", "second", "two", parent="a")
    reader = SessionJournal.read(writer.directory)
    assert set(reader.tree.nodes) == {"a", "b"}

    path = writer.directory / "transcript.jsonl"
    kept = [r for r in writer.records() if r.get("run_id") == "a"]
    text = "".join(json.dumps(record) + "\n" for record in kept)
    if how == "truncate":
        path.write_text(text)
    else:
        # Same length or longer, but a different file: size alone can't tell.
        kept.append({"kind": "turn_started", "run_id": "z", "parent_id": "a", "prompt": "x" * 400})
        text = "".join(json.dumps(record) + "\n" for record in kept)
        replacement = path.with_name("replacement")
        replacement.write_text(text)
        replacement.replace(path)
    assert reader.refresh() is True
    expected = {"a"} if how == "truncate" else {"a", "z"}
    assert set(reader.tree.nodes) == expected
    assert reader.tree.nodes["a"].status == "completed"
