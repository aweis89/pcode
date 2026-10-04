"""A short title for a saved session, asked of its own model after the first turn.

One small request beside the conversation, not part of it: the first prompt
and the start of the reply go to the session's model at low effort, and the
answer is cleaned into one short line. Nothing here raises past the caller's
`try`: a title is a convenience, and a session without one lists by its
first prompt as before.

No output cap is set on purpose. Reasoning models count thinking against
`max_tokens`, and Anthropic needs it above the thinking budget, so a tight cap
returns nothing or fails; the instruction and low effort keep the answer short.
"""

import asyncio
import re

INSTRUCTIONS = (
    "You name coding-assistant sessions so they can be found again later. "
    "Reply with only a title of 3 to 6 words for the session below: plain text, "
    "no quotes, no trailing punctuation, no prefix such as 'Title:'. Name the task "
    "or topic, not the assistant or the user."
)

# How much of the first exchange the request carries.
PROMPT_CHARS = 2000
REPLY_CHARS = 1500
TITLE_CHARS = 60
TIMEOUT_SECONDS = 60.0

_PREFIX = re.compile(r"^(?:session\s+)?(?:title|name)\s*:\s*", re.IGNORECASE)
# Quotes, Markdown emphasis and closing punctuation, trimmed from both ends.
_TRIM = "\"'`*_#“”‘’.!?:;, \t"


def clean_title(text: str) -> str:
    """One display line from a model's answer, or "" when nothing usable is left."""
    from pcode.tool_display import plain

    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    line = _PREFIX.sub("", line.strip(_TRIM)).strip(_TRIM)
    return plain(" ".join(plain(line, None).split()), TITLE_CHARS)


def request_text(prompt: str, reply: str) -> str:
    text = f"First message from the user:\n{prompt.strip()[:PROMPT_CHARS]}"
    if reply.strip():
        text += f"\n\nStart of the assistant's reply:\n{reply.strip()[:REPLY_CHARS]}"
    return text


async def suggest_title(model: str, prompt: str, reply: str = "", *, workspace=None) -> str:
    """Ask `model` for a title; raises whatever the request raises, or TimeoutError."""
    from pydantic_ai.direct import model_request_stream
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from pcode.agent import side_model

    chosen = await asyncio.to_thread(side_model, model, "low")
    settings = dict(chosen.settings or {})
    # Only a `claude:` model reads these; another provider must not be sent them.
    if getattr(chosen.model, "system", None) == "claude":
        from pcode.claude_sdk.model import KEEP_WARM_SETTING
        from pcode.claude_sdk.workspace import CWD_SETTING

        # A process of its own, stopped once it answers.
        settings[KEEP_WARM_SETTING] = False
        if workspace is not None:
            settings[CWD_SETTING] = str(workspace)
    messages = [
        ModelRequest(parts=[UserPromptPart(request_text(prompt, reply))], instructions=INSTRUCTIONS)
    ]

    async def ask() -> str:
        # Streamed, as every turn is: some subscription endpoints only stream.
        async with model_request_stream(
            chosen.model, messages, model_settings=settings or None
        ) as stream:
            async for _ in stream:
                pass
        response = stream.get()
        return "".join(
            part.content for part in response.parts if getattr(part, "part_kind", "") == "text"
        )

    return clean_title(await asyncio.wait_for(ask(), TIMEOUT_SECONDS))
