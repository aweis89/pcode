"""Shell completion scripts generated from the argparse parser.

Generating from the live parser (rather than checking in hand-written scripts)
keeps the completions honest: a new flag shows up without a second edit.
"""

from __future__ import annotations

import argparse
from pathlib import Path

SHELLS = ("zsh", "fish", "bash")

_INSTALL_HINTS = {
    "zsh": "pcode --completions zsh > ~/.zsh/completions/_pcode  # dir must be on $fpath",
    "fish": "pcode --completions fish > ~/.config/fish/completions/pcode.fish",
    "bash": 'eval "$(pcode --completions bash)"  # add to ~/.bashrc',
}


def install_hint(shell: str) -> str:
    return _INSTALL_HINTS[shell]


def _visible_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [
        action
        for action in parser._actions
        if action.option_strings and action.help is not argparse.SUPPRESS
    ]


def _takes_value(action: argparse.Action) -> bool:
    return action.nargs != 0 and not isinstance(
        action, argparse._StoreTrueAction | argparse._StoreFalseAction | argparse._CountAction
    )


def _optional_value(action: argparse.Action) -> bool:
    return action.nargs == "?"


def _choices(action: argparse.Action) -> list[str]:
    return [str(choice) for choice in (action.choices or [])]


def _wants_path(action: argparse.Action) -> bool:
    return action.type is Path


def _help(action: argparse.Action) -> str:
    return " ".join((action.help or "").split())


def _zsh_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "'\\''").replace("[", "\\[").replace("]", "\\]")


def _zsh(parser: argparse.ArgumentParser, prog: str) -> str:
    lines = [f"#compdef {prog}", "", f"_{prog}() {{", "  local -a specs", "  specs=("]
    for action in _visible_actions(parser):
        # Repeating an option is almost never meaningful here, so tell zsh to
        # drop every spelling of a flag once any of them is on the line.
        exclusion = f"({' '.join(action.option_strings)})" if len(action.option_strings) > 1 else ""
        for option in action.option_strings:
            # `=-` marks an option whose value, if any, must be glued on as
            # `--flag=value`, which is how argparse's nargs="?" behaves too.
            name = f"{option}=-" if _takes_value(action) and _optional_value(action) else option
            spec = f"{exclusion}{name}[{_zsh_quote(_help(action))}]"
            if _takes_value(action):
                metavar = str(action.metavar or action.dest)
                if choices := _choices(action):
                    spec += f":{metavar}:({' '.join(choices)})"
                elif _wants_path(action):
                    spec += f":{metavar}:_files"
                else:
                    spec += f":{metavar}: "
            lines.append(f"    '{spec}'")
    lines += [
        "    '*:prompt:_files'",
        "  )",
        "  _arguments -s -S $specs && return 0",
        "}",
        "",
        # In $fpath the file is autoloaded *as* the completion function, so it
        # must call itself; when sourced from .zshrc it must register instead.
        f'if [ "$funcstack[1]" = "_{prog}" ]; then',
        f'  _{prog} "$@"',
        "else",
        f"  compdef _{prog} {prog}",
        "fi",
        "",
    ]
    return "\n".join(lines)


def _fish(parser: argparse.ArgumentParser, prog: str) -> str:
    lines = ["# fish completions for pcode", ""]
    for action in _visible_actions(parser):
        flags = []
        for option in action.option_strings:
            if option.startswith("--"):
                flags += ["-l", option[2:]]
            else:
                flags += ["-s", option[1:]]
        parts = [f"complete -c {prog}", *flags]
        if help_text := _help(action):
            parts += ["-d", _fish_quote(help_text)]
        if _takes_value(action):
            # -r demands a value; an optional value stays -f so fish still
            # offers the other flags.
            parts.append("-r" if not _optional_value(action) else "")
            if choices := _choices(action):
                parts += ["-f", "-a", _fish_quote(" ".join(choices))]
            elif _wants_path(action):
                parts.append("-F")
            else:
                parts.append("-f")
        else:
            parts.append("-f")
        lines.append(" ".join(part for part in parts if part))
    lines.append("")
    return "\n".join(lines)


def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _bash(parser: argparse.ArgumentParser, prog: str) -> str:
    options = [option for action in _visible_actions(parser) for option in action.option_strings]
    choice_cases = []
    for action in _visible_actions(parser):
        if not _takes_value(action):
            continue
        if choices := _choices(action):
            pattern = "|".join(action.option_strings)
            choice_cases.append(
                f"    {pattern})\n"
                f'      COMPREPLY=($(compgen -W "{" ".join(choices)}" -- "$cur")); return 0 ;;'
            )
        elif _wants_path(action):
            pattern = "|".join(action.option_strings)
            choice_cases.append(
                f'    {pattern})\n      COMPREPLY=($(compgen -f -- "$cur")); return 0 ;;'
            )
    cases = "\n".join(choice_cases)
    return f"""_{prog}() {{
  local cur prev
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  case "$prev" in
{cases}
  esac
  if [[ "$cur" == -* ]]; then
    COMPREPLY=($(compgen -W "{" ".join(options)}" -- "$cur"))
    return 0
  fi
  COMPREPLY=($(compgen -f -- "$cur"))
}}
complete -F _{prog} {prog}
"""


def render(parser: argparse.ArgumentParser, shell: str, prog: str = "pcode") -> str:
    """Return a completion script for `shell` describing `parser`."""
    if shell == "zsh":
        return _zsh(parser, prog)
    if shell == "fish":
        return _fish(parser, prog)
    if shell == "bash":
        return _bash(parser, prog)
    raise ValueError(f"unsupported shell: {shell}")
