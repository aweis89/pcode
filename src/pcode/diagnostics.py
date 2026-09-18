"""Useful provider errors without HTTP headers or credential values."""

import os
import re
from importlib.metadata import version

_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"password|secret|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]+)"
)


def redact(text: str) -> str:
    for name, value in os.environ.items():
        if len(value) >= 8 and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY", name):
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    text = _TOKEN.sub("[redacted]", text)
    text = _ASSIGNMENT.sub(r"\1[redacted]", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def error_details(error: BaseException) -> dict:
    """Keep bounded, redacted causes without traceback locals or request objects."""
    return _error_details(error, set())


def _error_details(error: BaseException, seen: set[int]) -> dict:
    if id(error) in seen or len(seen) >= 16:
        return {"type": type(error).__name__, "truncated": True}
    seen.add(id(error))
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return _error_details(error.exceptions[0], seen)
    result = {"type": type(error).__name__}
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        result["status"] = status
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            for key in ("message", "code", "param", "type"):
                value = detail.get(key)
                if isinstance(value, str):
                    result[f"provider_{key}"] = redact(value)[:4000]
    if "provider_message" not in result:
        result["message"] = redact(str(error))[:4000]
    if error.__cause__ is not None:
        result["cause"] = _error_details(error.__cause__, seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        result["context"] = _error_details(error.__context__, seen)
    return result


TRANSIENT_TRANSPORT = frozenset(
    {
        "RemoteProtocolError",
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        "APITimeoutError",
        "IncompleteRead",
    }
)


def transport_types(error: BaseException) -> set[str]:
    """Name every exception in the bounded cause chain, never its text."""
    names, detail = set(), error_details(error)
    while detail:
        names.add(detail["type"])
        detail = detail.get("cause", detail.get("context", {}))
    return names


def transient(error: BaseException) -> bool:
    """Whether the provider dropped the connection rather than answering.

    A status code means the provider did answer: rate limits and server errors
    need their own handling, so they are deliberately not transient here.
    """
    detail = error_details(error)
    while detail:
        if "status" in detail:
            return False
        detail = detail.get("cause", detail.get("context", {}))
    return bool(transport_types(error) & TRANSIENT_TRANSPORT)


def versions() -> dict[str, str]:
    return {
        package: version(package)
        for package in ("pcode", "pydantic-ai-slim", "pydantic-ai-harness")
    }
