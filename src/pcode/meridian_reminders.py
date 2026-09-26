"""Durable reminder helper and Meridian-specific limit warnings."""

import re
from collections.abc import Callable
from dataclasses import replace

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.compaction import WarnNearLimits

from pcode.model_metadata import ContextWindowError, context_window, refresh_context

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
    """Warn against pcode's resolved window, and never remove sent Meridian text."""

    async def _for_model(self, model) -> WarnNearLimits:
        """This capability, bound to the window the footer and compaction use.

        Harness resolves windows from genai-prices, which knows no `meridian:` ids
        and silently assumes 200k (a 1M Meridian session was told it was 90% full
        at 162k), and gives `openai-codex:` the direct API's window by model name
        (1.05M where Codex serves 272k). Explicit windows and models pcode cannot
        resolve keep Harness's own resolution.
        """
        if self.max_context_fraction is None or self.context_window is not None:
            return self
        await refresh_context(model)
        try:
            window = context_window(model)
        except ContextWindowError:
            # Compaction reports the actionable configuration error.
            window = None
        return self if window is None else replace(self, context_window=window)

    async def before_model_request(self, ctx, request_context):
        warnings = await self._for_model(request_context.model)
        if request_context.model.system != "meridian":
            return await WarnNearLimits.before_model_request(warnings, ctx, request_context)
        original = request_context.messages
        if not original or not isinstance(original[-1], ModelRequest):
            return request_context
        # Upstream strips prior warnings and appends exactly one request when a
        # threshold fires. Evaluate on a disposable list; retain the real history.
        clean_count = len(self._strip_old_warnings(original))
        candidate = await WarnNearLimits.before_model_request(
            warnings, ctx, replace(request_context, messages=list(original))
        )
        if len(candidate.messages) > clean_count:
            # Warn at percentage deciles rather than on every token increase.
            # Keep severity and warning kinds so new limits/escalations still fire.
            append_reminder(
                request_context, LIMITS_TAG, candidate.messages[-1].parts[0].content, _decile_key
            )
        return request_context
