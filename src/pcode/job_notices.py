"""Deliver job exits to the model at the next request, so it never polls.

The reason a model writes `sleep 30; cat status.json` is that nothing else will
ever tell it the command finished. Steering already proves the seam exists: a
capability can append to the request the framework is about to send, between
model requests and never mid-tool. Completion notices ride the same seam, which
is why a background job costs no extra model requests and no waiting turns.
"""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart

from pcode.jobs import JobRegistry, format_duration


def notice(job) -> str:
    text = f"[{job.id}] {job.command} → {job.outcome()} after {format_duration(job.elapsed)}."
    if job.stopped:
        return text + " It was stopped, so its output may be incomplete."
    return text + f' Read its output with job_output("{job.id}").'


class JobNotices(AbstractCapability):
    def __init__(self, jobs: JobRegistry) -> None:
        self.jobs = jobs

    async def before_model_request(self, ctx, request_context):
        # Appended to the framework's own request, so tool results stay ahead
        # of the notice and the notice is persisted with the conversation.
        for job in self.jobs.take_announcements("model"):
            request_context.messages[-1].parts.append(UserPromptPart(notice(job)))
        return request_context
