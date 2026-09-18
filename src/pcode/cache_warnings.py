"""Route Harness cache-collapse warnings through the normal event stream."""

import warnings
from dataclasses import dataclass

from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning, WarnOnCacheBusts

from pcode.tool_display import command_text


@dataclass(kw_only=True)
class CacheBustEvent(CapabilityEvent, namespace="pcode_cache", name="bust"):
    text: str


class CacheBustReporting(WarnOnCacheBusts):
    """Keep Harness's per-run detector, thresholds, latch, and warning filters."""

    async def after_model_request(self, ctx, *, request_context, response):
        # The pinned upstream hook does not suspend: only its synchronous warning
        # emission is captured, never model/tool execution or another task's work.
        # Emit outside this scope; ctx.emit can suspend. No global warning handler.
        with warnings.catch_warnings(record=True) as caught:
            result = await super().after_model_request(
                ctx, request_context=request_context, response=response
            )
        for warning in caught:
            if issubclass(warning.category, CacheBustWarning):
                # Omit the Python suppression tutorial, keeping the full diagnosis
                # (including the TTL hint). The event contains no prompt contents.
                detail = str(warning.message).split("\n\n", 1)[0]
                model = "/".join(
                    part for part in (response.provider_name, response.model_name) if part
                )
                text = f"{model}: {detail}" if model else detail
                await ctx.emit(CacheBustEvent(text=command_text(text)))
            else:
                warnings.warn_explicit(
                    warning.message, warning.category, warning.filename, warning.lineno
                )
        return result
