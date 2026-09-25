"""Anthropic-compatible Meridian transport with client-owned tool execution."""

import hashlib
import os
from dataclasses import replace
from functools import cached_property

import httpx2
from anthropic import AsyncAnthropic
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart
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


def _failed_url(error: BaseException) -> str | None:
    """URL of the request an SDK error came from, found along its cause chain."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        try:
            # httpx raises RuntimeError, not AttributeError, for an unset request.
            url = getattr(getattr(error, "request", None), "url", None)
        except Exception:
            url = None
        if url is not None:
            return str(url)
        error = error.__cause__ or error.__context__
    return None


def failure_hint(error: BaseException) -> str | None:
    """What to do about a failed Meridian request, or None for any other provider."""
    from pcode.diagnostics import error_details, transport_types
    from pcode.meridian_process import managed_base_url

    url = _failed_url(error)
    if url is None:
        return None
    managed = managed_base_url()
    try:
        external = meridian_base_url()
    except ValueError:
        external = None
    base = next((b for b in (managed, external) if b and url.startswith(b + "/")), None)
    if base is None:
        return None
    detail = error_details(error)
    while detail and "status" not in detail:
        detail = detail.get("cause", detail.get("context", {}))
    if detail.get("status") == 401:
        if "api key" in detail.get("provider_message", "").lower():
            return f"Meridian at {base} rejected pcode's key. Check PCODE_MERIDIAN_API_KEY."
        return "Meridian could not use your Claude login. Run /login meridian, then retry."
    if transport_types(error) & {"APIConnectionError", "ConnectError"}:
        if base == managed:
            return "pcode's Meridian stopped and is being restarted. Retry in a moment."
        return (
            f"Meridian is not answering at {base}. Start it with `meridian`, or restart "
            "pcode with meridian_managed set to auto or on so it runs its own."
        )
    return None


def thinking_passthrough(base_url: str, api_key: str | None) -> bool | None:
    """Whether the proxy forwards readable thinking; None when it cannot say.

    A read-only settings lookup with a short timeout: this never changes a
    shared proxy's configuration.
    """
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        response = httpx2.get(
            base_url.rstrip("/") + "/settings/api/features",
            headers=headers,
            timeout=0.5,
            trust_env=False,
        )
        value = response.json().get("passthrough", {}).get("thinkingPassthrough")
    except (httpx2.HTTPError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


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


class MeridianModel(AnthropicModel):
    @cached_property
    def profile(self):
        # Meridian answers any Anthropic server tool (`web_search_*`, `web_fetch_*`)
        # with a 400: the Claude Max path cannot emit server_tool_use blocks. Drop
        # them from the profile so the web capabilities fall back to local tools.
        #
        # Tool search fails quietly rather than with a 400. Meridian re-registers
        # client tools with the Agent SDK, which drops `defer_loading` and
        # `tool_reference`: the model sees every deferred schema, but no search
        # result it could produce counts as discovery, so Pydantic AI refuses each
        # deferred call as "not available yet". Use the local `search_tools` and
        # no wire deferral: hidden tools are withheld, then sent in full once found.
        from pydantic_ai.native_tools import WebFetchTool, WebSearchTool
        from pydantic_ai.native_tools._tool_search import ToolSearchTool
        from pydantic_ai.profiles import SUPPORTED_NATIVE_TOOLS, merge_profile
        from pydantic_ai.profiles.anthropic import AnthropicModelProfile

        profile = super().profile
        native = profile.get("supported_native_tools", SUPPORTED_NATIVE_TOOLS)
        unsupported = {WebSearchTool, WebFetchTool, ToolSearchTool}
        return merge_profile(
            profile,
            AnthropicModelProfile(
                supported_native_tools=native - unsupported,
                tool_deferral_mode=None,
                tool_addition_mode=None,
            ),
        )


def meridian_model(model: str) -> AnthropicModel:
    name = model.removeprefix("meridian:")
    if not name.strip():
        raise ValueError("Meridian requires a model ID: meridian:<model-id>")
    return MeridianModel(name, provider=MeridianProvider())


def compaction_summary(messages) -> str | None:
    """The summary text when compaction has replaced the start of `messages`."""
    from pcode.compaction import SUMMARY_PREFIX

    first = messages[0] if messages else None
    if not isinstance(first, ModelRequest):
        return None
    for part in first.parts:
        if isinstance(part, UserPromptPart) and isinstance(part.content, str):
            if part.content.startswith(SUMMARY_PREFIX):
                return part.content
    return None


def session_identity(conversation_id: str, messages) -> str:
    """Meridian session for this history, which changes each time it is compacted.

    Meridian classifies a compacted history whose recent messages still match as a
    continuation and resumes the uncompacted transcript, so the summary never
    reaches the model. A session keyed on the summary starts fresh instead, once
    per compaction; an uncompacted history keeps the plain conversation ID.
    """
    summary = compaction_summary(messages)
    if summary is None:
        return conversation_id
    return f"{conversation_id}.{hashlib.sha256(summary.encode()).hexdigest()[:16]}"


class MeridianSessionIdentity(AbstractCapability):
    """Bind requests, not shared clients, to the current conversation.

    Harness children inherit the model but get fresh Pydantic conversation IDs.
    This also preserves identity across saved-session resume and model switches,
    and moves to a new session after compaction (see `session_identity`).
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
        headers["x-litellm-session-id"] = session_identity(
            ctx.conversation_id, request_context.messages
        )
        settings["extra_headers"] = headers
        return replace(request_context, model_settings=settings)
