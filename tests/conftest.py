import pytest


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path):
    """Tests must neither consume nor overwrite the user's saved defaults."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
