import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pcode import cli
from pcode.app import main
from pcode.completion import SHELLS
from pcode.host_protocol import HostEntry, write_entry

# Stands in for `pcode __complete hosts` inside the generated scripts.
FAKE_HOSTS = "abc123\tidle · repo · fix: bug\nabd456\tworking · r2 · other\n"


def render(shell: str, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["pcode", "--completions", shell])
    with patch("pcode.app.PreviewApp") as app:
        main()
    app.assert_not_called()
    return capsys.readouterr().out


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_script_covers_every_flag(shell, monkeypatch, capsys):
    script = render(shell, monkeypatch, capsys)
    for flag in ("theme", "theme-preview", "worktree", "continue", "profile-memory"):
        # fish spells long options without the leading dashes (`-l theme`).
        assert flag in script
    # Choices come from the parser, so they cannot drift from the real options.
    assert "dark" in script and "light" in script
    assert "# Install:" in script


def test_optional_value_flags_use_glued_syntax_in_zsh(monkeypatch, capsys):
    script = render("zsh", monkeypatch, capsys)
    assert "'(-c --continue)-c=-[" in script
    assert "'--sessions[" in script


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_zsh_script_loads_both_sourced_and_autoloaded(monkeypatch, capsys, tmp_path):
    script = render("zsh", monkeypatch, capsys)
    path = tmp_path / "_pcode"
    path.write_text(script)
    subprocess.run(
        ["zsh", "-c", f"autoload -U compinit; compinit -u -d {tmp_path}/zcompdump; source {path}"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "zsh",
            "-c",
            f"fpath=({tmp_path} $fpath); autoload -U compinit; "
            f"compinit -u -d {tmp_path}/zcompdump2; which _pcode >/dev/null",
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not installed")
def test_bash_script_is_valid_syntax(monkeypatch, capsys, tmp_path):
    path = Path(tmp_path / "pcode.bash")
    path.write_text(render("bash", monkeypatch, capsys))
    subprocess.run(["bash", "-n", str(path)], check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not installed")
def test_bash_script_completes_running_hosts(monkeypatch, capsys, tmp_path):
    path = tmp_path / "pcode.bash"
    path.write_text(render("bash", monkeypatch, capsys))
    script = (
        f"source {path}; pcode() {{ printf '{FAKE_HOSTS}'; }}; "
        'for cur in "" abd -; do COMP_WORDS=(pcode --attach "$cur"); COMP_CWORD=2; '
        'COMPREPLY=(); _pcode; echo "${COMPREPLY[*]}"; done'
    )
    result = subprocess.run(
        ["bash", "--norc", "-c", script], check=True, capture_output=True, text=True
    )
    every, prefixed, flags = result.stdout.splitlines()
    assert every == "abc123 abd456"
    assert prefixed == "abd456"
    # The value is optional, so a dash still completes the other flags.
    assert "--print" in flags.split()


def test_zsh_script_completes_attach_with_running_hosts(monkeypatch, capsys):
    script = render("zsh", monkeypatch, capsys)
    assert "_pcode_hosts() {" in script
    assert "pcode __complete hosts" in script
    # `=` (not `=-`) so the host can be the next word, as `pcode --attach abc` is typed.
    assert "'--attach=[" in script and "]::HOST:_pcode_hosts'" in script


@pytest.mark.skipif(not shutil.which("fish"), reason="fish not installed")
def test_fish_script_completes_running_hosts(monkeypatch, capsys, tmp_path):
    path = tmp_path / "pcode.fish"
    path.write_text(render("fish", monkeypatch, capsys))
    fake = FAKE_HOSTS.replace("\t", "\\t").replace("\n", "\\n")
    result = subprocess.run(
        [
            "fish",
            "--no-config",
            "-c",
            f"source {path}; function pcode; printf '{fake}'; end; "
            'complete -C "pcode --attach "; echo ---; complete -C "pcode --attach --pri"',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    hosts, flags = result.stdout.split("---\n")
    assert hosts.splitlines() == ["abc123\tidle · repo · fix: bug", "abd456\tworking · r2 · other"]
    assert "--print" in flags


def test_complete_command_lists_running_hosts_without_the_frontend(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("PCODE_HOST_DIR", str(tmp_path))
    write_entry(
        HostEntry(
            id="abc123",
            pid=os.getpid(),
            model="test",
            workspace="/src/repo",
            state="idle",
            title="fix\tthe\nbug " + "x" * 80,
        )
    )
    monkeypatch.setattr(sys, "argv", ["pcode", "__complete", "hosts"])
    with patch("pcode.app.main") as frontend, pytest.raises(SystemExit) as exit:
        cli.main()
    frontend.assert_not_called()
    assert exit.value.code == 0
    (line,) = capsys.readouterr().out.splitlines()
    value, description = line.split("\t")
    assert value == "abc123"
    assert description.startswith("idle · repo · fix the bug x")
    assert description.endswith("…")


def test_complete_command_rejects_an_unknown_kind(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "__complete", "planets"])
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 2
    assert "usage" in capsys.readouterr().err


@pytest.mark.skipif(not shutil.which("fish"), reason="fish not installed")
def test_fish_script_completes_a_flag(monkeypatch, capsys, tmp_path):
    path = tmp_path / "pcode.fish"
    path.write_text(render("fish", monkeypatch, capsys))
    result = subprocess.run(
        ["fish", "--no-config", "-c", f'source {path}; complete -C "pcode --theme-"'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--theme-preview" in result.stdout
