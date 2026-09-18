"""pcode's own Anthropic subscription sign-in: browser OAuth with PKCE.

`/login` opens claude.ai in a browser, receives the authorization code on a
loopback callback, exchanges it for tokens, and stores them under pcode's config
directory with owner-only permissions. pcode then owns the refresh token, so no
other agent installation is required for subscription access.

This authenticates as the public Claude Code client against an endpoint scoped to
it. It is compatibility support, not an official third-party OAuth integration:
entitlements and server behavior can change at any time. The supported path
remains `ANTHROPIC_API_KEY`. Never log, echo, or copy token values.
"""

import asyncio
import base64
import hashlib
import json
import math
import os
import secrets
import tempfile
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx2
from anthropic import AsyncAnthropic
from filelock import FileLock
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from pcode.auth import LoginError, SubscriptionOAuthWire

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
DEFAULT_CALLBACK_PORT = 54545
# Renew before the server's own expiry so an in-flight turn never races it.
EARLY_REFRESH_SECONDS = 300
TOKEN_TIMEOUT_SECONDS = 30.0
LOGIN_TIMEOUT_SECONDS = 300.0

_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8>"
    "<title>pcode</title></head><body style='font-family:sans-serif;padding:3rem'>"
    "<h1>{title}</h1><p>{message}</p></body></html>"
)


def callback_port() -> int:
    """Loopback port for the redirect URI; override it when 54545 is taken."""
    value = os.environ.get("PCODE_OAUTH_CALLBACK_PORT", "").strip()
    if not value:
        return DEFAULT_CALLBACK_PORT
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise LoginError("PCODE_OAUTH_CALLBACK_PORT must be a port number between 1 and 65535.")
    return int(value)


def redirect_uri(port: int) -> str:
    # `localhost` (not the bound literal) is what the browser is redirected to.
    return f"http://localhost:{port}{CALLBACK_PATH}"


