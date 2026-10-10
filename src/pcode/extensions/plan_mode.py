"""Plan mode: think a change through with the user before building it.

`/plan [topic]` turns it on. While it is on, each user turn carries a reminder
to investigate, keep a plan in `.pcode/plans/<slug>.md`, and hold off on
implementing until the user approves. Approval is the model's call: when the
user says "go ahead" (in whatever words), it calls `exit_plan_mode` and starts
building in the same turn. `/plan off` is the manual way out.

The model is trusted rather than fenced in: nothing blocks edits, the reminder
just says what the user wants. The active plan lives in a marker file beside
the plans, so it survives `/reload` (which re-imports this module) and restarts.
"""

import re

from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from pcode.steering import run_request

PLANS = ".pcode/plans"
ACTIVE = ".active"

REMINDER = """\
<plan-mode>
Plan mode is on. The user wants to agree on a plan before any implementation.
- Investigate freely: read files, search, run read-only commands, research.
- Write the plan to `{path}` and keep it current as the discussion moves on.
  The user may edit that file between turns, so re-read it before revising.
- Do not start implementing. A small throwaway experiment to answer a question
  is fine; making the change itself is not.
- End each turn with the open questions or a clear "ready to proceed?".
- When the user approves (for example "go ahead", "do it", "lgtm", "ship it"),
  call `exit_plan_mode`, then implement the plan in that same turn. Approval
  with changes ("yes, but skip step 3") counts: fold the change in, then exit.
</plan-mode>"""


def is_worker(ctx) -> bool:
    """A shared delegate runs this extension's hooks too; plan mode is the parent's."""
    return getattr(ctx.agent, "name", None) == "worker"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].rstrip("-") or "plan"


def setup(pcode) -> None:
    if pcode.is_worker:
        return
    plans = pcode.workspace / PLANS
    marker = plans / ACTIVE

    def active() -> str | None:
        try:
            return marker.read_text().strip() or None
        except FileNotFoundError:
            return None

    def relative(slug: str) -> str:
        return f"{PLANS}/{slug}.md"

    def latest() -> str | None:
        files = (
            sorted(plans.glob("*.md"), key=lambda p: p.stat().st_mtime) if plans.is_dir() else []
        )
        return files[-1].stem if files else None

    def start(slug: str) -> None:
        plans.mkdir(parents=True, exist_ok=True)
        ignore = plans / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        marker.write_text(slug + "\n")

    def plan_command(argument: str) -> None:
        argument = argument.strip()
        current = active()
        if argument == "off":
            if current is None:
                pcode.ui.notify("Plan mode is already off.")
                return
            marker.unlink(missing_ok=True)
            pcode.ui.notify(f"Plan mode off. The plan stays in {relative(current)}.")
            return
        if argument == "status":
            pcode.ui.notify(
                f"Plan mode on: {relative(current)}" if current else "Plan mode is off."
            )
            return
        slug = slugify(argument) if argument else (current or latest() or "plan")
        start(slug)
        exists = (pcode.workspace / relative(slug)).exists()
        verb = "Resuming" if exists else "Planning in"
        pcode.ui.notify(
            f"Plan mode on. {verb} {relative(slug)}. Describe the change; "
            "say 'go ahead' when the plan looks right."
        )

    pcode.register_command(
        "/plan",
        "Plan before implementing; the model exits when you approve",
        plan_command,
        arguments=("off", "status"),
        argument_descriptions={
            "off": "Leave plan mode (the plan file is kept)",
            "status": "Show whether plan mode is on and its file",
        },
    )

    @pcode.tool
    def exit_plan_mode() -> str:
        """Leave plan mode once the user has approved the plan, then implement it.

        Call this only when plan mode is on and the user has said to go ahead.
        """
        current = active()
        if current is None:
            return "Plan mode was not on; carry on."
        marker.unlink(missing_ok=True)
        pcode.ui.notify("Plan approved; plan mode off.")
        return (
            f"Plan mode off. Implement the plan in {relative(current)} now, "
            "tracking progress with write_plan, and update the file if the plan changes."
        )

    @pcode.hooks.on.before_tool_execute
    async def workers_cannot_exit(ctx, *, call, tool_def, args):
        if tool_def.name == "exit_plan_mode" and is_worker(ctx):
            raise ModelRetry("Only the main agent can leave plan mode.")
        return args

    @pcode.hooks.on.before_model_request
    async def remind(ctx, request_context):
        # Once per user turn, persisted in history so the prompt prefix stays
        # cached. A retried turn resends the same request, so skip one that
        # already carries the reminder or is mid-turn (holds tool results).
        current = active()
        if current is None or is_worker(ctx):
            return request_context
        request = run_request(ctx, request_context)
        parts = request.parts if isinstance(request, ModelRequest) else ()
        prompts = [p for p in parts if isinstance(p, UserPromptPart)]
        if (
            prompts
            and not any(isinstance(p, ToolReturnPart) for p in parts)
            and not any(str(p.content).startswith("<plan-mode>") for p in prompts)
        ):
            parts.append(UserPromptPart(REMINDER.format(path=relative(current))))
        return request_context
