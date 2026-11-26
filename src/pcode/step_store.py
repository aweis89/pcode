"""The step store a session owner writes through.

Its own module because Harness's step persistence imports all of Pydantic AI
(~0.4 s): `pcode.sessions` is also how a terminal reads a session it only
displays, and that path never opens the store.
"""

from dataclasses import replace

from pydantic_ai_harness.step_persistence import SqliteStepStore, StepEvent, ToolEffectRecord

from pcode.diagnostics import redact


class PrivateStepStore(SqliteStepStore):
    """Use the public store hooks to redact error strings, not replay-critical history."""

    async def append_event(self, event: StepEvent) -> None:
        if event.error:
            event = replace(event, error=redact(event.error))
        await super().append_event(event)

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        if record.effect_summary:
            record = replace(record, effect_summary=redact(record.effect_summary))
        await super().record_tool_effect(record)