def credentials_path() -> Path:
    override = os.environ.get("PCODE_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "pcode" / "credentials.json"


def have_credentials(path: Path | None = None) -> bool:
    """Existence check only; the picker must never read a stored token."""
    try:
        return (path if path is not None else credentials_path()).is_file()
    except OSError:
        return False


def anthropic_auth_source() -> str:
    """Resolve which Anthropic credential to use: api-key, oauth, or pi.

    `PCODE_ANTHROPIC_AUTH` is authoritative when set. Otherwise pcode's own
    stored sign-in wins over `ANTHROPIC_API_KEY`, so `/login` keeps applying
    after a restart; `PCODE_ANTHROPIC_AUTH=api-key` opts back out.
    """
    source = os.environ.get("PCODE_ANTHROPIC_AUTH", "").strip()
    if source:
        return source
    return "oauth" if have_credentials() else "api-key"


@dataclass(frozen=True)
class OAuthTokens:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    expires_at: float

    @property
    def stale(self) -> bool:
        return self.expires_at <= time.time()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _token(value: object) -> str | None:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return None
    return value


def read_tokens(path: Path) -> OAuthTokens:
    """Load pcode's own stored credential, never another tool's auth file."""
    try:
        # Bound malformed input and avoid echoing parser exceptions or contents.
        with path.open("r", encoding="utf-8-sig") as source:
            contents = source.read(1024 * 1024 + 1)
        if len(contents) > 1024 * 1024:
            raise ValueError
        data = json.loads(contents)
        entry = data.get("anthropic") if isinstance(data, dict) else None
        if not isinstance(entry, dict) or entry.get("type") != "oauth":
            raise ValueError
        access, refresh = _token(entry.get("access")), _token(entry.get("refresh"))
        expires_at = _number(entry.get("expires_at"))
        if access is None or refresh is None or expires_at is None:
            raise ValueError
        return OAuthTokens(access, refresh, expires_at)
    except Exception:
        raise LoginError(
            "No usable pcode Anthropic login. Run /login to sign in with your "
            "Anthropic account, or set ANTHROPIC_API_KEY for API-key access."
        ) from None


def write_tokens(path: Path, tokens: OAuthTokens) -> None:
    """Replace the credential file atomically, owner-readable only."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "anthropic": {
            "type": "oauth",
            "access": tokens.access,
            "refresh": tokens.refresh,
            "expires_at": tokens.expires_at,
        }
    }
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            name = file.name
            # Restrict before any token bytes reach the filesystem.
            os.chmod(file.fileno(), 0o600)
            json.dump(payload, file, indent=2)
            file.write("\n")
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def delete_tokens(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise LoginError(f"Could not remove the stored Anthropic login: {path}") from None


def _parse_tokens(data: object, previous_refresh: str = "") -> OAuthTokens:
    entry = data if isinstance(data, dict) else {}
    access = _token(entry.get("access_token"))
    # A refresh response may legitimately omit a new refresh token.
    refresh = _token(entry.get("refresh_token")) or previous_refresh
    lifetime = _number(entry.get("expires_in"))
    if access is None or not refresh or lifetime is None:
        raise LoginError("Anthropic returned an unusable token response. Try /login again.")
    return OAuthTokens(access, refresh, time.time() + max(lifetime, 0) - EARLY_REFRESH_SECONDS)


def _post_tokens(payload: dict[str, str], transport=None) -> object:
    """Call the token endpoint. Errors never include bodies or credentials."""
    try:
        with httpx2.Client(transport=transport, timeout=TOKEN_TIMEOUT_SECONDS) as client:
            response = client.post(
                TOKEN_URL,
                json={"client_id": CLIENT_ID, **payload},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except Exception:
        raise LoginError(
            f"Could not reach Anthropic's token endpoint ({urlsplit(TOKEN_URL).netloc}). "
            "Check your network connection, then retry."
        ) from None
    if response.status_code in (400, 401, 403):
        raise LoginError(
            f"Anthropic rejected the sign-in ({response.status_code}). Run /login to sign in again."
        )
    if response.status_code >= 400:
        raise LoginError(
            f"Anthropic's token endpoint failed ({response.status_code}). Retry in a moment."
        )
    try:
        return response.json()
    except Exception:
        raise LoginError(
            "Anthropic returned an unreadable token response. Try /login again."
        ) from None


def refresh_tokens(path: Path, *, force: bool = False, transport=None) -> OAuthTokens:
    """Renew the stored credential, serialized across pcode processes."""
    with FileLock(str(path) + ".lock", timeout=30):
        # Another process may have refreshed while this one waited for the lock.
        tokens = read_tokens(path)
        if not force and not tokens.stale:
            return tokens
        fresh = _parse_tokens(
            _post_tokens(
                {"grant_type": "refresh_token", "refresh_token": tokens.refresh}, transport
            ),
            tokens.refresh,
        )
        write_tokens(path, fresh)
        return fresh


class StoredLogin:
    """Async access-token provider for the Anthropic SDK's `credentials=` hook.

    The SDK's `TokenCache` caches the token in memory, refreshes it proactively,
    and sets `force_refresh` after a 401, so the file is read and the endpoint
    called only at those boundaries. Blocking file/HTTP work runs off the loop.
    """

    def __init__(self, path: Path | None = None, *, transport=None):
        self.path = path if path is not None else credentials_path()
        self._transport = transport

    def _resolve(self, force_refresh: bool) -> OAuthTokens:
        tokens = read_tokens(self.path)
        if force_refresh or tokens.stale:
            return refresh_tokens(self.path, force=force_refresh, transport=self._transport)
        return tokens

    async def __call__(self, *, force_refresh: bool = False):
        from anthropic.lib.credentials import AccessToken

        tokens = await asyncio.to_thread(self._resolve, force_refresh)
        return AccessToken(token=tokens.access, expires_at=int(tokens.expires_at))


class AnthropicOAuthModel(SubscriptionOAuthWire, AnthropicModel):
    """Anthropic transport authenticated by pcode's own stored subscription login."""

    _subscription_oauth = True

    def __init__(
        self,
        model: str,
        *,
        path: Path | None = None,
        http_client=None,
        transport=None,
    ):
        login = StoredLogin(path, transport=transport)
        # Fail here, not mid-turn, when there is nothing usable to sign with.
        read_tokens(login.path)
        # Do not inherit API keys, bearer tokens, or base URLs from the
        # environment: `credentials=` already suppresses credential env lookups.
        client = AsyncAnthropic(
            credentials=login,
            base_url="https://api.anthropic.com",
            http_client=http_client,
        )
        super().__init__(
            model.removeprefix("anthropic:"),
            provider=AnthropicProvider(anthropic_client=client),
        )


def pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge) as base64url text without padding."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(challenge: str, state: str, port: int) -> str:
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri(port),
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # This flow echoes the verifier back as `state`; the callback and the
            # token exchange both compare against it, as pi and Claude Code do.
            "state": state,
        }
    )


