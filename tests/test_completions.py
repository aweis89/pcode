import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pcode.app import main
from pcode.completion import SHELLS


def render(shell: str, monkeypatch, capsys) -> str:
    monkeypatch.setattr(sys, "argv", ["pcode", "--completions", shell])
    with patch("pcode.app.PreviewApp") as app:
        main()
    app.assert_not_called()
    return capsys.readouterr().out


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_script_covers_every_flag(shell, monkeypatch, capsys):
    script = render(shell, monkeypatch, capsys)
    for flag in ("theme", "color-style", "worktree", "continue", "profile-memory"):
        # fish spells long options without the leading dashes (`-l theme`).
        assert flag in script
    # Choices come from the parser, so they cannot drift from the real options.
    assert "palette" in script and "terminal" in script
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


@pytest.mark.skipif(not shutil.which("fish"), reason="fish not installed")
def test_fish_script_completes_a_flag(monkeypatch, capsys, tmp_path):
    path = tmp_path / "pcode.fish"
    path.write_text(render("fish", monkeypatch, capsys))
    result = subprocess.run(
        ["fish", "--no-config", "-c", f'source {path}; complete -C "pcode --color-"'],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--color-style" in result.stdout
