"""Shell completion scripts generated from the argparse parser.

Generating from the live parser (rather than checking in hand-written scripts)
keeps the completions honest: a new flag shows up without a second edit.

Values that only exist at runtime (running session hosts) cannot be baked into
the script, so an option marked with `complete_with` makes the script call
back into `pcode __complete KIND`, which prints `value<TAB>description` lines.
`pcode.cli` answers that before importing the frontend, so a Tab stays quick.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

SHELLS = ("zsh", "fish", "bash")

# The hidden first argument the generated scripts call back with.
COMPLETE_COMMAND = "__complete"


_TITLE_WIDTH = 50


def _hosts() -> list[tuple[str, str]]:
    from pcode.host_protocol import list_hosts

    candidates = []
    for entry in list_hosts():
        title = " ".join(entry.label().split())
        if len(title) > _TITLE_WIDTH:
            title = title[: _TITLE_WIDTH - 1] + "…"
        candidates.append((entry.id, f"{entry.state} · {Path(entry.workspace).name} · {title}"))
    return candidates


# Kinds of runtime value an option can complete, by the name the scripts pass.
_DYNAMIC: dict[str, Callable[[], list[tuple[str, str]]]] = {"hosts": _hosts}


def complete_with(action: argparse.Action, kind: str) -> argparse.Action:
    """Mark `action` as completing `kind` values, looked up when Tab is pressed."""
    if kind not in _DYNAMIC:
        raise ValueError(f"unknown completion kind: {kind}")
    action.complete_kind = kind  # type: ignore[attr-defined]
    return action


def _dynamic(action: argparse.Action) -> str | None:
    return getattr(action, "complete_kind", None)


def print_candidates(argv: list[str], stream=None) -> int:
    """`pcode __complete KIND`: one `value<TAB>description` line per candidate."""
    stream = stream or sys.stdout
    if len(argv) != 1 or argv[0] not in _DYNAMIC:
        print(f"usage: pcode {COMPLETE_COMMAND} {{{','.join(_DYNAMIC)}}}", file=sys.stderr)
        return 2
    for value, description in _DYNAMIC[argv[0]]():
        # A tab or newline would split the line the shells read.
        print(f"{value}\t{' '.join(description.split())}", file=stream)
    return 0


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
    lines = [f"#compdef {prog}", ""]
    kinds = sorted({kind for action in _visible_actions(parser) if (kind := _dynamic(action))})
    for kind in kinds:
        # Helpers are defined at the top level so both the autoloaded and the
        # sourced form of this file have them before `_{prog}` first runs.
        lines += [
            f"_{prog}_{kind}() {{",
            "  local -a values",
            f'  values=(${{(f)"$({prog} {COMPLETE_COMMAND} {kind} 2>/dev/null)"}})',
            # _describe splits value from description on the first colon.
            "  values=(\"${(@)values//$'\\t'/:}\")",
            f"  _describe -t {kind} {kind} values",
            "}",
            "",
        ]
    lines += [f"_{prog}() {{", "  local -a specs", "  specs=("]
    for action in _visible_actions(parser):
        # Repeating an option is almost never meaningful here, so tell zsh to
        # drop every spelling of a flag once any of them is on the line.
        exclusion = f"({' '.join(action.option_strings)})" if len(action.option_strings) > 1 else ""
        kind = _dynamic(action)
        for option in action.option_strings:
            name = option
            if _takes_value(action) and _optional_value(action):
                # `=-` marks an option whose value, if any, must be glued on as
                # `--flag=value`, so the next word still completes as a prompt.
                # A runtime value is the reason to type the option at all, so
                # there `=` also completes it as the next word.
                name = f"{option}=" if kind else f"{option}=-"
            spec = f"{exclusion}{name}[{_zsh_quote(_help(action))}]"
            if _takes_value(action):
                metavar = str(action.metavar or action.dest)
                if kind:
                    optional = ":" if _optional_value(action) else ""
                    spec += f":{optional}{metavar}:_{prog}_{kind}"
                elif choices := _choices(action):
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
        kind = _dynamic(action)
        if _takes_value(action):
            # -r demands a value; an optional value stays -f so fish still
            # offers the other flags. A runtime value is the reason to type the
            # option at all, so it takes -r anyway (a `-` still lists flags).
            parts.append("-r" if kind or not _optional_value(action) else "")
            if kind:
                parts += ["-f", "-a", _fish_quote(f"({prog} {COMPLETE_COMMAND} {kind})")]
            elif choices := _choices(action):
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
        if kind := _dynamic(action):
            pattern = "|".join(action.option_strings)
            values = f"$({prog} {COMPLETE_COMMAND} {kind} 2>/dev/null | cut -f1)"
            # An optional value leaves a `-` word to the flag list below.
            choice_cases.append(
                f'    {pattern})\n      if [[ "$cur" != -* ]]; then\n'
                f'        COMPREPLY=($(compgen -W "{values}" -- "$cur")); return 0\n'
                "      fi ;;"
            )
        elif choices := _choices(action):
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
