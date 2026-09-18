"""Translate Pydantic streams to UI-independent application events."""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    RetryPromptPart,
)
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem, Planning
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, StepPersistence
from pydantic_ai_harness.subagents import DelegationEndEvent, DelegationStartEvent

from pcode.compaction import AutoCompaction, summarize
from pcode.conversation_tree import ConversationTree
from pcode.delegation import ChildActivity
from pcode.diagnostics import error_details, transient, transport_types
from pcode.inspection import ToolArchive, capture
from pcode.mcp import MCPState
from pcode.plan_preview import StreamingPlanPreview
from pcode.preferences import SETTINGS, load_preferences
from pcode.retries import RequestCheckpoint
from pcode.runtime import (
    CommandOutput,
    Event,
    Message,
    PlanPreview,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
)
from pcode.sessions import SavedSession, SessionError
from pcode.shell import CommandOutputEvent
from pcode.steering import Steering
from pcode.tool_display import (
    command_error,
    command_text,
    delegation_detail,
    label,
    result_detail,
    target,
)


class AgentRuntime:
    def __init__(
        self,
        agent: Agent,
        session: SavedSession | None = None,
        *,
        session_factory: Callable[[], SavedSession] | None = None,
    ) -> None:
        self.agent = agent
        self.session = session
        if session is not None:
            model, workspace, root = (
                session.info.model,
                Path(session.info.workspace),
                session.directory.parent,
            )

            def session_factory():
                return SavedSession.create(model, workspace, root)

        self.session_factory = session_factory
        preferences = load_preferences()
        self.auto_compact = preferences.get("autocompact") == "on"
        # Snapshotted like autocompact: a saved default applies to the next launch.
        self.retry_attempts = int(
            preferences.get("retry_attempts", SETTINGS["retry_attempts"].default)
        )
        self.compaction_notice = lambda text: None
        self.retry_notice = lambda text: None
        self.take_steering = lambda: []
        self._clear()
        self.replace_agent(agent)

    def replace_agent(self, agent: Agent) -> None:
        """Change the agent without resetting conversation-scoped state."""
        self.agent = agent
        # Coder's public root capability is flattened by Pydantic AI. A resolver
        # keeps the store conversation-scoped, including after /new.
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, Planning):
                capability.store_resolver = lambda ctx: self.plan_store

    async def refresh_context(self) -> None:
        """Refresh optional metadata outside rendering and before model requests."""
        from pydantic_ai.models import infer_model

        from pcode.model_metadata import refresh_context

        if isinstance(self.agent.model, str):
            try:
                # Retain the resolved provider so the UI and future requests
                # share its identity-scoped metadata, including deferred login.
                self.agent.model = infer_model(self.agent.model)
            except Exception:
                # Optional discovery must not prevent reaching /login.
                pass
        await refresh_context(self.agent.model)

    def startup_context(self) -> list[str]:
        """Report only repository context configured on this agent."""
        from pcode.repo_context import AutomaticRepoContext

        lines = []
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, AutomaticRepoContext):
                lines.extend(capability.startup_summary())
        return lines

    def _clear(self) -> None:
        info = self.session.info if self.session else None
        self.tree = self.session.tree if self.session else ConversationTree()
        self.inspections = ToolArchive()
        self.history: list[ModelMessage] = []
        self.context_history: list[ModelMessage] | None = None
        self.conversation_id = info.id if info else str(uuid4())
        self.turns = info.turns if info else 0
        self.input_tokens = info.input_tokens if info else 0
        self.output_tokens = info.output_tokens if info else 0
        self.recovery_blocked = ""
        self._request_checkpoint = RequestCheckpoint()
        self.plan_store = InMemoryPlanStore()
        self.mcp = MCPState()

    def reset(self) -> None:
        if self.session:
            self.session.close()
            self.session = None
        self._clear()

    async def restore(self) -> None:
        if self.session:
            self.history = await self.session.recover()
            await self.plan_store.set_items(
                [PlanItem.model_validate(item) for item in self.session.latest_plan()]
            )

    async def navigate(self, identity: str | None, *, edit: bool = False) -> str:
        """Restore a safe checkpoint without running a model or replaying tools."""
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        self.tree.path(identity)
        node = self.tree.nodes[identity] if identity else None
        target = node.parent if edit and node else identity
        draft = node.prompt if edit and node else ""
        history = (
            await self.session.history_at(target)
            if self.session
            else deepcopy(self.tree.nodes[target].history or [])
            if target
            else []
        )
        plan = InMemoryPlanStore()
        await plan.set_items(
            [
                PlanItem.model_validate(item)
                for item in (self.tree.nodes[target].plan if target else [])
            ]
        )
        # Publish the cursor only after all restoration/validation succeeds.
        if self.session:
            self.session.append("tree_selected", node_id=target, sync=True)
        else:
            self.tree.active = target
        self.history = history
        self.plan_store = plan
        return draft

    async def compact(self, focus: str = ""):
        """Persist a new branch-local context checkpoint before publishing it."""
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        usage = RunUsage()
        try:
            async with self.agent:
                result = await summarize(
                    self.history, model=self.agent.model, focus=focus or None, usage=usage
                )
        finally:
            # A cancelled/failed summary can still have incurred provider usage.
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            if self.session:
                self.session.info.input_tokens = self.input_tokens
                self.session.info.output_tokens = self.output_tokens
                self.session.save_info()
        if not result.changed:
            return result
        record = {
            "node_id": str(uuid4()),
            "parent_id": self.tree.active,
            "focus": focus,
            "messages": ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
            "plan": [item.model_dump(mode="json") for item in await self.plan_store.get_items()],
            "before": result.before,
            "after": result.after,
        }
        if self.session:
            self.session.append("compaction_checkpoint", sync=True, **record)
        else:
            self.tree.consume({"kind": "compaction_checkpoint", **record})
        self.history = result.messages
        return result

    def close(self) -> None:
        if self.session:
            self.session.close()

    def resend_prompt(self) -> str:
        """The original prompt is a display label, not another model message."""
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        active = self.tree.nodes.get(self.tree.active)
        if active and active.resend_blocked:
            raise SessionError(
                "The interrupted turn may have changed files or run commands. "
                "Inspect its tools and send an explicit next step instead of /resend."
            )
        for identity in reversed(self.tree.path(self.tree.active)):
            node = self.tree.nodes[identity]
            if node.kind == "turn" and node.prompt:
                return node.prompt
        raise SessionError("There is no earlier prompt to resend; send a message instead.")

    async def stream(self, prompt: str | None) -> AsyncIterator[Event]:
        """Retry only failed provider requests, with one budget per submitted turn."""
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        send = prompt
        if send is None:
            original = self.resend_prompt()
            node = self.tree.nodes[self.tree.active]
            if not self.history:
                send = original
            # A completed text response would short-circuit Agent.run(None).
            # Regenerate just that response, retaining all settled tool results.
            elif isinstance(self.history[-1], ModelResponse):
                if self.history[-1].tool_calls:
                    raise SessionError("The checkpoint has unsettled tools; cannot resend safely.")
                if node.status != "completed" and not node.continuation:
                    send = original
                else:
                    self.history = self.history[:-1]
        for attempt in range(self.retry_attempts + 1):
            try:
                async with aclosing(self._turn(send)) as turn:
                    async for event in turn:
                        yield event
            except Exception as error:
                if (
                    attempt == self.retry_attempts
                    or self.recovery_blocked
                    or self._request_checkpoint.messages is None
                    or not transient(error)
                ):
                    raise
                # _turn saved the exact failed request, including steering and
                # compaction. Never infer progress from the length of history.
                send = None
                self.retry_notice(
                    f"Provider connection dropped; retry {attempt + 1}/{self.retry_attempts}…"
                )
                await asyncio.sleep(1)
            else:
                return

    async def _turn(self, send: str | None) -> AsyncIterator[Event]:
        """Run one attempt. A `None` prompt continues from history without adding to it."""
        prompt = send or ""
        self._request_checkpoint = RequestCheckpoint()
        if self.session is None and self.session_factory is not None:
            self.session = self.session_factory()
            self.conversation_id = self.session.info.id
            self.tree = self.session.tree
        saved = self.session
        run_id = str(uuid4())
        if saved:
            saved.append(
                "turn_started",
                prompt=prompt,
                run_id=run_id,
                parent_id=self.tree.active,
                continuation=send is None,
                sync=True,
            )
            saved.info.status = "running"
            saved.save_info()
        else:
            self.tree.consume(
                {
                    "kind": "turn_started",
                    "prompt": prompt,
                    "continuation": send is None,
                    "run_id": run_id,
                    "parent_id": self.tree.active,
                }
            )
        self.inspections.run_id = run_id
        self._compaction_usage = RunUsage()
        tools_started = False
        try:
            async with aclosing(self._stream(send, run_id)) as stream:
                async for event in stream:
                    if isinstance(event, ToolStarted):
                        tools_started = True
                    if isinstance(event, (PlanPreview, CommandOutput)):
                        # Unexecuted arguments must never enter replay/tree history.
                        yield event
                        continue
                    if saved:
                        saved.event(event)
                    if saved is None:
                        self.tree.consume({"kind": type(event).__name__, **asdict(event)})
                    if saved is None and isinstance(event, (ToolStarted, ToolSummary)):
                        self.inspections.event(event)
                    yield event
        except BaseException as error:
            resend_blocked = tools_started and self._request_checkpoint.messages is None
            # Successful runs account for nested summary usage through result.usage.
            # A failed next request must not hide the summary's already incurred cost.
            self.input_tokens += self._compaction_usage.input_tokens
            self.output_tokens += self._compaction_usage.output_tokens
            if saved:
                saved.info.input_tokens = self.input_tokens
                saved.info.output_tokens = self.output_tokens
            self.inspections.settle(
                "interrupted"
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit))
                else "unknown"
            )
            if saved:
                cancelled = isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
                )
                saved.append(
                    "turn_cancelled" if cancelled else "turn_failed",
                    run_id=run_id,
                    error=error_details(error),
                    resend_blocked=resend_blocked,
                    sync=True,
                )
                saved.info.status = "cancelled" if cancelled else "failed"
                saved.save_info()
                try:
                    # Keep completed tool results even if the *following* request
                    # failed. Never silently re-run a side effect on retry.
                    checkpoint = self._request_checkpoint
                    if checkpoint.messages is not None:
                        # Override any partial response saved during unwind. This
                        # also persists a bare first prompt, which Harness omits.
                        await saved.store.save_snapshot(
                            ContinuableSnapshot(
                                run_id=run_id,
                                step_index=checkpoint.step,
                                messages=checkpoint.messages,
                                conversation_id=self.conversation_id,
                            )
                        )
                    self.history = await saved.recover()
                except SessionError as recovery_error:
                    self.recovery_blocked = str(recovery_error)
            else:
                cancelled = isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
                )
                if self._request_checkpoint.messages is not None:
                    self.history = self._request_checkpoint.messages
                self.tree.consume(
                    {
                        "kind": "turn_cancelled" if cancelled else "turn_failed",
                        "resend_blocked": resend_blocked,
                    }
                )
                self.tree.nodes[run_id].history = deepcopy(self.history)
            raise
        else:
            self.inspections.settle("unknown")
            if saved:
                saved.append("turn_completed", run_id=run_id, sync=True)
                saved.info.status = "complete"
                saved.info.turns = self.turns
                saved.info.input_tokens = self.input_tokens
                saved.info.output_tokens = self.output_tokens
                saved.save_info()
            else:
                self.tree.consume({"kind": "turn_completed"})
                self.tree.nodes[run_id].history = deepcopy(self.history)

        finally:
            self.context_history = None

    async def _stream(self, prompt: str | None, run_id: str) -> AsyncIterator[Event]:
        await self.refresh_context()
        plan_items = [item.model_dump(mode="json") for item in await self.plan_store.get_items()]
        preview = (
            StreamingPlanPreview()
            if any(isinstance(c, Planning) for c in self.agent.root_capability.capabilities)
            else None
        )
        emitted_text = False
        tools: dict[str, tuple[str, dict, float]] = {}
        delegates: dict[str, ToolStarted] = {}
        child_tools: dict[str, ToolStarted] = {}
        delegation_ends: dict[str, DelegationEndEvent] = {}

        def activity() -> RunStatus:
            if not tools:
                return RunStatus("Waiting for model…")
            if len(tools) == 1:
                name, args, _ = next(iter(tools.values()))
                where = target(name, args)
                title = label(name) if name == "delegate_task" else name
                return RunStatus(f"Running {title}" + (f" · {where}" if where else "") + "…")
            names = ", ".join(label(item[0]) for item in list(tools.values())[:3])
            return RunStatus(f"Running {len(tools)} tools · {names}…")

        # Unlike run_stream(), this completes the tool loop even when the model
        # sends explanatory text alongside its tool calls.
        # Enter the agent too: a run alone does not own a statically supplied
        # model's HTTP client. Exit closes it on success, failure, or cancellation.
        async with (
            self.agent,
            self.agent.run_stream_events(
                prompt,
                message_history=self.history,
                toolsets=self.mcp.toolsets(),
                conversation_id=self.conversation_id,
                run_id=run_id,
                capabilities=(
                    ([StepPersistence(store=self.session.store)] if self.session else [])
                    + [Steering(self.take_steering), self._request_checkpoint]
                    + ([AutoCompaction(self, run_id)] if self.auto_compact else [])
                ),
                # Explicitly disable the cap; omitting this restores the library default.
                usage_limits=UsageLimits(request_limit=None),
            ) as events,
        ):
            async for event in events:
                if isinstance(event, DelegationStartEvent):
                    if start := delegates.get(event.tool_call_id):
                        start = replace(start, activity="Waiting for model")
                        delegates[event.tool_call_id] = start
                        yield start
                elif isinstance(event, DelegationEndEvent):
                    delegation_ends[event.tool_call_id] = event
                    # Timeouts/budget stops can leave a child's tool without a
                    # result. Settle it before the parent resumes its tool loop.
                    for call_id, child in list(child_tools.items()):
                        if child.parent_call_id == event.tool_call_id:
                            yield ToolSummary(
                                child.name,
                                child.detail + " → Interrupted",
                                failed=True,
                                call_id=call_id,
                                run_id=run_id,
                                outcome="interrupted",
                                parent_call_id=child.parent_call_id,
                            )
                            del child_tools[call_id]
                elif isinstance(event, ChildActivity):
                    if start := delegates.get(event.tool_call_id):
                        if event.activity != start.activity:
                            start = replace(start, activity=event.activity)
                            delegates[event.tool_call_id] = start
                            yield start
                        if event.child is not None:
                            if isinstance(event.child, ToolStarted):
                                child_tools[event.child.call_id] = event.child
                            else:
                                child_tools.pop(event.child.call_id, None)
                            yield event.child
                elif isinstance(event, CommandOutputEvent):
                    yield CommandOutput(event.call_id, event.command, event.output)
                elif isinstance(event, PartStartEvent):
                    if isinstance(event.part, TextPart):
                        yield TextDelta(event.part.content)
                    elif isinstance(event.part, ThinkingPart):
                        if event.part.content:
                            yield ThinkingDelta(event.part.content)
                        yield RunStatus("Thinking…")
                elif isinstance(event, PartDeltaEvent):
                    if isinstance(event.delta, TextPartDelta):
                        yield TextDelta(event.delta.content_delta)
                    elif isinstance(event.delta, ThinkingPartDelta):
                        if event.delta.content_delta:
                            yield ThinkingDelta(event.delta.content_delta)
                        yield RunStatus("Thinking…")
                elif isinstance(event, PartEndEvent):
                    if isinstance(event.part, TextPart) and event.part.content:
                        emitted_text = True
                        yield Message(event.part.content)
                    elif isinstance(event.part, ThinkingPart) and event.part.content:
                        yield Thinking(event.part.content)
                elif isinstance(event, FunctionToolCallEvent):
                    try:
                        args = event.part.args_as_dict()
                    except (ValueError, TypeError):
                        args = {}
                    tools[event.part.tool_call_id] = (event.part.tool_name, args, monotonic())
                    start = ToolStarted(
                        event.part.tool_name,
                        target(event.part.tool_name, args),
                        event.part.tool_call_id,
                        arguments=capture(args if args else event.part.args),
                        run_id=run_id,
                        started_at=datetime.now(timezone.utc).isoformat(),
                        process_id=capture(args.get("command_id", "")),
                        command=command_text(args["command"])
                        if event.part.tool_name in {"run_command", "start_command"}
                        and isinstance(args.get("command"), str)
                        else "",
                    )
                    if event.part.tool_name == "delegate_task":
                        delegates[event.part.tool_call_id] = start
                    yield start
                    yield activity()
                elif isinstance(event, FunctionToolResultEvent):
                    name, args, started = tools.pop(
                        event.tool_call_id,
                        (event.part.tool_name or "tool", {}, monotonic()),
                    )
                    outcome = (
                        "retry" if isinstance(event.part, RetryPromptPart) else event.part.outcome
                    )
                    detail, failed = result_detail(name, args, event.part.content, outcome)
                    delegates.pop(event.tool_call_id, None)
                    end = delegation_ends.pop(event.tool_call_id, None)
                    if end is not None:
                        outcome = end.outcome
                        detail, failed = delegation_detail(args, outcome)
                    elif name == "delegate_task" and outcome == "success":
                        # Harness returns max_calls refusals as normal strings,
                        # with no lifecycle events because no child was launched.
                        outcome = "not_started"
                        detail = target(name, args) + " → Not started"
                        failed = True
                    # Read the store after every settled tool: covers granular,
                    # batched, and future plan mutations without parsing results.
                    items = [
                        item.model_dump(mode="json") for item in await self.plan_store.get_items()
                    ]
                    if items != plan_items:
                        plan_items = items
                        yield PlanUpdated(items)
                    yield ToolSummary(
                        name,
                        detail,
                        failed=failed,
                        call_id=event.tool_call_id,
                        result=capture(event.part.content),
                        run_id=run_id,
                        outcome=outcome,
                        process_id=(
                            match[1]
                            if name == "start_command"
                            and isinstance(event.part.content, str)
                            and (
                                match := re.search(r"^ID: (\w+)$", event.part.content, re.MULTILINE)
                            )
                            else capture(args.get("command_id", ""))
                        ),
                        elapsed_seconds=max(0, monotonic() - started),
                        command=command_text(args["command"])
                        if name in {"run_command", "start_command"}
                        and isinstance(args.get("command"), str)
                        else "",
                        error=command_error(event.part.content)
                        if failed
                        and name
                        in {"run_command", "start_command", "check_command", "stop_command"}
                        else "",
                    )
                    yield activity()
                elif isinstance(event, AgentRunResultEvent):
                    result = event.result
                    if result.output and not emitted_text:
                        yield Message(str(result.output))
                    # Full successful history. The outer persistence wrapper also
                    # recovers settled tool-boundary snapshots after failures.
                    self.history = result.all_messages()
                    self.turns += 1
                    self.input_tokens += result.usage.input_tokens
                    self.output_tokens += result.usage.output_tokens
                if preview is not None:
                    if (update := preview.update(event, plan_items)) is not None:
                        yield update


