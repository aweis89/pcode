"""Turn discovered SKILL.md assets into slash commands the user can invoke.

The repo-context inventory already locates skills without reading them; this
module reads only each file's frontmatter, so completion can show a description
while the body still reaches the model through a normal tool read.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pcode.commands import Command
from pcode.preferences import SETTINGS, load_preferences

PREFIX = "/skill:"

# Mirrors Harness's RepoContext.asset_roots and its skills/**/SKILL.md glob.
# Importing Harness here would cost ~0.8s on the terminal's startup path, which
# the app otherwise keeps off the event loop; test_skills guards the drift.
ASSET_ROOTS = (".claude", ".agents", ".codex", ".grok")


@dataclass(frozen=True)
class Skill:
    """One discovered SKILL.md, named after its containing directory."""

    name: str
    path: str
    description: str = ""


def _frontmatter(path: Path) -> dict[str, str]:
    """Read the leading `---` block as flat `key: value` pairs.

    Skills are authored for other assistants, so treat anything unparsable as
    absent rather than failing the launch.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key and not key[0].isspace():
            fields[key.strip().lower()] = value.strip().strip("\"'")
    return fields


def skill_dirs(workspace: Path) -> list[Path]:
    """Resolve the configured skill directories against `workspace`."""
    configured = load_preferences().get("skill_dirs", SETTINGS["skill_dirs"].default) or ""
    directories = []
    for entry in configured.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry).expanduser()
        directories.append(path if path.is_absolute() else workspace / path)
    return directories


def _reference(path: Path, workspace: Path) -> str:
    """Name the file for the model: workspace-relative inside, absolute outside."""
    if path.is_relative_to(workspace):
        return path.relative_to(workspace).as_posix()
    return path.as_posix()


def discover_skills(workspace: Path) -> list[Skill]:
    """Locate skills, keeping the first of any duplicated name.

    Workspace asset roots come first, so a project skill shadows a user-level one
    of the same name.
    """
    workspace = workspace.resolve()
    roots = [workspace / root / "skills" for root in ASSET_ROOTS]
    skills: dict[str, Skill] = {}
    for directory in roots + skill_dirs(workspace):
        for path in sorted(directory.glob("**/SKILL.md")):
            if not path.is_file():
                continue
            name = path.parent.name.strip().replace(" ", "-")
            if not name or name in skills:
                continue
            reference = _reference(path, workspace)
            skills[name] = Skill(name, reference, _frontmatter(path).get("description", ""))
    return list(skills.values())


def skill_prompt(skill: Skill, argument: str) -> str:
    """Ask for the skill by path; the model reads the body itself."""
    prompt = (
        f'Use the "{skill.name}" skill: read {skill.path} '
        "and follow its instructions for this request."
    )
    return f"{prompt}\n\n{argument}" if argument else prompt


def skill_style() -> str:
    return load_preferences().get("skill_commands", SETTINGS["skill_commands"].default)


def skill_commands(
    skills: Sequence[Skill],
    handler: Callable[[Skill, str], None],
    style: str | None = None,
) -> list[Command]:
    """Build the commands for `skills`, in the configured naming style."""
    style = style or skill_style()
    if style == "off":
        return []
    commands = []
    for skill in skills:
        names = [PREFIX + skill.name] if style != "bare" else []
        if style in ("bare", "both"):
            names.append("/" + skill.name)
        description = skill.description or f"Run the {skill.name} skill ({skill.path})"
        # Skill descriptions are written for a model, not for a completion menu.
        if len(description) > 96:
            description = description[:95].rstrip() + "…"
        commands.append(
            Command(
                names[0],
                description,
                lambda argument, skill=skill: handler(skill, argument),
                aliases=tuple(names[1:]),
                free_arguments=True,
                group="Skills",
            )
        )
    return commands
