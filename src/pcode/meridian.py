"""Anthropic-compatible Meridian transport with client-owned tool execution."""

import os

import httpx2
from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

DEFAULT_BASE_URL = "http://127.0.0.1:3456"


def meridian_base_url() -> str:
    value = os.environ.get("PCODE_MERIDIAN_BASE_URL", "").strip() or DEFAULT_BASE_URL
    try:
        url = httpx2.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            raise ValueError
        if url.query or url.fragment:
            raise ValueError
    except (ValueError, httpx2.InvalidURL):
        raise ValueError(
            "PCODE_MERIDIAN_BASE_URL must be an HTTP(S) URL without credentials, query, or fragment"
        ) from None
    return value.rstrip("/")


class MeridianProvider(AnthropicProvider):
    @property
    def name(self) -> str:
        return "meridian"

    def __init__(self) -> None:
        base_url = meridian_base_url()

        # Never inherit upstream Anthropic credentials or global HTTP proxies.
        def make_client():
            return httpx2.AsyncClient(trust_env=False, timeout=600)

        client = make_client()
        super().__init__(
            anthropic_client=AsyncAnthropic(
                base_url=base_url,
                api_key=os.environ.get("PCODE_MERIDIAN_API_KEY", "").strip() or "meridian-local",
                auth_token="",
                http_client=client,
                default_headers={"x-meridian-agent": "passthrough"},
            )
        )
        # Empty token prevents environment lookup during construction; None then
        # suppresses the SDK's otherwise empty Bearer header on requests.
        self.client.auth_token = None
        self._own_http_client = client
        self._http_client_factory = make_client


def meridian_model(model: str) -> AnthropicModel:
    name = model.removeprefix("meridian:")
    if not name.strip():
        raise ValueError("Meridian requires a model ID: meridian:<model-id>")
    return AnthropicModel(name, provider=MeridianProvider())
