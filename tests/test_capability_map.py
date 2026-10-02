"""`dev/capabilities.md` lists every pcode capability, and nothing that is gone."""

import ast
import importlib
import inspect
import pkgutil
import re
import sys
from pathlib import Path

from pydantic_ai.capabilities import AbstractCapability

import pcode

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "dev" / "capabilities.md"
SOURCE = ROOT / "src" / "pcode"
FIX = "Update dev/capabilities.md to match the code."


def _capability_classes() -> set[str]:
    for module in pkgutil.walk_packages(pcode.__path__, "pcode."):
        importlib.import_module(module.name)
    return {
        cls.__name__
        for name, module in list(sys.modules.items())
        if name == "pcode" or name.startswith("pcode.")
        for cls in vars(module).values()
        if inspect.isclass(cls) and cls.__module__ == name and issubclass(cls, AbstractCapability)
    }


def _defined_classes() -> set[str]:
    """Every class statement under src/pcode, nested ones included."""
    return {
        node.name
        for path in SOURCE.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
    }


def _table_classes(text: str) -> set[str]:
    return set(re.findall(r"^\| `(\w+)` \|", text, flags=re.MULTILINE))


def test_every_capability_is_mapped():
    text = MAP.read_text()
    missing = sorted(name for name in _capability_classes() if f"`{name}`" not in text)
    assert not missing, f"Capabilities missing from dev/capabilities.md: {missing}. {FIX}"


def test_every_mapped_class_still_exists():
    listed = _table_classes(MAP.read_text())
    assert listed, "dev/capabilities.md has no table rows; did its format change?"
    stale = sorted(listed - _defined_classes())
    assert not stale, f"dev/capabilities.md lists classes no longer in src/pcode: {stale}. {FIX}"
