"""Anthropic-compatible Meridian transport with client-owned tool execution."""

import os
from dataclasses import replace

import httpx2
from anthropic import AsyncAnthropic
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext
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
        from pcode.meridian_process import managed_endpoint

        endpoint = managed_endpoint()
        base_url = endpoint[0] if endpoint else meridian_base_url()
        api_key = (
            endpoint[1]
            if endpoint
            else os.environ.get("PCODE_MERIDIAN_API_KEY", "").strip() or "meridian-local"
        )

        # Never inherit upstream Anthropic credentials or global HTTP proxies.
        def make_client():
            return httpx2.AsyncClient(trust_env=False, timeout=600)

        client = make_client()
        super().__init__(
            anthropic_client=AsyncAnthropic(
                base_url=base_url,
                api_key=api_key,
                auth_token="",
                http_client=client,
                default_headers={"x-meridian-agent": "passthrough"},
                # The SDK default of 2 would retry 408/409/429/5xx and connection
                # errors silently, outside the runtime's visible retry budget and
                # against the transient-error policy in `diagnostics.py`. The
                # runtime owns transport retries here, as it does for API keys.
                max_retries=0,
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


class MeridianSessionIdentity(AbstractCapability):
    """Bind requests, not shared clients, to the current conversation.

    Harness children inherit the model but get fresh Pydantic conversation IDs.
    This also preserves identity across saved-session resume and model switches.
    """

    async def before_model_request(
        self, ctx: RunContext, request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if request_context.model.system != "meridian":
            return request_context
        settings = dict(request_context.model_settings or {})
        headers = dict(settings.get("extra_headers") or {})
        # Headers are case-insensitive; never leave an alternate-cased stale ID.
        headers = {k: v for k, v in headers.items() if k.lower() != "x-litellm-session-id"}
        headers["x-litellm-session-id"] = ctx.conversation_id
        settings["extra_headers"] = headers
        return replace(request_context, model_settings=settings)
