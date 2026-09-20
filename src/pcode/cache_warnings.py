"""Route Harness cache-collapse warnings through the normal event stream."""

import warnings
from dataclasses import dataclass, field

from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning, WarnOnCacheBusts

from pcode.cache_diagnostics import CacheDiagnostics
from pcode.tool_display import command_text


@dataclass(kw_only=True)
class CacheBustEvent(CapabilityEvent, namespace="pcode_cache", name="bust"):
    text: str


@dataclass
class CacheBustReporting(WarnOnCacheBusts):
    """Keep Harness's per-run detector, thresholds, latch, and warning filters."""

    # `for_run` copies the capability with `replace()`, which re-initializes
    # `init=False` fields, so each run fingerprints its own requests -- matching
    # the upstream detector's per-run step numbering.
    diagnostics: CacheDiagnostics = field(
        init=False, default_factory=CacheDiagnostics, compare=False, repr=False
    )

    async def after_model_request(self, ctx, *, request_context, response):
        # Harness hooks are filters, not listeners: the return value replaces the
        # response (or request context, for `before_model_request`). A hook that
        # only records something must still return it, or the loss surfaces far
        # away as `AttributeError: 'NoneType' object has no attribute 'usage'`
        # from the upstream `WarnOnCacheBusts` this class extends.
        #
        # The pinned upstream hook does not suspend: only its synchronous warning
        # emission is captured, never model/tool execution or another task's work.
        # Emit outside this scope; ctx.emit can suspend. No global warning handler.
        with warnings.catch_warnings(record=True) as caught:
            result = await super().after_model_request(
                ctx, request_context=request_context, response=response
            )
        # Fingerprint every request, not just collapsing ones: diagnosing a
        # collapse needs the healthy request before it to compare against.
        # Part shapes vary by provider and capability, so a diagnostic that
        # cannot read one must degrade to silence rather than end the run.
        try:
            self.diagnostics.record(request_context, response)
        except Exception:
            self.diagnostics.records.clear()
        for warning in caught:
            if issubclass(warning.category, CacheBustWarning):
                # Omit the Python suppression tutorial, keeping the full diagnosis
                # (including the TTL hint). The event contains no prompt contents.
                detail = str(warning.message).split("\n\n", 1)[0]
                model = "/".join(
                    part for part in (response.provider_name, response.model_name) if part
                )
                text = f"{model}: {detail}" if model else detail
                text = "\n".join(part for part in (text, self.diagnostics.summary()) if part)
                path = self.diagnostics.dump()
                if path is not None:
                    text += f"\nRequest fingerprints: {path}"
                await ctx.emit(CacheBustEvent(text=command_text(text)))
            else:
                warnings.warn_explicit(
                    warning.message, warning.category, warning.filename, warning.lineno
                )
        return result