def _callback_result(target: str, state: str) -> tuple[str, str, str]:
    """Return (code, page title, page message) for one callback request."""
    query = urlsplit(target)
    if query.path != CALLBACK_PATH:
        return "", "Not found", "This is not the pcode sign-in callback."
    fields = dict(parse_qsl(query.query))
    if fields.get("error"):
        return "", "Sign-in failed", "Anthropic did not complete the sign-in."
    if not fields.get("code") or not fields.get("state"):
        return "", "Sign-in failed", "The callback was missing its code or state."
    if not secrets.compare_digest(fields["state"], state):
        return "", "Sign-in failed", "The callback state did not match this sign-in."
    return fields["code"], "Signed in", "pcode received the authorization. You can close this tab."


async def _receive_code(port: int, state: str, timeout: float) -> str:
    """Serve the loopback callback until the browser delivers a valid code."""
    loop = asyncio.get_running_loop()
    received: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        code, title, message = "", "Sign-in failed", "The callback request was unreadable."
        try:
            request = await asyncio.wait_for(reader.readline(), 10)
            parts = request.decode("latin-1", "replace").split()
            if len(parts) >= 2 and parts[0] == "GET":
                code, title, message = _callback_result(parts[1], state)
            # Drain the (bounded) header block so the browser sees a clean reply.
            while (line := await asyncio.wait_for(reader.readline(), 10)) not in (
                b"\r\n",
                b"\n",
                b"",
            ):
                if len(line) > 8192:
                    break
            body = _PAGE.format(title=title, message=message).encode("utf-8")
            status = "200 OK" if code else "400 Bad Request"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        except (OSError, asyncio.TimeoutError, UnicodeError):
            code = ""
        finally:
            writer.close()
        if code and not received.done():
            received.set_result(code)

    try:
        server = await asyncio.start_server(handle, CALLBACK_HOST, port)
    except OSError:
        raise LoginError(
            f"Port {port} is already in use, so the sign-in callback cannot start. "
            "Close the other login, or set PCODE_OAUTH_CALLBACK_PORT to a free port."
        ) from None
    async with server:
        try:
            return await asyncio.wait_for(received, timeout)
        except asyncio.TimeoutError:
            raise LoginError("Anthropic sign-in timed out. Run /login to try again.") from None


async def login(
    *,
    path: Path | None = None,
    notify=None,
    open_browser: bool = True,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    transport=None,
) -> OAuthTokens:
    """Run the browser sign-in and store the resulting credential."""
    path = path if path is not None else credentials_path()
    port = callback_port()
    verifier, challenge = pkce_pair()
    url = authorization_url(challenge, verifier, port)
    if notify is not None:
        notify(url)
    if open_browser:
        # Opening a browser can block on some desktops; never stall the terminal.
        await asyncio.to_thread(_open_browser, url)
    code = await _receive_code(port, verifier, timeout)
    tokens = await asyncio.to_thread(
        lambda: _parse_tokens(
            _post_tokens(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "state": verifier,
                    "redirect_uri": redirect_uri(port),
                    "code_verifier": verifier,
                },
                transport,
            )
        )
    )
    await asyncio.to_thread(write_tokens, path, tokens)
    return tokens


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        # A headless machine is expected; the URL was already displayed.
        pass
