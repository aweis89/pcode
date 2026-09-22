import json
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition

from pcode.agent import create_coder
from pcode.strict_tools import _closed, _constrainable, prepare_strict_tools


@pytest.fixture
def preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def write(**values):
        path = tmp_path / "config" / "pcode"
        path.mkdir(parents=True, exist_ok=True)
        (path / "preferences.json").write_text(json.dumps(values))

    return write


def context(*, system="anthropic", supported=True):
    profile = {"supports_json_schema_output": supported}
    return SimpleNamespace(model=SimpleNamespace(system=system, profile=profile))


def edit_file_schema(workspace):
    """The real `edit_file` schema, so the transform is checked against upstream's."""
    from pydantic_ai_harness.filesystem import FileSystem

    toolset = FileSystem(root_dir=workspace).get_toolset()
    return toolset.tools["edit_file"].function_schema.json_schema


def definition(name="edit_file", schema=None):
    return ToolDefinition(name=name, parameters_json_schema=schema or {"type": "object"})


def test_closes_every_object_node_including_nested_defs(tmp_path):
    closed = _closed(edit_file_schema(tmp_path))

    assert closed["additionalProperties"] is False
    # `replacements` items live behind a `$ref`, so the definition must be closed too.
    assert closed["$defs"]["Replacement"]["additionalProperties"] is False


def test_closing_leaves_the_original_schema_untouched(tmp_path):
    schema = edit_file_schema(tmp_path)
    before = json.dumps(schema, sort_keys=True)

    _closed(schema)

    assert json.dumps(schema, sort_keys=True) == before


def test_closing_keeps_optional_and_referenced_shapes(tmp_path):
    """Anthropic allows optional properties, so nothing needs forcing into `required`."""
    closed = _closed(edit_file_schema(tmp_path))

    assert closed["required"] == ["path"]
    assert closed["properties"]["old_text"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert closed["properties"]["replacements"]["anyOf"][0]["items"] == {
        "$ref": "#/$defs/Replacement"
    }


def test_marks_allow_listed_tools_strict(tmp_path):
    schema = edit_file_schema(tmp_path)

    prepared = prepare_strict_tools(context(), [definition(schema=schema)])

    assert prepared[0].strict is True
    assert prepared[0].parameters_json_schema["additionalProperties"] is False


def test_leaves_tools_outside_the_allow_list_alone(tmp_path):
    original = definition(name="shell", schema=edit_file_schema(tmp_path))

    prepared = prepare_strict_tools(context(), [original])

    assert prepared[0] is original


def test_skips_non_anthropic_models(tmp_path):
    """OpenAI already infers strict mode; Pydantic AI ignores the flag elsewhere."""
    original = definition(schema=edit_file_schema(tmp_path))

    assert prepare_strict_tools(context(system="openai"), [original]) == [original]


def test_skips_models_without_structured_output_support(tmp_path):
    """The same gate `AnthropicModel` applies before it will send `strict` at all."""
    original = definition(schema=edit_file_schema(tmp_path))

    assert prepare_strict_tools(context(supported=False), [original]) == [original]


def test_skips_schemas_with_a_free_form_object():
    """Closing a map-valued property would forbid the keys it exists to carry."""
    schema = {
        "type": "object",
        "properties": {"env": {"type": "object", "description": "Extra variables"}},
    }
    original = definition(schema=schema)

    assert _constrainable(schema) is False
    assert prepare_strict_tools(context(), [original]) == [original]


def test_skips_schemas_using_keywords_anthropic_rejects():
    """An upstream schema change must cost the constraint, not every edit.

    Anthropic 400s the whole request for an unsupported keyword, so declining is
    the only safe answer to one we have not verified.
    """
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
    }
    original = definition(schema=schema)

    assert _constrainable(schema) is False
    assert prepare_strict_tools(context(), [original]) == [original]


def test_property_and_definition_names_are_not_read_as_keywords(tmp_path):
    """`properties` and `$defs` are name -> schema maps, so their keys are free."""
    assert _constrainable(edit_file_schema(tmp_path)) is True


def test_reaches_the_anthropic_wire_format(tmp_path):
    """The flag is only useful if `AnthropicModel` agrees to send it.

    Guards the two conditions in `_map_tool_definition`: a truthy `strict` and a
    profile advertising structured-output support. Either one missing drops the
    flag silently, leaving the schema unconstrained with nothing to notice.
    """
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.profiles.anthropic import anthropic_model_profile

    profile = anthropic_model_profile("claude-opus-5")
    assert profile.get("supports_json_schema_output") is True

    # A real `ModelProfile`, not a dict, so the `.get` in the gate is exercised too.
    anthropic = SimpleNamespace(model=SimpleNamespace(system="anthropic", profile=profile))
    prepared = prepare_strict_tools(anthropic, [definition(schema=edit_file_schema(tmp_path))])[0]
    param = AnthropicModel._map_tool_definition(
        SimpleNamespace(profile=profile), prepared, {}, visibility="visible"
    )

    assert param["strict"] is True
    assert param["input_schema"]["additionalProperties"] is False
    assert param["input_schema"]["$defs"]["Replacement"]["additionalProperties"] is False


def installed(workspace) -> bool:
    return any(
        isinstance(capability, PrepareTools) and capability.prepare_func is prepare_strict_tools
        for capability in create_coder(workspace).capabilities
    )


def test_installed_by_default(preferences, tmp_path):
    preferences()

    assert installed(tmp_path)


def test_not_installed_when_disabled(preferences, tmp_path):
    preferences(strict_tools="off")

    assert not installed(tmp_path)


def strict_tool_names(workspace) -> set[str]:
    seen: set[str] = set()

    async def model(messages, info):
        seen.update(tool.name for tool in info.function_tools if tool.strict)
        yield "Done"

    Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)]).run_sync(
        "inspect"
    )
    return seen


def test_leaves_other_providers_unstrict(preferences, tmp_path):
    """The model is the gate, not the preference: FunctionModel is not Anthropic."""
    preferences(strict_tools="on")

    assert strict_tool_names(tmp_path) == set()