def error_message(error: Exception) -> str:
    """Don't print raw provider bodies/validation inputs; they can contain secrets."""
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return error_message(error.exceptions[0])
    from pcode.auth import LoginError
    from pcode.compaction import CompactionError

    if isinstance(error, CompactionError):
        return str(error)
    name = type(error).__name__
    if isinstance(error, (SessionError, LoginError)):
        # LoginError contains only fixed, sanitized setup/refresh guidance.
        return str(error)
    if name == "UserError" and "Codex CLI credentials" in str(error):
        return "Provider login missing or invalid. Run `codex login`, then restart pcode."
    if name == "UserError" and "ANTHROPIC_API_KEY" in str(error):
        # pcode defers the model check so /login stays reachable without a
        # credential; the failure then surfaces here, on the first prompt.
        return (
            "No Anthropic credential is selected. Run `/login` (anthropic or pi), "
            "or start pcode with PCODE_ANTHROPIC_AUTH=pi, "
            "or set ANTHROPIC_API_KEY."
        )
    if name == "CredentialsRefreshError":
        # Recognize only fixed public error codes, never echo token-endpoint bodies.
        for code in (
            "refresh_token_invalidated",
            "refresh_token_reused",
            "refresh_token_expired",
            "invalid_grant",
        ):
            if code in str(error):
                return (
                    f"Provider login is no longer valid ({code}). "
                    "Run `codex login`, then restart pcode."
                )
        return "Provider token refresh failed. Run `codex login`, then restart pcode."
    if "Credential" in name or "Authentication" in name:
        return f"Authentication failed ({name}). Refresh your provider login and restart."
    status = getattr(error, "status_code", None)
    if status is not None:
        details = error_details(error)
        detail = details.get("provider_message", "")
        suffix = f" {detail}" if detail else " See the saved session diagnostics."
        return f"Provider request failed (HTTP {status}).{suffix}"
    if isinstance(error, ImportError):
        return "Provider dependency missing. Install its pydantic-ai-slim extra and try again."
    # SDKs wrap transport failures in ModelAPIError. Classify the bounded cause
    # chain, but never echo transport text: it may contain URLs or credentials.
    names = transport_types(error)
    if "RemoteProtocolError" in names:
        return (
            "Provider connection closed or returned an incomplete/invalid response. "
            "Check provider/proxy connectivity and retry when ready. "
            "See the saved session diagnostics."
        )
    if names & {"APITimeoutError", "ConnectTimeout", "ReadTimeout", "WriteTimeout"}:
        return (
            "Provider request timed out. Check provider/proxy connectivity and retry when ready. "
            "See the saved session diagnostics."
        )
    if names & {"APIConnectionError", "ConnectError", "ReadError", "WriteError"}:
        return (
            "Could not communicate with the provider. "
            "Check network/proxy settings and provider availability, then retry when ready. "
            "See the saved session diagnostics."
        )
    return f"Run failed ({name}). Check the model string, provider credentials, and connectivity."
