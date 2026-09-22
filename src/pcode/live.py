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
    ToolReturn,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    RetryPromptPart,
)
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem, Planning
from pydantic_ai_harness.shell import (
    CommandFinishedEvent,
    CommandOutputEvent,
    CommandStartedEvent,
    Shell,
)
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, StepPersistence
from pydantic_ai_harness.subagents import DelegationEndEvent, DelegationStartEvent, SubAgents
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from pcode.agent import worker_toolsets
from pcode.cache_warnings import CacheBustEvent
from pcode.compaction import AutoCompaction, ContextTracking, summarize
from pcode.conversation_tree import ConversationTree
from pcode.delegation import ChildActivity
from pcode.diagnostics import (
    error_details,
    provider_context,
    quota_message,
    transient,
    transport_types,
)
from pcode.edit_preview import StreamingEditPreview
from pcode.filesystem import FileChangeEvent
from pcode.inspection import ToolArchive, capture
from pcode.job_notices import JobNotices
from pcode.jobs import registry as job_registry
from pcode.mcp import MCPState
from pcode.native_results import drop_unreadable_results, unreadable_native_results
from pcode.plan_preview import StreamingPlanPreview
from pcode.preferences import SETTINGS, load_preferences
from pcode.profiling import activity as profiled_activity
from pcode.retries import RequestCheckpoint
from pcode.runtime import (
    CacheBust,
    CommandOutput,
    EditPreview,
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
from pcode.shell import ShellPreview, result_projection
from pcode.shell_mode import ShellRun, reduce_result, shell_exchange
from pcode.steering import Steering
from pcode.token_accounting import TokenAccounting, TokenTotals
from pcode.tool_display import (
    COMMAND_TOOLS,
    command_error,
    command_text,
    delegation_detail,
    execution_mode,
    job_status,
    label,
    native_result_detail,
    native_result_projection,
    result_detail,
    stated_purpose,
    target,
)
from pcode.turn import TurnContext


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
        # Shell jobs outlive both the run and the conversation, so the registry
        # is not reset by `_clear`, `/new`, or conversation checkout.
        self.jobs = job_registry()
        self.take_steering = lambda: []
        # Prompt overhead describes the agent's configuration, not one
        # conversation, so it outlives /new and conversation checkout.
        self.request_parameters = None
        # Built on first use by `aside`; see `create_aside_agent`.
        self._aside_agent: Agent | None = None
        self._clear()
        self.replace_agent(agent)

    def replace_agent(self, agent: Agent) -> None:
        """Change the agent without resetting conversation-scoped state."""
        self.agent = agent
        # Side questions follow the conversation's model, so the twin is rebuilt
        # against the new agent rather than left on the previous provider.
        self._aside_agent = None
        # Coder's public root capability is flattened by Pydantic AI. A resolver
        # keeps the store conversation-scoped, including after /new. It resolves
        # per request, so this is also where a second turn would be handed its
        # own store: `ctx` names the run the tools are being called for.
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, Planning):
                capability.store_resolver = lambda ctx: self.plan_store

    def _persist_child_runs(self) -> None:
        """Record delegated runs in the current session's store.

        Sub-agents receive `shared_capabilities`, not the per-run capabilities the
        parent passes to `run_stream_events`, so child requests are otherwise
        absent from the store: their tokens reach session totals but nothing says
        how they were spent. The store changes with `/new`, so this is resolved
        per turn rather than at construction. `agent_name` is left unset: one
        shared capability serves every sub-agent, and `parent_run_id` already
        marks a run as delegated.
        """
        for capability in self.agent.root_capability.capabilities:
            if not isinstance(capability, SubAgents):
                continue
            shared = [
                shared_capability
                for shared_capability in capability.shared_capabilities
                if not isinstance(shared_capability, (StepPersistence, TokenAccounting))
            ]
            if self.session is not None:
                shared.append(StepPersistence(store=self.session.store))
            # Count a delegated request where it happens. Child tokens also
            # aggregate into the parent's run usage, which nothing reads.
            shared.append(TokenAccounting(record=self.totals.add))
            capability.shared_capabilities = shared

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
        # The active branch's turn state. Everything a turn reads and writes
        # back lives here rather than on the runtime; see `TurnContext`.
        self.context = TurnContext()
        self.conversation_id = info.id if info else str(uuid4())
        self.turns = info.turns if info else 0
        self.totals = TokenTotals(
            input=info.input_tokens if info else 0,
            output=info.output_tokens if info else 0,
            cache_read=info.cache_read_tokens if info else 0,
            cache_write=info.cache_write_tokens if info else 0,
        )
        self.recovery_blocked = ""
        self.mcp = MCPState()

    # The active branch's turn state, under the names callers already use.
    @property
    def history(self) -> list[ModelMessage]:
        return self.context.history

    @history.setter
    def history(self, messages: list[ModelMessage]) -> None:
        self.context.history = messages

    @property
    def pending_shell(self) -> list[ModelMessage]:
        return self.context.pending_shell

    @pending_shell.setter
    def pending_shell(self, messages: list[ModelMessage]) -> None:
        self.context.pending_shell = messages

    @property
    def plan_store(self) -> InMemoryPlanStore:
        return self.context.plan_store

    @plan_store.setter
    def plan_store(self, store: InMemoryPlanStore) -> None:
        self.context.plan_store = store

    @property
    def context_history(self) -> list[ModelMessage] | None:
        return self.context.context_history

    @context_history.setter
    def context_history(self, messages: list[ModelMessage] | None) -> None:
        self.context.context_history = messages

    @property
    def input_tokens(self) -> int:
        return self.totals.input

    @property
    def output_tokens(self) -> int:
        return self.totals.output

    def _save_totals(self, info) -> None:
        info.input_tokens = self.totals.input
        info.output_tokens = self.totals.output
        info.cache_read_tokens = self.totals.cache_read
        info.cache_write_tokens = self.totals.cache_write

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

    def aside_context(self) -> list[ModelMessage]:
        """The newest context a side question can be asked against.

        `context_history` is the request in flight, so a question asked mid-turn
        sees what the model is working on rather than the state before the turn
        began. Only the settled prefix is usable; see `settled_context`.

        The copy is not a precaution but the isolation itself: a side question is
        appended to the last request the way steering is, and these message
        objects belong to the running turn's own history.
        """
        from pcode.aside import settled_context

        history = self.context_history if self.context_history is not None else self.history
        return deepcopy(settled_context(list(history)))

    async def aside(self, question: str, *, report=None) -> str:
        """Answer `question` beside the conversation, recording nothing.

        Nothing here touches conversation state: no journal record, no tree
        node, no plan, and `self.history` is only read. The run is billed to the
        session's token totals, because the tokens were really spent. `report`
        receives `(answer_so_far, activity)` as the answer streams.
        """
        from pcode.agent import create_aside_agent
        from pcode.aside import ASIDE_REQUEST_LIMIT

        if self._aside_agent is None:
            workspace, _ = self.shell_environment()
            self._aside_agent = create_aside_agent(self.agent, workspace)
        agent = self._aside_agent
        messages = self.aside_context()
        # A turn in flight ends on a user-role request: its new prompt, or the
        # tool results it is working through. A second user message after one of
        # those is what providers reject as non-alternating roles, so the
        # question joins that request the way steering does, and the run
        # continues from history instead of adding a message of its own.
        joined = bool(messages) and isinstance(messages[-1], ModelRequest)
        pending = [question]
        capabilities = [TokenAccounting(record=self.totals.add)]
        if joined:
            capabilities.append(Steering(lambda: [pending.pop()] if pending else []))
        blocks: list[str] = []
        partial = ""
        activity = "Waiting for model…"
        tools: dict[str, str] = {}

        def publish() -> None:
            if report is not None:
                report("\n\n".join([*blocks, partial] if partial else blocks), activity)

        async with (
            agent,
            agent.run_stream_events(
                None if joined else question,
                message_history=messages,
                model_settings=self.agent.model_settings,
                capabilities=capabilities,
                usage_limits=UsageLimits(request_limit=ASIDE_REQUEST_LIMIT),
            ) as events,
        ):
            async for event in events:
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    partial += event.part.content
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    partial += event.delta.content_delta
                elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                    if event.part.content:
                        blocks.append(event.part.content)
                    partial = ""
                elif isinstance(event, FunctionToolCallEvent):
                    try:
                        args = event.part.args_as_dict()
                    except (ValueError, TypeError):
                        args = {}
                    where = target(event.part.tool_name, args)
                    tools[event.part.tool_call_id] = event.part.tool_name
                    activity = f"Reading {event.part.tool_name}" + (f" · {where}" if where else "")
                elif isinstance(event, FunctionToolResultEvent):
                    tools.pop(event.tool_call_id, None)
                    activity = "Waiting for model…" if not tools else activity
                else:
                    continue
                publish()
        if partial:
            blocks.append(partial)
            partial = ""
        activity = ""
        publish()
        return "\n\n".join(blocks)

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
            self.totals.add(usage)
            if self.session:
                self._save_totals(self.session.info)
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
        # Drops the logs of finished jobs only. A still-running job is the
        # whole point of the design and is left alone, still writing its log.
        self.jobs.shutdown()

    def shell_environment(self) -> tuple[Path, dict[str, str] | None]:
        """Where and with what environment `!command` runs: the agent's own shell settings."""
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, Shell):
                return Path(capability.cwd or Path.cwd()), capability.env
        return Path.cwd(), None

    async def record_shell(self, run: ShellRun) -> str | ToolReturn:
        """Queue a finished `!command` as a shell tool exchange for the next request.

        It is not written to history yet: nothing has been sent, and a turn
        that fails before its first request must not leave a tool call the
        session's snapshots never saw. `_stream` appends it to the request's
        message history, so the turn's own persistence carries it from then on.
        Returns what the model will see, reduced by the agent's output limits.
        """
        call_id = f"shell_mode_{uuid4().hex[:12]}"
        content: str | ToolReturn = run.tool_result()
        limits = next(
            (c for c in self.agent.root_capability.capabilities if isinstance(c, ToolOutputLimits)),
            None,
        )
        if limits is not None:
            content = await reduce_result(
                limits, call_id=call_id, command=run.command, result=content
            )
        first = not self.history and not self.pending_shell
        self.pending_shell.extend(
            shell_exchange(run.command, content, call_id=call_id, first=first)
        )
        return content

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
        """Retry only failed provider requests, with one budget per submitted turn.

        A history the current credential cannot replay is the exception: the
        same request would be rejected every time, so it is repaired once and
        resent outside that budget. Provider errors arrive here rather than at a
        capability because the model request is streamed, and its failure
        surfaces while the event stream is consumed.

        The whole generator is one profiled span, including the consumer's
        rendering of each event, so a capture separates a session's working cost
        from what it burns sitting at an idle prompt.
        """
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
        attempt = 0
        repaired = False
        while True:
            try:
                with profiled_activity("turn"):
                    async with aclosing(self._turn(send)) as turn:
                        async for event in turn:
                            yield event
            except Exception as error:
                if self.recovery_blocked or self.context.checkpoint.messages is None:
                    raise
                # Results the current login cannot decrypt fail identically on
                # every attempt, so repair the history once rather than spend
                # the retry budget on a request that cannot succeed.
                if not repaired and unreadable_native_results(error):
                    dropped = drop_unreadable_results(self.context.history)
                    if dropped:
                        repaired = True
                        send = None
                        self.retry_notice(
                            f"Dropped {dropped} unreadable web search "
                            f"{'result' if dropped == 1 else 'results'} from an earlier "
                            "sign-in, and retrying…"
                        )
                        continue
                if attempt == self.retry_attempts or not transient(error):
                    raise
                # _turn saved the exact failed request, including steering and
                # compaction. Never infer progress from the length of history.
                attempt += 1
                send = None
                self.retry_notice(
                    f"{error_message(error)} Retrying provider request "
                    f"{attempt}/{self.retry_attempts}…"
                )
                await asyncio.sleep(1)
            else:
                return

    async def _turn(self, send: str | None) -> AsyncIterator[Event]:
        """Run one attempt. A `None` prompt continues from history without adding to it."""
        prompt = send or ""
        # This turn's state, read and written through one object rather than
        # through the runtime. One turn runs at a time, so it is still the
        # active branch's context; a second turn would be given its own.
        context = self.context
        context.checkpoint = RequestCheckpoint()
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
        context.run_id = run_id
        context.compaction_usage = RunUsage()
        tools_started = False
        try:
            async with aclosing(self._stream(send, context)) as stream:
                async for event in stream:
                    if isinstance(event, ToolStarted):
                        tools_started = True
                    if isinstance(event, (PlanPreview, CommandOutput, EditPreview)):
                        # Unexecuted arguments must never enter replay/tree history.
                        yield event
                        continue
                    if saved:
                        saved.event(event, run_id=run_id)
                    if saved is None:
                        # Named with its turn, the way the journal records it.
                        record = {"kind": type(event).__name__, **asdict(event)}
                        record["run_id"] = record.get("run_id") or run_id
                        self.tree.consume(record)
                    if saved is None and isinstance(event, (ToolStarted, ToolSummary)):
                        self.inspections.event(event)
                    yield event
        except BaseException as error:
            resend_blocked = tools_started and context.checkpoint.messages is None
            if saved:
                self._save_totals(saved.info)
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
                    provider_context=provider_context(self.agent.model),
                    resend_blocked=resend_blocked,
                    sync=True,
                )
                if not cancelled:
                    # A bug reaches the user as a type and a message; the frames
                    # that name the responsible line live only in this process.
                    # Stopping on purpose is not a defect worth a traceback.
                    saved.record_error(
                        error,
                        run_id=run_id,
                        provider_context=provider_context(self.agent.model),
                    )
                saved.info.status = "cancelled" if cancelled else "failed"
                saved.save_info()
                try:
                    # Keep completed tool results even if the *following* request
                    # failed. Never silently re-run a side effect on retry.
                    checkpoint = context.checkpoint
                    if checkpoint.messages is not None:
                        # The failed request already carried any shell-mode
                        # exchange; recovering it below must not queue it twice.
                        context.pending_shell = []
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
                    context.history = await saved.recover()
                except SessionError as recovery_error:
                    self.recovery_blocked = str(recovery_error)
            else:
                cancelled = isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
                )
                if context.checkpoint.messages is not None:
                    context.pending_shell = []
                    context.history = context.checkpoint.messages
                self.tree.consume(
                    {
                        "kind": "turn_cancelled" if cancelled else "turn_failed",
                        "run_id": run_id,
                        "resend_blocked": resend_blocked,
                    }
                )
                self.tree.nodes[run_id].history = deepcopy(context.history)
            raise
        else:
            self.inspections.settle("unknown")
            if saved:
                saved.append("turn_completed", run_id=run_id, sync=True)
                saved.info.status = "complete"
                saved.info.turns = self.turns
                self._save_totals(saved.info)
                saved.save_info()
            else:
                self.tree.consume({"kind": "turn_completed", "run_id": run_id})
                self.tree.nodes[run_id].history = deepcopy(context.history)

        finally:
            # The auto-compaction summarizer is its own agent run: its usage
            # reaches neither `after_model_request` nor this run's result. It is
            # reset per attempt, so a retry loop cannot double-count it.
            self.totals.add(context.compaction_usage)
            context.context_history = None

    def _consume_steering(self, run_id: str) -> list[str]:
        messages = self.take_steering()
        if self.session:
            for text in messages:
                self.session.append("steering", run_id=run_id, prompt=text)
        return messages

    async def _stream(self, prompt: str | None, context: TurnContext) -> AsyncIterator[Event]:
        run_id = context.run_id
        await self.refresh_context()
        self._persist_child_runs()
        plan_items = [item.model_dump(mode="json") for item in await context.plan_store.get_items()]
        preview = (
            StreamingPlanPreview()
            if any(isinstance(c, Planning) for c in self.agent.root_capability.capabilities)
            else None
        )
        filesystem_root = next(
            (
                Path(c.cwd or c.root_dir)
                for c in self.agent.root_capability.capabilities
                if isinstance(c, FileSystem) and not c.read_only
            ),
            None,
        )
        edit_preview = (
            StreamingEditPreview(filesystem_root) if filesystem_root is not None else None
        )
        emitted_text = False
        tools: dict[str, tuple[str, dict, float]] = {}
        delegates: dict[str, ToolStarted] = {}
        child_tools: dict[str, ToolStarted] = {}
        delegation_ends: dict[str, DelegationEndEvent] = {}
        shell_preview = ShellPreview()
        shell_ends: dict[str, CommandFinishedEvent] = {}

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
            worker_toolsets(self.mcp.toolsets()),
            self.agent.run_stream_events(
                prompt,
                message_history=context.messages(),
                toolsets=self.mcp.toolsets(),
                conversation_id=self.conversation_id,
                run_id=run_id,
                # Per-run capabilities bind to this turn's context, not to the
                # runtime: what they publish and rewrite belongs to this turn.
                capabilities=(
                    ([StepPersistence(store=self.session.store)] if self.session else [])
                    + [
                        Steering(lambda: self._consume_steering(run_id)),
                        # Finished jobs reach the model here rather than by
                        # being polled for; see `pcode.job_notices`. Ahead of the
                        # checkpoint, so a saved request carries the notices it
                        # was really sent with, as steering and compaction do.
                        JobNotices(self.jobs),
                        context.checkpoint,
                        TokenAccounting(record=self.totals.add),
                        ContextTracking(self, context),
                    ]
                    + ([AutoCompaction(self, context)] if self.auto_compact else [])
                ),
                # Explicitly disable the cap; omitting this restores the library default.
                usage_limits=UsageLimits(request_limit=None),
            ) as events,
        ):
            async for event in events:
                if edit_preview is not None:
                    for edit_update in edit_preview.update(event):
                        yield edit_update
                if isinstance(event, CacheBustEvent):
                    yield CacheBust(event.text)
                elif isinstance(event, FileChangeEvent):
                    yield event.change
                elif isinstance(event, DelegationStartEvent):
                    if start := delegates.get(event.tool_call_id):
                        start = replace(start, activity="Waiting for model")
                        delegates[event.tool_call_id] = start
                        yield start
                elif isinstance(event, DelegationEndEvent):
                    delegation_ends[event.tool_call_id] = event
                    # Deliberately not added to session totals: a child's *tokens*
                    # already reach `result.usage` even under its own budget, so
                    # adding `event.usage` here counts them twice. Only its
                    # request count stays isolated, which is the point of the
                    # budget. Tokens from an interrupted turn are a separate gap.
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
                elif isinstance(
                    event, (CommandStartedEvent, CommandOutputEvent, CommandFinishedEvent)
                ):
                    if isinstance(event, CommandFinishedEvent):
                        shell_ends[event.tool_call_id] = event
                    if (output := shell_preview.update(event)) is not None:
                        yield output
                elif isinstance(event, PartStartEvent):
                    if isinstance(event.part, TextPart):
                        yield TextDelta(event.part.content)
                    elif isinstance(event.part, ThinkingPart):
                        if event.part.content:
                            yield ThinkingDelta(event.part.content)
                        yield RunStatus("Thinking…")
                    elif isinstance(event.part, NativeToolReturnPart):
                        # Provider-executed tools (native web search/fetch) return
                        # inside the response stream; there is no function event.
                        name, args, started = tools.pop(
                            event.part.tool_call_id, (event.part.tool_name, {}, monotonic())
                        )
                        detail, failed = native_result_detail(
                            name, args, event.part.content, event.part.outcome
                        )
                        yield ToolSummary(
                            name,
                            detail,
                            failed=failed,
                            call_id=event.part.tool_call_id,
                            result=capture(native_result_projection(event.part.content)),
                            run_id=run_id,
                            outcome=event.part.outcome if not failed else "error",
                            elapsed_seconds=max(0, monotonic() - started),
                        )
                        yield activity()
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
                    elif isinstance(event.part, NativeToolCallPart):
                        try:
                            args = event.part.args_as_dict()
                        except (ValueError, TypeError):
                            args = {}
                        tools[event.part.tool_call_id] = (event.part.tool_name, args, monotonic())
                        yield ToolStarted(
                            event.part.tool_name,
                            target(event.part.tool_name, args),
                            event.part.tool_call_id,
                            arguments=capture(args if args else event.part.args),
                            run_id=run_id,
                            started_at=datetime.now(timezone.utc).isoformat(),
                        )
                        yield activity()
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
                        if event.part.tool_name in {"shell", "run_command", "start_command"}
                        and isinstance(args.get("command"), str)
                        else "",
                        purpose=stated_purpose(args),
                        execution=execution_mode(event.part.tool_name, args),
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
                    shell_end = shell_ends.pop(event.tool_call_id, None)
                    if name == "shell" and shell_end is not None and outcome == "success":
                        # The result's own job marker is authoritative: it is
                        # written after the wait ends, while the event is a
                        # snapshot the command can finish just after.
                        status, failed = job_status(event.part.content)
                        detail = target(name, args) + (f" → {status}" if status else "")
                        if shell_end.truncated:
                            detail += " · preview capped"
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
                        item.model_dump(mode="json")
                        for item in await context.plan_store.get_items()
                    ]
                    if items != plan_items:
                        plan_items = items
                        yield PlanUpdated(items)
                    display_content = (
                        result_projection(event.part.content, shell_end)
                        if name == "shell"
                        else event.part.content
                    )
                    yield ToolSummary(
                        name,
                        detail,
                        failed=failed,
                        call_id=event.tool_call_id,
                        result=capture(display_content),
                        run_id=run_id,
                        outcome=outcome,
                        process_id=(
                            str(shell_end.pid)
                            if shell_end is not None
                            else match[1]
                            if name == "start_command"
                            and isinstance(event.part.content, str)
                            and (
                                match := re.search(r"^ID: (\w+)$", event.part.content, re.MULTILINE)
                            )
                            else capture(args.get("command_id", ""))
                        ),
                        elapsed_seconds=max(0, monotonic() - started),
                        command=command_text(args["command"])
                        if name in {"shell", "run_command", "start_command"}
                        and isinstance(args.get("command"), str)
                        else "",
                        purpose=stated_purpose(args),
                        error=command_error(display_content)
                        if failed and name in COMMAND_TOOLS
                        else "",
                    )
                    yield activity()
                elif isinstance(event, AgentRunResultEvent):
                    result = event.result
                    if result.output and not emitted_text:
                        yield Message(str(result.output))
                    # Full successful history. The outer persistence wrapper also
                    # recovers settled tool-boundary snapshots after failures.
                    context.history = result.all_messages()
                    context.pending_shell = []
                    self.turns += 1
                if preview is not None:
                    if (update := preview.update(event, plan_items)) is not None:
                        yield update


