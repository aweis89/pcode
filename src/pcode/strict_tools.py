"""Grammar-constrained tool arguments for Anthropic models.

Claude sometimes emits a nested tool argument as an escaped JSON *string* rather
than the array the schema asks for, and botches the escaping while doing it. In
~2k recorded `edit_file` calls the Anthropic share of `replacements` arrivals
failed validation about one time in eight, while the OpenAI share never did.
Across every saved session through 2026-11-17, 11 of
6,231 Anthropic `edit_file` calls (0.2%) came back to the model as validation
retries, and none of 645 OpenAI ones. That OpenAI figure is not strict mode's
doing today: OpenAI applies strict only to a schema it can constrain, and of
the coding tools only `write_file`'s qualifies (2.69); Gemini constrains them all.

Harness's `Coder` turned strict mode off for its tools from Pydantic AI 2.64
(pydantic-ai#11743, a preference for repairing arguments over constraining
them, with no measured failure behind it), and pcode inherited that until it
stopped composing from `Coder`. On OpenAI's and Google's own APIs pcode now
leaves `strict` unset, so they decide (`PROVIDER_STRICT`); on Gemini one
`strict=False` tool would also drop every other tool in the request, MCP ones
included, out of `VALIDATED`. Everywhere else the coding tools keep it off.

Anthropic exposes the same thing as `tools[].strict`, but two gaps keep it off:
`ToolDefinition.strict` defaults to `None`, which Pydantic AI reads as "off" for
Anthropic, and the schema Pydantic generates is rejected with
`For 'object' type, 'additionalProperties' must be explicitly set to false`.
This module closes the object nodes and sets the flag.

It does not close the hole. With `strict: true` confirmed on the wire, Anthropic
still delivers `replacements` as a truncated JSON string a few times in ten when
the model is nudged toward that shape (claude-opus-5, subscription endpoint,
pcode's schema and the plainest `{"type": "array"}` alike), while a scalar
property in the same schema is held to its type. Production moved from 11.5% to
9.6% of `replacements` arrivals failing. Left on because nothing measured got
worse, not because it works; the retry budget is what actually recovers the
edit.
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


# Model systems whose own API turns an unset `strict` into something sound:
# OpenAI constrains the schemas it can, Gemini validates the whole request.
# Elsewhere an unset `strict` can go out as `strict: true` on schemas nobody
# adapted (OpenRouter, Together and Cerebras did, for every coding tool), so
# the coding tools keep it off there.
PROVIDER_STRICT = frozenset({"openai", "openai-codex", "google", "google-cloud"})


def provider_strict_off(names: set[str]) -> PrepareTools:
    """Turn strict mode off for `names` where nothing chose it, except on the
    systems in `PROVIDER_STRICT`, which decide for themselves.
    `create_strict_tools`, later in the list, still opts Anthropic's `edit_file` in.
    """

    def prepare(ctx, tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        if getattr(getattr(ctx, "model", None), "system", None) in PROVIDER_STRICT:
            return tool_defs
        return [
            replace(tool_def, strict=False)
            if tool_def.strict is None and tool_def.name in names
            else tool_def
            for tool_def in tool_defs
        ]

    return PrepareTools(prepare)


def create_strict_tools() -> PrepareTools | None:
    """Return the capability when enabled, or None to leave tool schemas alone."""
    if not strict_tools_enabled():
        return None
    return PrepareTools(prepare_strict_tools)
