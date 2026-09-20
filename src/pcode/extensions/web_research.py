"""Web search and page fetching, choosing the best backend that is configured.

Search and fetch are separate concerns because provider-native search returns
snippets only: reading documentation needs a page fetch either way. Each half
prefers the model's native tool (Anthropic and OpenAI models search server-side;
Anthropic also fetches) and otherwise exposes a local tool. Exa backs the local
tools when `EXA_API_KEY` is set; DuckDuckGo and a plain HTTP fetch otherwise.

The `web_search` preference picks the policy: `auto` (native when supported),
`local` (never advertise native tools), or `off`. Copy this file to
`~/.config/pcode/extensions/web_research.py` to replace the defaults; an empty
`setup` removes web tools entirely.
"""

import os

SEARCH_RESULTS = 5
PAGE_CHARS = 10_000

INSTRUCTIONS = (
    "You can research the web: search for pages, then fetch the most promising "
    "URLs to read them in full. Start broad and survey several results before "
    "reading. For library and API documentation prefer the project's official "
    "site; many publish `/llms.txt` as an index written for agents. Cite the "
    "URLs you relied on."
)


def _mode() -> str:
    from pcode.preferences import SETTINGS, load_preferences

    return load_preferences().get("web_search", SETTINGS["web_search"].default)


def _exa_tools():
    """Exa's `web_search` and `get_page`, split so native search can hide only the first."""
    from pydantic_ai import Tool
    from pydantic_ai_harness.exa import ExaSearchToolset

    exa = ExaSearchToolset(
        client=None,  # Harness builds the client from EXA_API_KEY; the model never sees it.
        num_results=SEARCH_RESULTS,
        max_text_chars=PAGE_CHARS,
        include_deep_search=False,
    )
    return Tool(exa.web_search, name="web_search"), Tool(exa.get_page, name="get_page")


def _local_tools():
    """DuckDuckGo search and an HTTP fetch, named like Exa's so display and docs agree."""
    from pydantic_ai import Tool
    from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
    from pydantic_ai.common_tools.web_fetch import web_fetch_tool

    duckduckgo = duckduckgo_search_tool(max_results=SEARCH_RESULTS)

    async def web_search(query: str) -> str:
        """Search the web and return matching pages with a short excerpt of each.

        Args:
            query: The search query. Natural-language questions and keyword
                queries both work.
        """
        results = await duckduckgo.function(query)
        if not results:
            return f"No results found for {query!r}."
        return "\n\n".join(f"{item['title']}\n{item['href']}\n{item['body']}" for item in results)

    fetch = web_fetch_tool(max_content_length=PAGE_CHARS)
    fetch.name = "get_page"
    fetch.description = "Retrieve the full text of a specific URL, converted to Markdown."
    return Tool(web_search, name="web_search"), fetch


def setup(pcode) -> None:
    mode = _mode()
    if mode == "off":
        return
    from pydantic_ai.capabilities import WebFetch, WebSearch

    if os.environ.get("EXA_API_KEY", "").strip():
        search, fetch = _exa_tools()
    else:
        search, fetch = _local_tools()
    native = mode == "auto"
    # Ids name the prompt sources in /status; they never reach the model.
    pcode.add_capability(WebSearch(id="web_research", native=native, local=search))
    pcode.add_capability(WebFetch(id="web_fetch", native=native, local=fetch))
    pcode.instructions(INSTRUCTIONS)
