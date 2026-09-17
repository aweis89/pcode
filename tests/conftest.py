import pytest


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path):
    """Tests must neither consume nor overwrite the user's saved defaults."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("PCODE_MCP_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def isolated_context_catalog(monkeypatch, tmp_path):
    """Never fetch real metadata or read user caches in ordinary unit tests.

    Adapter tests construct their own ContextCatalog with mocked transports.
    """
    from unittest.mock import AsyncMock

    from pcode import model_metadata

    catalog = model_metadata.ContextCatalog(tmp_path / "metadata.json")
    monkeypatch.setattr(catalog, "refresh", AsyncMock())
    monkeypatch.setattr(model_metadata, "catalog", catalog)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("PCODE_CONTEXT_WINDOW", raising=False)
