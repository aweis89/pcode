"""Follow OpenAI's redirect to a project's regional API host.

A project with data residency only accepts requests at its regional host, and
its key looks like any other, so nothing tells pcode in advance. The default
host rejects every request with a 401 naming the right one:

    code: incorrect_hostname
    message: ... Please make your request to us.api.openai.com

Retrying unchanged fails forever, so the runtime repoints OpenAI at the named
host and resends once. Only a host under `api.openai.com` is followed: the
error body is untrusted, and following it anywhere else would hand the API key
to whatever host it named. A user's own `OPENAI_BASE_URL` (a proxy or gateway)
or a client built for another URL is never touched.

The regional URL is kept here rather than exported as `OPENAI_BASE_URL`: the
variable would leak into every shell command the agent runs and could not
tell pcode's choice from the user's. Instead pcode passes each OpenAI model it
builds through `track`, which repoints it now if the host is already known, or
later when it is learned. That covers models built before the 401 too, such as
the sub-agent menu resolved at startup.
"""

import os
import re
import threading
import weakref
from urllib.parse import urlsplit

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel

DEFAULT_HOST = "api.openai.com"
_REGIONAL = re.compile(r"\b((?:[a-z0-9-]+\.)+api\.openai\.com)\b")

# The regional API URL once learned, and every default-host OpenAI client
# pcode built, so learning it repoints all of them at once.
_regional_url: str | None = None
_clients: weakref.WeakSet = weakref.WeakSet()
# Isolated workers resolve models on another thread.
_lock = threading.Lock()


def regional_host(error: BaseException) -> str | None:
    """The regional host an `incorrect_hostname` rejection names, if it is one to trust."""
    if not isinstance(error, ModelHTTPError) or not isinstance(error.body, dict):
        return None
    if error.body.get("code") != "incorrect_hostname":
        return None
    match = _REGIONAL.search(str(error.body.get("message", "")).lower())
    return match.group(1) if match else None


def is_regional_url(url: object) -> bool:
    """Whether `url` is an OpenAI regional API endpoint, as `use_regional_host` builds."""
    try:
        parts = urlsplit(str(url))
        return (
            parts.scheme == "https"
            and parts.port is None
            and _REGIONAL.fullmatch(parts.hostname or "") is not None
            and parts.path.rstrip("/") == "/v1"
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        return False


def track[M: Model | str](model: M) -> M:
    """Remember an OpenAI model's client, repointed already if the region is known."""
    client = _default_openai_client(model)
    if client is not None:
        with _lock:
            _clients.add(client)
            if _regional_url is not None:
                client.base_url = _regional_url
    return model


def use_regional_host(host: str, models: list[Model | str | None]) -> bool:
    """Send pcode's OpenAI requests to `host` from now on; whether any client moved.

    `models` are the failed turn's, tracked here in case nothing built them
    through `track`. Nothing changes when the user chose a base URL or none of
    those models is on the default host, since then the retry would only fail
    the same way.
    """
    global _regional_url
    if os.environ.get("OPENAI_BASE_URL", "").strip():
        return False
    if not any(_default_openai_client(model) for model in models):
        return False
    for model in models:
        track(model)
    with _lock:
        _regional_url = f"https://{host}/v1"
        for client in list(_clients):
            client.base_url = _regional_url
    return True


def _default_openai_client(model: Model | str | None):
    """The model's OpenAI client if it targets the default host."""
    while isinstance(model, WrapperModel):
        model = model.wrapped
    if not isinstance(model, Model):
        return None
    try:
        provider = model.provider
        if provider is None or provider.name != "openai":
            return None
        client = provider.client
        if urlsplit(str(client.base_url)).hostname != DEFAULT_HOST:
            return None
    except Exception:
        return None
    return client