RETRY_CEILING = re.compile(r"^Tool '(?P<tool>[^']+)' exceeded max retries count of (?P<limit>\d+)")


def retry_ceiling(error: Exception) -> str | None:
    """Explain a turn that ended because a tool call could not be corrected in time.

    This is a local failure, not a provider one: the model kept sending
    arguments the tool's schema rejected (or the tool kept raising ModelRetry),
    and Pydantic AI stopped offering corrections. Saying "check your
    credentials and connectivity" for it sends the reader to the wrong place.

    Name the tool and the fields that failed, never their values: a rejected
    argument is model-authored content that can quote a file or a secret.
    """
    cause = error.__cause__
    if type(error).__name__ != "UnexpectedModelBehavior" or cause is None:
        return None
    match = RETRY_CEILING.match(str(error))
    if match is None:
        return None
    tool, limit = match["tool"], int(match["limit"])
    fields = ""
    if callable(getattr(cause, "errors", None)):
        try:
            names = {
                ".".join(str(part) for part in entry.get("loc", ()))
                for entry in cause.errors()
                if entry.get("loc")
            }
        except Exception:
            names = set()
        if names:
            fields = " Rejected argument: " + ", ".join(sorted(names)) + "."
    reason = (
        "arguments that failed validation"
        if type(cause).__name__ == "ValidationError"
        else "a call the tool rejected"
    )
    return (
        f"The model sent {reason} to `{tool}` more times than the retry limit "
        f"({limit}) allowed, so the turn stopped.{fields} "
        "Nothing is wrong with the model or the connection; rephrasing the request "
        "usually clears it. Raise the budget with `/config set tool_retries N`. "
        "See the saved session diagnostics."
    )


