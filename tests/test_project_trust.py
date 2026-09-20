import io
import subprocess
import sys
from unittest.mock import patch

from pcode import project_trust, worktree
from pcode.app import main
from pcode.ext import extension_dirs
from pcode.preferences import load_preferences, save_preferences


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_repo(path):
    path = path.resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "README").write_text("x")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return path


def ship_setup(root):
    (root / ".pcode").mkdir(exist_ok=True)
    (root / ".pcode" / "worktree-setup").write_text("touch ran\n")


def test_project_code_lists_only_real_code(tmp_path):
    assert project_trust.project_code(tmp_path) == []
    (tmp_path / ".pcode" / "extensions").mkdir(parents=True)
    assert project_trust.project_code(tmp_path) == []  # empty dir is nothing
    (tmp_path / ".pcode" / "extensions" / "_private.py").write_text("")
    assert project_trust.project_code(tmp_path) == []
    (tmp_path / ".pcode" / "extensions" / "tool.py").write_text("")
    ship_setup(tmp_path)
    assert project_trust.project_code(tmp_path) == [
        project_trust.EXTENSIONS_DIR,
        project_trust.SETUP_SCRIPT,
    ]


def test_trust_is_keyed_on_primary_checkout(tmp_path):
    repo = make_repo(tmp_path / "repo")
    linked = worktree.create(repo, "wt").path
    assert not project_trust.is_trusted(repo)
    assert project_trust.trust(linked) == repo
    assert project_trust.is_trusted(repo)
    assert project_trust.is_trusted(linked)
    assert load_preferences()["trusted_projects"] == str(repo)
    # Another repo is untouched; the blanket setting still covers everything.
    other = make_repo(tmp_path / "other")
    assert not project_trust.is_trusted(other)
    save_preferences(project_extensions="on")
    assert project_trust.is_trusted(other)


def test_trust_outside_git_uses_the_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert project_trust.trust(plain) == plain.resolve()
    assert project_trust.is_trusted(plain)


def test_prompt_paths(tmp_path):
    repo = make_repo(tmp_path / "repo")
    err = io.StringIO()
    assert project_trust.prompt_trust(repo, ask=lambda _: "y", stream=err) is False
    assert err.getvalue() == ""  # nothing shipped: silent
    ship_setup(repo)
    assert project_trust.prompt_trust(repo, ask=None, stream=err) is False
    assert "skipping untrusted project code" in err.getvalue()
    assert project_trust.prompt_trust(repo, ask=lambda _: "n", stream=err) is False
    assert "not trusted" in err.getvalue()
    assert not project_trust.is_trusted(repo)

    def eof(_):
        raise EOFError

    assert project_trust.prompt_trust(repo, ask=eof, stream=err) is False
    assert project_trust.prompt_trust(repo, ask=lambda _: " Yes ", stream=err) is True
    assert project_trust.is_trusted(repo)
    err = io.StringIO()
    assert project_trust.prompt_trust(repo, ask=lambda _: "n", stream=err) is True
    assert err.getvalue() == ""  # already trusted: no prompt


def test_trust_gates_extensions_and_setup_script(tmp_path):
    repo = make_repo(tmp_path / "repo")
    ship_setup(repo)
    (repo / ".pcode" / "extensions").mkdir()
    (repo / ".pcode" / "extensions" / "x.py").write_text("def setup(pcode): pass\n")
    assert all(scope != "project" for _, scope in extension_dirs(repo))
    created = worktree.create(repo, "wt")
    assert worktree.setup_scripts(created) == []
    project_trust.trust(repo)
    assert any(scope == "project" for _, scope in extension_dirs(repo))
    assert worktree.setup_scripts(created) == [repo / ".pcode" / "worktree-setup"]


def cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pcode", *argv])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_cli_prompts_before_creating_the_worktree(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    repo = make_repo(tmp_path / "repo")
    ship_setup(repo)
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree", "one")
    with patch("pcode.app.PreviewApp"), patch("builtins.input", return_value="n"):
        main()
    assert not (repo / ".worktrees" / "pcode-one" / "ran").exists()
    assert "ships code that runs at launch" in capsys.readouterr().err
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree", "two")
    with patch("pcode.app.PreviewApp"), patch("builtins.input", return_value="y"):
        main()
    assert (repo / ".worktrees" / "pcode-two" / "ran").exists()
    assert project_trust.is_trusted(repo)
    # Trusted now: no prompt on the next launch.
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--worktree", "three")
    with patch("pcode.app.PreviewApp"), patch("builtins.input", side_effect=AssertionError):
        main()
    assert (repo / ".worktrees" / "pcode-three" / "ran").exists()


def test_cli_print_mode_never_prompts(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "repo")
    ship_setup(repo)
    cli(monkeypatch, "-m", "test:local", "-C", str(repo), "--print", "hi")
    with patch("pcode.app.PreviewApp") as app, patch("builtins.input", side_effect=AssertionError):
        app.return_value.run_print.return_value = True
        main()
    assert "skipping untrusted project code" in capsys.readouterr().err
    assert not project_trust.is_trusted(repo)
