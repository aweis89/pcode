"""The `/model` pick list: rows for the models on offer, and the custom IDs it takes."""

import re
from collections.abc import Iterable

from pcode.models import PROVIDERS
from pcode.pick_ui import PickItem

TITLE = "Choose model"
HEADER = "Type provider:model-id for a custom model."
EMPTY = "No matches. Try another filter or provider:model-id."


def model_items(models: Iterable[str], current: str | None = None) -> list[PickItem]:
    """One row per model, matched by its id or by the provider's name and the model's.

    So `anth opus` and `anthopus` both find anthropic:claude-opus-5.
    """
    items = []
    for model in models:
        provider, _, name = model.partition(":")
        shown = PROVIDERS.get(provider, provider)
        items.append(
            PickItem(
                model,
                f"{shown} · {name}",
                marker="current" if model == current else "",
                search=f"{model}\n{shown} {name}",
            )
        )
    return items


def custom_pattern() -> str:
    """A typed `provider:model-id` the list offers as a row of its own.

    Any supported provider, not only configured ones: the key may be exported
    after launch, and infer_model reports a missing key clearly.
    """
    providers = "|".join(re.escape(provider) for provider in sorted(PROVIDERS))
    return rf"(?:{providers}):[A-Za-z0-9_.:/-]+"
