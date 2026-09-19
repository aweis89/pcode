"""Durable reminder helper and Meridian-specific limit warnings."""

import re
from dataclasses import replace

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.compaction import WarnNearLimits

# Keep the persisted metadata key so existing session histories still deduplicate.
_MARKER = "pcode_meridian_reminder"


def append_reminder(request_context, kind: str, key: str, text: str) -> None:
    """Deduplicate against persisted branch history, not process-local state."""
    messages = request_context.messages
    if not messages or not isinstance(messages[-1], ModelRequest):
        return
    for message in reversed(messages):
        marker = (message.metadata or {}).get(_MARKER)
        if marker and marker.get("kind") == kind:
            if marker.get("key") == key:
                return
            break
    messages.append(
        ModelRequest(
            parts=[UserPromptPart(content=text)],
            metadata={_MARKER: {"kind": kind, "key": key}},
        )
    )


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
            text = candidate.messages[-1].parts[0].content
            # Warn at percentage deciles rather than on every token increase.
            # Keep severity and warning kinds so new limits/escalations still fire.
            key = re.sub(r"\d+", "#", text)
            key += str([int(p) // 10 for p in re.findall(r"(\d+)%", text)])
            append_reminder(request_context, "limits", key, text)
        return request_context
