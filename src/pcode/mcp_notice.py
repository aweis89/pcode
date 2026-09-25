"""Tell the model which MCP servers are enabled without rewriting its prompt.

Tool search hides deferred MCP tools, so nothing in a request says a server
exists until the model thinks to search, and a model that does not know gdrive
is enabled reaches for `gcloud` instead. The fix has two halves. A fixed
instruction says where the list is; it names no server, so it never changes
during a session. The list itself is appended to history, and only when it
differs from the last one there, so enabling a server mid-conversation leaves
earlier messages (and the provider's cached prefix) untouched. The same check
restores the list after compaction drops it.
"""

from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar

from pydantic_ai.capabilities import AbstractCapability

from pcode.meridian_reminders import append_reminder, last_reminder

TAG = "<mcp-servers>"

INSTRUCTIONS = (
    f"Enabled MCP servers are listed in the latest {TAG} message; without one, none "
    "are enabled. Their tools are named mcp_<server>_<tool>, and any not in your tool "
    "list can be found with tool search. Before concluding that an integration or data "
    "source is unavailable, check that list and search for the matching server's tools."
)

# Set per turn by the runtime. A context variable rather than a capability
# field because the worker is built once, from copies of these capabilities,
# yet must see the servers enabled for the turn that delegated to it.
_servers: ContextVar[tuple[tuple[str, str | None], ...]] = ContextVar("mcp_servers", default=())
# A live view, read at each request: servers fail while the run enters its
# toolsets, after this turn's list was published.
_unavailable: ContextVar[Mapping[str, str]] = ContextVar("mcp_unavailable", default={})


@asynccontextmanager
async def enabled_servers(
    servers: Mapping[str, str | None], unavailable: Mapping[str, str] | None = None
):
    """Publish a turn's enabled servers (name -> description) to its agents."""
    token = _servers.set(tuple(servers.items()))
    down = _unavailable.set(unavailable if unavailable is not None else {})
    try:
        yield
    finally:
        _unavailable.reset(down)
        _servers.reset(token)


def render(servers, unavailable: Mapping[str, str] | None = None) -> str:
    unavailable = unavailable or {}
    if servers:
        lines = "\n".join(
            f"- {name}"
            + (f": {' '.join(description.split())}" if description else "")
            + (
                " (failed to connect this turn; its tools are unavailable)"
                if name in unavailable
                else ""
            )
            for name, description in servers
        )
        body = f"Enabled MCP servers (replaces any earlier list):\n{lines}"
    else:
        body = "No MCP servers are enabled now (replaces any earlier list)."
    return f"{TAG}\n{body}\n</mcp-servers>"


class MCPServers(AbstractCapability):
    """The fixed instruction, plus the enabled-server list whenever it changes."""

    def __init__(self, *, instruct: bool) -> None:
        # Named so /status can attribute the instruction.
        self.id = "mcp_servers"
        self.instruct = instruct

    def get_instructions(self):
        return INSTRUCTIONS if self.instruct else None

    async def before_model_request(self, ctx, request_context):
        servers = _servers.get()
        # "None enabled" only retracts an earlier list; it is never news on its own.
        if servers or last_reminder(request_context.messages, TAG):
            append_reminder(request_context, TAG, render(servers, _unavailable.get()))
        return request_context
