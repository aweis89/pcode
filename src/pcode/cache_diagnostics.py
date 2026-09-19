"""Name what moved the cacheable prefix when a cache collapse fires.

A collapse warning reports only the provider's token verdict, which cannot say
whether the prompt prefix was rewritten or the cache simply expired. This keeps a
small rolling fingerprint of each model request so the next collapse answers that
question directly: an intact prefix points at TTL or breakpoint placement, and a
changed prefix names the message index that moved.

Fingerprints are content-free by construction: digests, sizes and kinds only,
never prompt text. Prompts carry file contents and command output, so writing them
to a debug file would leak exactly what the rest of pcode refuses to log.
"""

import hashlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.messages import CachePoint

from pcode.diagnostics import versions

HISTORY = 8
"""Requests retained per run. A collapse is diagnosed from the preceding request,
so this only needs enough depth to show the trend that led into it."""

CACHE_SETTINGS = (
    "anthropic_cache",
    "anthropic_cache_instructions",
    "anthropic_cache_tool_definitions",
)


def diagnostics_root() -> Path:
    """`PCODE_CACHE_DIAGNOSTICS` overrides the directory; `off` disables dumps."""
    override = os.environ.get("PCODE_CACHE_DIAGNOSTICS", "").strip()
    if override and override.lower() != "off":
        return Path(override).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "cache-diagnostics"


