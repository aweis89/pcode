"""Durable reminder helper and Meridian-specific limit warnings."""

import re
from collections.abc import Callable
from dataclasses import replace

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.compaction import WarnNearLimits

PLAN_TAG = "<plan-reminder>"
LIMITS_TAG = "[WarnNearLimits]"


def last_reminder(messages, tag: str) -> str | None:
    """Return the most recent reminder text for `tag`, or None when none was sent.

    Detection is by content, not message metadata: Pydantic AI merges consecutive
    `ModelRequest`s when history is resumed and keeps only its own reserved
    metadata namespace, so a marker stored there survives a run but not a resume.
    Only a `UserPromptPart` that *starts* with the tag counts, so a tool result or
    quoted prompt that merely mentions it is not mistaken for a sent reminder.
    """
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            content = getattr(part, "content", None)
            if isinstance(part, UserPromptPart) and isinstance(content, str):
                if content.startswith(tag):
                    return content
    return None


def append_reminder(
    request_context, tag: str, text: str, normalize: Callable[[str], str] | None = None
) -> None:
    """Append `text` as durable history unless the last reminder already says it.

    Appending, rather than moving a mutable tail, keeps previously sent messages
    byte-identical so the provider's cached prefix stays reusable as the
    conversation grows. `normalize` collapses differences that should not count
    as a change (for example, warning percentages within the same decile).
    """
    messages = request_context.messages
    if not messages or not isinstance(messages[-1], ModelRequest):
        return
    previous = last_reminder(messages, tag)
    key = normalize or (lambda value: value)
    if previous is not None and key(previous) == key(text):
        return
    messages.append(ModelRequest(parts=[UserPromptPart(content=text)]))


def _decile_key(text: str) -> str:
    """Treat percentages within the same decile as the same warning."""
    return re.sub(r"\d+", "#", text) + str([int(p) // 10 for p in re.findall(r"(\d+)%", text)])


class MeridianLimitWarnings(WarnNearLimits):
    """Keep upstream warning calculation, but never remove sent Meridian text."""

    async def before_model_request(self, ctx, request_context):
        if request_context.model.system != "meridian":
            return await super().before_model_request(ctx, request_context)
        original = request_context.messages
        if not original or not isinstance(original[-1], ModelRequest):
            return request_context
        # Upstream strips prior warnings and appends exactly one request when a
        # threshold fires. Evaluate on a disposable list; retain the real history.
        clean_count = len(self._strip_old_warnings(original))
        candidate = await super().before_model_request(
            ctx, replace(request_context, messages=list(original))
        )
        if len(candidate.messages) > clean_count:
            # Warn at percentage deciles rather than on every token increase.
            # Keep severity and warning kinds so new limits/escalations still fire.
            append_reminder(
                request_context, LIMITS_TAG, candidate.messages[-1].parts[0].content, _decile_key
            )
        return request_context
