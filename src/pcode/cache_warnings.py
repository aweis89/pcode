"""Route Harness cache-collapse warnings through the normal event stream."""

import warnings
from dataclasses import dataclass, field

from pydantic_ai import CapabilityEvent
from pydantic_ai.messages import NativeToolCallPart
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning, WarnOnCacheBusts

from pcode.cache_diagnostics import CacheDiagnostics
from pcode.tool_display import command_text


@dataclass(kw_only=True)
class CacheBustEvent(CapabilityEvent, namespace="pcode_cache", name="bust"):
    text: str


def server_tool_iterations(response) -> int:
    """Count server-side tool calls (web search, code execution) in a response.

    Each one is an extra sampling pass inside a single API call, and the provider
    reports one ``usage`` summed over every pass: ``cache_read_tokens`` is then
    roughly ``passes * prefix``, not a prefix anyone can read back.
    """
    return sum(isinstance(part, NativeToolCallPart) for part in response.parts)


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
        key = (response.provider_name, response.model_name)
        prior = self._state.keys.get(key)
        with warnings.catch_warnings(record=True) as caught:
            result = await super().after_model_request(
                ctx, request_context=request_context, response=response
            )
        if server_tool_iterations(response):
            # Upstream raises its high-water mark to `read + write` of every
            # response, but a summed multi-pass usage puts the mark several
            # times above the real prefix, and the next healthy request then
            # "collapses" against it (seen: 148k established from a 29k prefix
            # after three web searches; the next request read exactly 47k).
            # Keep the previous mark; the next single-pass response sets a real
            # one from its own usage.
            self._state.keys[key].prefix = prior.prefix if prior else 0
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
                # Report measured reuse, not upstream's speculative expiry cause
                # or an inferred number of tokens billed uncached.
                detail = (
                    f"request {self._state.step}: cached {response.usage.cache_read_tokens:,}"
                    f" vs ~{prior.prefix if prior else 0:,} established tokens."
                )
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