CODEX_LOGIN_HINT = "Run `/login openai-codex` (or `codex login`, then restart pcode)."


def error_message(error: Exception) -> str:
    """Don't print raw provider bodies/validation inputs; they can contain secrets."""
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return error_message(error.exceptions[0])
    from pcode.auth import LoginError
    from pcode.compaction import CompactionError
    from pcode.workspace import WorkspaceGoneError
    from pcode.worktree import WorktreeError

    if isinstance(error, (CompactionError, WorktreeError, WorkspaceGoneError)):
        # All three carry only local paths and git's own messages.
        return str(error)
    name = type(error).__name__
    if isinstance(error, (SessionError, LoginError)):
        # LoginError contains only fixed, sanitized setup/refresh guidance.
        return str(error)
    if name == "UserError" and "Codex CLI credentials" in str(error):
        return f"Provider login missing or invalid. {CODEX_LOGIN_HINT}"
    if name == "UserError" and "ANTHROPIC_API_KEY" in str(error):
        # pcode defers the model check so /login stays reachable without a
        # credential; the failure then surfaces here, on the first prompt.
        return "No Anthropic credential is selected. Run `/login`, or set ANTHROPIC_API_KEY."
    if name == "CredentialsRefreshError":
        # Recognize only fixed public error codes, never echo token-endpoint bodies.
        for code in (
            "refresh_token_invalidated",
            "refresh_token_reused",
            "refresh_token_expired",
            "invalid_grant",
        ):
            if code in str(error):
                return f"Provider login is no longer valid ({code}). {CODEX_LOGIN_HINT}"
        return f"Provider token refresh failed. {CODEX_LOGIN_HINT}"
    if "Credential" in name or "Authentication" in name:
        return f"Authentication failed ({name}). Refresh your provider login and restart."
    if (quota := quota_message(error)) is not None:
        return quota
    status = getattr(error, "status_code", None)
    if status is not None:
        details = error_details(error)
        detail = details.get("provider_message", "")
        suffix = f" {detail}" if detail else " See the saved session diagnostics."
        return f"Provider request failed (HTTP {status}).{suffix}"
    if isinstance(error, ImportError):
        return "Provider dependency missing. Install its pydantic-ai-slim extra and try again."
    if (exhausted := retry_ceiling(error)) is not None:
        return exhausted
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