def enabled() -> bool:
    return os.environ.get("PCODE_CACHE_DIAGNOSTICS", "").strip().lower() != "off"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _content_text(value) -> str:
    """Render content for hashing, marking cache points so a moved one is visible."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, CachePoint):
        return f"<cache-point:{value.ttl}>"
    if isinstance(value, (list, tuple)):
        return "".join(_content_text(item) for item in value)
    # Binary and multimodal parts have no stable text; size and type still detect
    # a change without pulling the payload into the digest.
    data = getattr(value, "data", None)
    size = len(data) if isinstance(data, (bytes, bytearray)) else ""
    return f"<{type(value).__name__}:{size}>"


def _cache_points(value) -> int:
    if isinstance(value, CachePoint):
        return 1
    if isinstance(value, (list, tuple)):
        return sum(_cache_points(item) for item in value)
    return 0


def _part_text(part) -> str:
    content = getattr(part, "content", None)
    if content is None and hasattr(part, "args"):
        # Tool calls carry their payload in `args`; `args_as_json_str` normalizes
        # the dict/str union so an unchanged call hashes identically either way.
        try:
            content = part.args_as_json_str()
        except Exception:
            content = repr(part.args)
    kind = getattr(part, "part_kind", type(part).__name__)
    name = getattr(part, "tool_name", "") or ""
    return f"{kind}|{name}|{_content_text(content)}"


@dataclass(frozen=True)
class MessageFingerprint:
    kind: str
    parts: tuple[str, ...]
    chars: int
    digest: str
    cache_points: int

    def moved(self, other: "MessageFingerprint") -> bool:
        return self.kind != other.kind or self.digest != other.digest

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "parts": list(self.parts),
            "chars": self.chars,
            "digest": self.digest,
            "cache_points": self.cache_points,
        }


def fingerprint_message(message) -> MessageFingerprint:
    parts = tuple(getattr(part, "part_kind", type(part).__name__) for part in message.parts)
    text = "\n".join(_part_text(part) for part in message.parts)
    points = sum(_cache_points(getattr(part, "content", None)) for part in message.parts)
    return MessageFingerprint(
        kind=getattr(message, "kind", type(message).__name__),
        parts=parts,
        chars=len(text),
        digest=_digest(text),
        cache_points=points,
    )


@dataclass(frozen=True)
class RequestFingerprint:
    step: int
    model: str
    at: float
    instructions: str
    instruction_chars: int
    tools: str
    tool_names: tuple[str, ...]
    settings: dict
    messages: tuple[MessageFingerprint, ...]
    cache_read: int
    cache_write: int
    input_tokens: int

    @property
    def cache_point_indexes(self) -> list[int]:
        return [i for i, message in enumerate(self.messages) if message.cache_points]

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "model": self.model,
            "at": self.at,
            "instructions": self.instructions,
            "instruction_chars": self.instruction_chars,
            "tools": self.tools,
            "tool_names": list(self.tool_names),
            "settings": self.settings,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "input_tokens": self.input_tokens,
            "cache_point_indexes": self.cache_point_indexes,
            "messages": [message.as_dict() for message in self.messages],
        }


def fingerprint(request_context, response, step: int) -> RequestFingerprint:
    parameters = request_context.model_request_parameters
    instruction_text = "\n".join(
        _content_text(getattr(part, "content", None))
        for part in (getattr(parameters, "instruction_parts", None) or [])
    )
    tools = [
        *(getattr(parameters, "function_tools", None) or []),
        *(getattr(parameters, "output_tools", None) or []),
    ]
    # Definitions reach the provider in list order: a reordered toolset invalidates
    # the cached block just as an edited schema does, so order stays in the digest.
    tool_text = "\n".join(
        f"{tool.name}|{tool.description or ''}|"
        f"{json.dumps(tool.parameters_json_schema, sort_keys=True, default=str)}"
        for tool in tools
    )
    settings = request_context.model_settings or {}
    usage = response.usage
    return RequestFingerprint(
        step=step,
        model="/".join(part for part in (response.provider_name, response.model_name) if part),
        at=time.time(),
        instructions=_digest(instruction_text),
        instruction_chars=len(instruction_text),
        tools=_digest(tool_text),
        tool_names=tuple(tool.name for tool in tools),
        settings={key: settings[key] for key in CACHE_SETTINGS if key in settings},
        messages=tuple(fingerprint_message(message) for message in request_context.messages),
        cache_read=usage.cache_read_tokens,
        cache_write=usage.cache_write_tokens,
        input_tokens=usage.input_tokens,
    )


def divergence(previous: RequestFingerprint, current: RequestFingerprint) -> str:
    """Describe the first prefix change, or report the prefix as intact.

    Order matters: instructions and tool definitions sit ahead of every message, so
    a change there explains a collapse that a message-level diff would misattribute.
    """
    if previous.instructions != current.instructions:
        return (
            "Instructions changed "
            f"({previous.instruction_chars} -> {current.instruction_chars} chars): "
            "everything after the instruction block was re-sent."
        )
    if previous.tools != current.tools:
        added = [name for name in current.tool_names if name not in previous.tool_names]
        removed = [name for name in previous.tool_names if name not in current.tool_names]
        detail = ", ".join(
            part
            for part in (
                f"added {', '.join(added)}" if added else "",
                f"removed {', '.join(removed)}" if removed else "",
            )
            if part
        )
        return "Tool definitions changed" + (
            f" ({detail})." if detail else " (same names, edited schema or order)."
        )
    if previous.settings != current.settings:
        return f"Cache settings changed ({previous.settings} -> {current.settings})."
    shared = min(len(previous.messages), len(current.messages))
    for index in range(shared):
        before, after = previous.messages[index], current.messages[index]
        if before.moved(after):
            return (
                f"Message {index} of {len(previous.messages)} changed "
                f"(kind {before.kind} -> {after.kind}, {before.chars} -> {after.chars} chars, "
                f"cache points {before.cache_points} -> {after.cache_points}). "
                f"{len(previous.messages) - index} message(s) after it were re-sent."
            )
    if len(current.messages) < len(previous.messages):
        return (
            f"History shrank from {len(previous.messages)} to {len(current.messages)} "
            "messages: the tail was dropped or rewritten by a capability."
        )
    appended = len(current.messages) - len(previous.messages)
    points = (
        f" Cache points moved {previous.cache_point_indexes} -> {current.cache_point_indexes}."
        if previous.cache_point_indexes != current.cache_point_indexes
        else ""
    )
    gap = current.at - previous.at
    return (
        f"Prefix intact: all {len(previous.messages)} earlier messages are byte-identical "
        f"and {appended} were appended, so nothing rewrote history. Suspect cache TTL "
        f"(~{gap:.0f}s since the previous request) or breakpoint placement.{points}"
    )


@dataclass
class CacheDiagnostics:
    """Per-run rolling fingerprints, written out only when a collapse fires."""

    records: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    step: int = 0

    def record(self, request_context, response) -> RequestFingerprint:
        # Count separately from the window: the deque is bounded, so its length
        # stops growing and would repeat step numbers for exactly the long runs
        # a collapse is most likely to show up in.
        self.step += 1
        current = fingerprint(request_context, response, step=self.step)
        self.records.append(current)
        return current

    def summary(self) -> str:
        if len(self.records) < 2:
            return ""
        return divergence(self.records[-2], self.records[-1])

    def dump(self) -> Path | None:
        """Write the rolling window; return the path, or None when unavailable."""
        if not enabled() or not self.records:
            return None
        current = self.records[-1]
        root = diagnostics_root()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(current.at))
        # Two runs in one process can collapse in the same second at the same step,
        # so the obvious name is not unique. Overwriting would discard the only
        # record of the earlier collapse.
        base = f"{stamp}-{os.getpid()}-step{current.step}"
        path = root / f"{base}.json"
        for attempt in range(1, 100):
            if not path.exists():
                break
            path = root / f"{base}-{attempt}.json"
        payload = {
            "versions": versions(),
            "model": current.model,
            "summary": self.summary(),
            "requests": [record.as_dict() for record in self.records],
        }
        try:
            root.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            # A diagnostic must never take down the run that produced it.
            return None
        return path
