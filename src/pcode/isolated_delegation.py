"""Worktree-aware delegation over the pinned Harness run/event/budget machinery.

Only the built-in worker is reconstructible in another workspace. Specialized
extension agents keep their own capabilities and shared-workspace semantics.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai_harness.subagents import SubAgents
from pydantic_ai_harness.subagents._toolset import SubAgentToolset

from pcode.preferences import SETTINGS, load_preferences
from pcode.task_worktrees import TaskWorktrees
from pcode.worktree import WorktreeError, describe, setup_scripts

_outcome: ContextVar[str | None] = ContextVar("isolated_delegation_outcome", default=None)


def isolation_enabled() -> bool:
    """Require explicit worker opt-in as well as the active worktree preference."""
    preferences = load_preferences()
    return all(
        preferences.get(key, SETTINGS[key].default) == "on"
        for key in ("worktree", "worker_isolation")
    )


async def _joined(task):
    """Do not abandon a mutation/cleanup thread when the caller is cancelled."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        result = task.result()
    except Exception:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _finish(store, task_id, status, summary):
    return await _joined(
        asyncio.create_task(asyncio.to_thread(store.finish, task_id, status, summary, wait=True))
    )


async def _stop_setup(process):
    # Setup owns a whole process group, including grandchildren holding pipes.
    # Always kill the group after the grace period, even if its leader exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _setup(tree):
    """Use the trusted setup policy, but own cancellable subprocesses per task."""
    env = {
        **os.environ,
        "PCODE_MAIN": str(tree.main),
        "PCODE_WORKTREE": str(tree.path),
        "PCODE_BRANCH": tree.branch,
    }
    for script in setup_scripts(tree):
        command = [str(script)] if os.access(script, os.X_OK) else ["sh", str(script)]
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=tree.path,
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        process = None
        try:
            # If cancelled during spawn, join it to obtain and reap its process.
            process = await asyncio.shield(launch)
            code = await process.wait()
            if code:
                raise WorktreeError(f"{script} exited {code}")
        except asyncio.CancelledError:
            if process is None:
                try:
                    await _joined(launch)
                except asyncio.CancelledError:
                    pass
                process = launch.result()
            await _joined(asyncio.create_task(_stop_setup(process)))
            raise


def _worker_slots() -> asyncio.Semaphore | None:
    limit = int(
        load_preferences().get("worker_concurrency", SETTINGS["worker_concurrency"].default)
    )
    return asyncio.Semaphore(limit) if limit else None


@dataclass(kw_only=True)
class WorkspaceSubAgents(SubAgents):
    workspace: Path
    worker_factory: Any
    worker_slots: asyncio.Semaphore | None = field(
        default_factory=_worker_slots, repr=False, compare=False
    )

    def get_toolset(self):
        return WorkspaceSubAgentToolset(
            workspace=self.workspace,
            worker_factory=self.worker_factory,
            worker_slots=self.worker_slots,
            agents=self._by_name,
            forward_usage=self.forward_usage,
            inherit_tools=self.inherit_tools,
            shared_capabilities=self.shared_capabilities,
            event_stream_handler=self.event_stream_handler,
            tool_name=self.tool_name,
            tool_retries=self.tool_retries,
            contain_errors=self.contain_errors,
            call_counts=self._call_counts,
            models=self._menu,
        )

    def get_instructions(self):
        return (super().get_instructions() or "") + (
            "\nWorker workspace_mode defaults to auto: shared unless both worktree=on and "
            "worker_isolation=on in the active workspace's config. worker_isolation defaults "
            "to off; worktree alone never isolates workers. Isolation requires a clean tracked "
            "parent checkout; commit a checkpoint first, never stash or silently switch modes. "
            "Use shared explicitly for work that needs the live parent files; coordinate writes. "
            "Specialized delegates retain their own tools and use shared mode. "
            "Isolated results are branches, not changes to your checkout. Review them, then "
            "integrate_task into this parent branch and verify the combined changes. "
            "Use list_task_worktrees to recover task IDs after restart. "
            "Never push worker branches or merge them directly into mainline. "
            "Ask the user before discard_task(confirm=True) destroys unintegrated work."
        )


