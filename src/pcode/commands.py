"""One registry drives dispatch, help, and slash completion."""

from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: Callable[[str], None]
    arguments: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    free_arguments: bool = False
    argument_provider: Callable[[], tuple[str, ...]] | None = None


class CommandRegistry:
    def __init__(self) -> None:
        self.commands: list[Command] = []
        self._lookup: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        names = (command.name, *command.aliases)
        if len(set(names)) != len(names) or any(name in self._lookup for name in names):
            raise ValueError(f"Duplicate command: {command.name}")
        self.commands.append(command)
        self._lookup.update((name, command) for name in names)

    def find(self, name: str) -> Command | None:
        return self._lookup.get(name)

    def dispatch(self, text: str) -> bool:
        parts = text.strip().split(maxsplit=1)
        command = self.find(parts[0]) if parts else None
        if command is None:
            return False
        argument = parts[1].strip() if len(parts) > 1 else ""
        if argument and not command.free_arguments and argument not in command.arguments:
            usage = "|".join(command.arguments)
            raise ValueError(f"Usage: {command.name}" + (f" [{usage}]" if usage else ""))
        command.handler(argument)
        return True


class SlashCompleter(Completer):
    def __init__(self, registry: CommandRegistry) -> None:
        self.registry = registry

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in document.text or document.text_after_cursor:
            return
        if not any(char.isspace() for char in text):
            for command in self.registry.commands:
                if any(name.startswith(text) for name in (command.name, *command.aliases)):
                    yield Completion(
                        command.name,
                        start_position=-len(text),
                        display_meta=command.description,
                    )
            return
        name, prefix = text.split(maxsplit=1) if len(text.split()) > 1 else (text.strip(), "")
        command = self.registry.find(name)
        if command:
            arguments = (
                command.argument_provider() if command.argument_provider else command.arguments
            )
            for argument in arguments:
                if argument.startswith(prefix):
                    yield Completion(argument, start_position=-len(prefix))
