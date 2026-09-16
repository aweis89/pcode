"""Shared authentication helpers; credentials are never stored by pcode."""


class LoginError(ValueError):
    """Safe-to-display authentication setup failure."""


def anthropic_model(model: str, key: str):
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    return AnthropicModel(model.removeprefix("anthropic:"), provider=AnthropicProvider(api_key=key))