class WorkspaceSubAgentToolset(SubAgentToolset):
    def __init__(self, *, workspace: Path, worker_factory, worker_slots, **kwargs):
        self.workspace = workspace
        self.worker_factory = worker_factory
        self.worker_slots = worker_slots
        super().__init__(**kwargs)
        self.add_function(self.integrate_task)
        self.add_function(self.discard_task)
        self.add_function(self.list_task_worktrees)

    async def delegate_task(
        self,
        ctx: RunContext,
        agent_name: str,
        task: str,
        workspace_mode: Literal["auto", "isolated", "shared"] = "auto",
        model: str | None = None,
        purpose: str = "",
    ) -> Any:
        """Delegate a self-contained task; the child does not see this conversation.

        auto shares the parent's live files unless both worktree=on and
        worker_isolation=on in the active config. isolated requires both settings
        and clean tracked files, creates a branch at the parent's HEAD, and returns
        a persistent task record. shared is not read-only. Specialized agents only
        support shared mode. Review isolated results before integrate_task.

        Args:
            purpose: What the sub-agent is doing, at most 8 words, present tense
                (e.g. "fixing the /btw cache prefix"). It labels the delegation
                in the user's task list; the sub-agent never sees it.
        """
        # `purpose` is display-only: the event layer reads it from the call's arguments.
        if agent_name == "worker" and self.worker_slots is not None:
            async with self.worker_slots:
                return await self._delegate(ctx, agent_name, task, workspace_mode, model)
        return await self._delegate(ctx, agent_name, task, workspace_mode, model)

    async def _delegate(self, ctx, agent_name, task, workspace_mode, model):
        isolated = workspace_mode == "isolated" or (
            workspace_mode == "auto" and agent_name == "worker" and isolation_enabled()
        )
        if not isolated:
            return await super().delegate_task(ctx, agent_name, task, model)
        if not isolation_enabled():
            raise ModelRetry(
                "Isolated delegation requires both worktree=on and worker_isolation=on "
                "in the active config. Use shared mode without worker isolation."
            )
        if agent_name != "worker":
            raise ModelRetry("Only the built-in worker supports isolated workspaces; use shared.")
        key = self._resolve_model_key(agent_name, self._agents[agent_name], model)
        try:
            store = TaskWorktrees(self.workspace)
            creation = asyncio.create_task(asyncio.to_thread(store.create, wait=True))
            try:
                record = await _joined(creation)
            except asyncio.CancelledError:
                if not creation.cancelled() and creation.exception() is None:
                    await _finish(
                        store,
                        creation.result().task_id,
                        "cancelled",
                        "Delegation cancelled during creation; checkout preserved.",
                    )
                raise
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error
        token = _outcome.set(None)
        try:
            # Keep hung provisioning bounded too; timeout/failure preserves the artifact.
            async with asyncio.timeout(self._agents[agent_name].timeout_seconds):
                await _setup(describe(Path(record.worktree)))
            async with self.worker_factory(Path(record.worktree)) as worker:
                prompt = (
                    f"{task}\n\nYou are working in an isolated task worktree.\n"
                    f"Task ID: {record.task_id}\nBase commit: {record.base_commit}\n"
                    f"Branch: {record.branch}\nWorkspace: {record.worktree}\n"
                    "Edit and test here. Commit completed changes on this branch. "
                    "Do not push, merge into another checkout, or remove this worktree. "
                    "The parent will review and integrate your result. Stop all jobs before "
                    "finishing; remaining jobs will be terminated when delegation ends."
                )
                output = await self._run_delegation(
                    ctx,
                    agent_name,
                    replace(self._agents[agent_name], agent=worker),
                    task=prompt,
                    key=key,
                )
            status = "completed" if _outcome.get() == "ok" else "failed"
            record = await _finish(store, record.task_id, status, output)
        except asyncio.CancelledError:
            await _finish(
                store, record.task_id, "cancelled", "Delegation cancelled; partial work preserved."
            )
            raise
        except Exception as error:
            # Preserve the artifact even when Harness reports a retry or setup fails.
            record = await _finish(
                store, record.task_id, "failed", f"{type(error).__name__}: {error}"
            )
        finally:
            _outcome.reset(token)
        return {
            **asdict(record),
            "workspace_mode": "isolated",
            "verification": "Worker-reported; see summary.",
        }

    async def _settle(self, *args, **kwargs):
        ended = await super()._settle(*args, **kwargs)
        _outcome.set(ended.outcome)
        return ended

    async def integrate_task(self, task_id: str) -> dict:
        """Merge a completed task into its recorded parent, never mainline.

        Requires clean parent and child checkouts. Conflicts remain in the parent;
        resolve and commit them, then retry. The child is kept for worktree cleanup.
        """
        try:
            record = await _joined(
                asyncio.create_task(
                    asyncio.to_thread(TaskWorktrees(self.workspace).integrate, task_id)
                )
            )
            return asdict(record)
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error

    async def discard_task(self, task_id: str, confirm: bool = False) -> dict:
        """Remove an inactive task checkout and branch.

        Unintegrated or dirty work requires confirm=True, only after the user has
        explicitly approved discarding it. Active tasks cannot be discarded.
        """
        try:
            record = await _joined(
                asyncio.create_task(
                    asyncio.to_thread(TaskWorktrees(self.workspace).discard, task_id, confirm)
                )
            )
            return asdict(record)
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error

    async def list_task_worktrees(self) -> list[dict]:
        """List this parent's persistent worker artifacts, including failed tasks."""
        try:
            return [asdict(record) for record in TaskWorktrees(self.workspace).list()]
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error
