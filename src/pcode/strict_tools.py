"""Opt-in grammar-constrained tool arguments for Anthropic models.

Claude sometimes emits a nested tool argument as an escaped JSON *string* rather
than the array the schema asks for, and botches the escaping while doing it. In
~2k recorded `edit_file` calls the Anthropic share of `replacements` arrivals
failed validation about one time in eight, while the OpenAI share never did --
Pydantic AI turns OpenAI's strict mode on by itself, and strict mode is
constrained decoding, so the wrong shape is unsamplable rather than merely
rejected.

Anthropic exposes the same thing as `tools[].strict`, but two gaps keep it off:
`ToolDefinition.strict` defaults to `None`, which Pydantic AI reads as "off" for
Anthropic (only OpenAI infers it), and the Anthropic profile carries no
`json_schema_transformer`, so the schema Pydantic generates is rejected with
`For 'object' type, 'additionalProperties' must be explicitly set to false`.
This module closes the object nodes and sets the flag.

Off by default. Constrained decoding changes how the model samples, and the
payoff is a few wasted round trips per session, so it is worth opting into
deliberately rather than inheriting.
"""

from dataclasses import replace

from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.tools import ToolDefinition

from pcode.preferences import SETTINGS, load_preferences

# Only `edit_file` has a measured failure: its `replacements` array is the one
# tool argument with a nested object shape. Keeping the set small keeps the
# blast radius small too -- Anthropic validates the schema of tools marked
# strict and 400s the whole request, so MCP and extension tools carrying
# arbitrary schemas must stay out of it.
STRICT_TOOLS = frozenset({"edit_file"})


def strict_tools_enabled() -> bool:
    preferences = load_preferences()
    return preferences.get("strict_tools", SETTINGS["strict_tools"].default) == "on"


def _closed(schema: object) -> object:
    """Copy `schema` with every object node explicitly closed."""
    if isinstance(schema, dict):
        node = {key: _closed(value) for key, value in schema.items()}
        if node.get("type") == "object":
            node["additionalProperties"] = False
        return node
    if isinstance(schema, list):
        return [_closed(item) for item in schema]
    return schema


# Anthropic's strict mode accepts a subset of JSON Schema and rejects the whole
# request on anything outside it, so an upstream schema change could break every
# edit rather than one. An allow-list fails the safe way: an unrecognized keyword
# declines strict and leaves the tool exactly as it behaves today.
SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "title",
        "type",
    }
)

# Keyed by name rather than by keyword, so their keys are not schema vocabulary.
_SCHEMA_MAPS = frozenset({"$defs", "properties"})


def _constrainable(node: object) -> bool:
    """Whether `node` is safe to close and send as strict, and faithful once closed."""
    if isinstance(node, list):
        return all(_constrainable(item) for item in node)
    if not isinstance(node, dict):
        return True
    # A map-valued object: closing it would forbid the keys it exists to carry.
    if node.get("type") == "object" and not node.get("properties"):
        return False
    if node.keys() - SUPPORTED_KEYWORDS:
        return False
    return all(
        all(_constrainable(child) for child in value.values())
        if key in _SCHEMA_MAPS and isinstance(value, dict)
        else _constrainable(value)
        for key, value in node.items()
    )


def prepare_strict_tools(ctx, tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    """Mark the allow-listed tools strict when the model can honor it."""
    model = getattr(ctx, "model", None)
    if getattr(model, "system", None) != "anthropic":
        return tool_defs
    profile = getattr(model, "profile", None)
    # The same gate `AnthropicModel` applies before it will send `strict` at all,
    # so an unsupported model silently keeps the schema it already had.
    if not (profile and profile.get("supports_json_schema_output", False)):
        return tool_defs
    return [
        replace(
            tool_def,
            parameters_json_schema=_closed(tool_def.parameters_json_schema),
            strict=True,
        )
        if tool_def.name in STRICT_TOOLS and _constrainable(tool_def.parameters_json_schema)
        else tool_def
        for tool_def in tool_defs
    ]


def create_strict_tools() -> PrepareTools | None:
    """Return the capability when enabled, or None to leave tool schemas alone."""
    if not strict_tools_enabled():
        return None
    return PrepareTools(prepare_strict_tools)
