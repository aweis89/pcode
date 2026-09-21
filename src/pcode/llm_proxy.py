"""Codex-only proxy transport; never change process-wide HTTP settings."""

import httpx2
from pydantic_ai.models import get_user_agent
from pydantic_ai.providers.openai_codex import OpenAICodexCredentialSource, OpenAICodexProvider


class ProxiedCodexProvider(OpenAICodexProvider):
    """Own a dedicated proxied client, including across repeated agent runs."""

    def __init__(
        self, proxy: str, *, credential_source: OpenAICodexCredentialSource | None = None
    ) -> None:
        # Validate without echoing a URL that may contain proxy credentials.
        try:
            url = httpx2.URL(proxy)
            if url.scheme not in {"http", "https"} or not url.host:
                raise ValueError
        except (ValueError, httpx2.InvalidURL):
            raise ValueError(
                "PCODE_LLM_PROXY must be a valid http:// or https:// proxy URL"
            ) from None
        self._proxy_url = proxy
        client = self._new_http_client()
        super().__init__(credential_source=credential_source, http_client=client)
        # Pydantic AI 2.43's Provider context manager closes owned clients and
        # recreates them on re-entry. Injected clients are otherwise caller-owned.
        # These private ownership hooks are covered by lifecycle regression tests.
        self._own_http_client = client
        self._http_client_factory = self._create_http_client

    def _new_http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            proxy=self._proxy_url,
            trust_env=False,
            timeout=httpx2.Timeout(600, connect=5),
            headers={"User-Agent": get_user_agent()},
        )

    def _create_http_client(self) -> httpx2.AsyncClient:
        client = self._new_http_client()
        client.auth = self._auth
        self._http_client = client
        return client
