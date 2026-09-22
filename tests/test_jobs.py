"""The job registry and the tools built on it: waiting, readiness, delivery."""

import asyncio
import json
import shlex
import sys

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_coder
from pcode.job_notices import notice
from pcode.jobs import JobRegistry, format_duration, registry
from pcode.live import AgentRuntime
from pcode.runtime import ToolSummary


def command(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def wait_for(predicate, timeout=10):
    async def poll():
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    asyncio.run(poll())


def until_finished(jobs, *watched):
    def done():
        jobs.refresh()
        return all(not job.running for job in watched)

    wait_for(done)


def test_registry_reports_exit_without_anyone_waiting(tmp_path):
    jobs = JobRegistry()
    job = jobs.launch(command("import sys; print('hi'); sys.exit(3)"), cwd=tmp_path)
    assert job.id == "j1" and job.running
    until_finished(jobs, job)
    assert job.exit_code == 3
    assert job.outcome() == "exit 3"
    assert jobs.read_output(job)[0].strip() == "hi"
    # Nothing was waiting on it, so nothing has been told yet.
    assert [j.id for j in jobs.take_announcements("ui")] == []


def test_background_jobs_are_announced_once_per_channel(tmp_path):
    jobs = JobRegistry()
    job = jobs.launch(command("pass"), cwd=tmp_path, background=True)
    until_finished(jobs, job)
    assert [j.id for j in jobs.take_announcements("model")] == ["j1"]
    # Each channel hears once: the terminal's copy is independent of the model's.
    assert [j.id for j in jobs.take_announcements("model")] == []
    assert [j.id for j in jobs.take_announcements("ui")] == ["j1"]


def test_finished_job_logs_are_evicted_but_running_ones_are_kept(tmp_path):
    jobs = JobRegistry(retain=1)
    first = jobs.launch(command("pass"), cwd=tmp_path)
    second = jobs.launch(command("pass"), cwd=tmp_path)
    live = jobs.launch(command("import time; time.sleep(60)"), cwd=tmp_path)
    until_finished(jobs, first, second)
    assert not first.directory.exists()
    assert second.directory.exists()
    assert "j1" not in jobs.jobs and "j2" in jobs.jobs
    # A running job owns its log: the command is still writing to it.
    jobs.shutdown()
    assert live.directory.exists() and live.running
    jobs.stop(live)
    assert live.outcome() == "stopped"


def test_stop_kills_the_whole_process_group(tmp_path):
    jobs = JobRegistry()
    marker = tmp_path / "child-started"
    job = jobs.launch(
        command(
            "import subprocess, sys, pathlib, time; "
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"pathlib.Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)"
        ),
        cwd=tmp_path,
    )
    wait_for(lambda: marker.exists())
    child = int(marker.read_text())
    assert jobs.stop(job)

    def gone():
        import os

        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    wait_for(gone)


def test_format_duration_reads_at_a_glance():
    assert format_duration(0.25) == "250ms"
    assert format_duration(4.2) == "4.2s"
    assert format_duration(95) == "1m35s"
    assert format_duration(3725) == "1h02m"


def runtime_with(calls_plan, workspace):
    """An agent that issues the given tool calls, one per model request."""
    seen = []

    async def model(messages, info):
        step = len(seen)
        seen.append(step)
        if step < len(calls_plan):
            name, args = calls_plan[step]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
        else:
            yield "Done."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)])
    )
    return runtime


def results_of(runtime, prompt="go"):
    async def run():
        return [e async for e in runtime.stream(prompt)]

    return [e for e in asyncio.run(run()) if isinstance(e, ToolSummary)]


def test_background_then_wait_returns_the_result_without_rerunning(tmp_path):
    source = "import time; time.sleep(.3); print('LATE')"
    runtime = runtime_with(
        [
            ("shell", {"command": command(source), "background": True}),
            ("wait_for_job", {"job_id": "j1"}),
        ],
        tmp_path,
    )
    background, waited = results_of(runtime)
    assert background.result.startswith("[j1 · running · pid ")
    # The same job, finished: output and status, and no handles to manage.
    assert waited.result.startswith("LATE\n[j1 · exit 0 · ")
    assert 'wait_for_job("j1")' not in waited.result
    # One launch, not two.
    assert len(runtime.jobs.jobs) == 1


def test_purpose_from_the_model_reaches_the_row_and_the_registry(tmp_path):
    runtime = runtime_with(
        [
            (
                "shell",
                {
                    "command": command("print('done')"),
                    "background": True,
                    "purpose": "checking the build",
                },
            )
        ],
        tmp_path,
    )
    (started,) = [e for e in results_of(runtime) if e.name == "shell"]
    assert started.detail.startswith("checking the build · ")
    assert runtime.jobs.get("j1").purpose == "checking the build"


def test_until_output_returns_at_readiness_for_a_server_that_never_exits(tmp_path):
    source = "import time; print('listening on 8080'); time.sleep(60)"
    runtime = runtime_with(
        [
            ("shell", {"command": command(source), "background": True}),
            ("wait_for_job", {"job_id": "j1", "until_output": "listening on (\\d+)"}),
        ],
        tmp_path,
    )
    try:
        _, ready = results_of(runtime)
        assert "Matched until_output" in ready.result
        assert "[j1 · running · pid " in ready.result
        assert runtime.jobs.get("j1").running
    finally:
        runtime.jobs.stop_all()


def test_completed_job_is_reported_to_the_model_at_the_next_request(tmp_path):
    """The point of the design: no `sleep`, no polling call, no extra turn."""
    prompts = []

    async def model(messages, info):
        parts = [p for m in messages for p in m.parts]
        prompts.append([p for p in parts if type(p).__name__ == "UserPromptPart"])
        step = len(prompts)
        if step == 1:
            yield {
                0: DeltaToolCall(
                    name="shell",
                    json_args=json.dumps({"command": command("print('FAST')"), "background": True}),
                )
            }
        elif step == 2:
            # Independent work while the job runs; by the next request it is done.
            yield {0: DeltaToolCall(name="list_jobs", json_args="{}")}
        else:
            yield "Done."

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        # Let the command finish while the model is between requests.
        async for event in runtime.stream("go"):
            await asyncio.sleep(0.05)

    asyncio.run(run())
    delivered = [p.content for turn in prompts for p in turn]
    assert any("j1" in text and "exit 0" in text for text in delivered), delivered
    assert not any("sleep" in text for text in delivered)


def test_unknown_job_is_a_retry_not_a_crash(tmp_path):
    runtime = runtime_with([("wait_for_job", {"job_id": "j99"})], tmp_path)

    async def run():
        return [e async for e in runtime.stream("go")]

    events = asyncio.run(run())
    summaries = [e for e in events if isinstance(e, ToolSummary)]
    assert summaries and summaries[0].outcome == "retry"


def test_purpose_labels_a_job_without_hiding_what_runs(tmp_path):
    jobs = JobRegistry()
    plain = jobs.launch("sleep 60", cwd=tmp_path)
    labelled = jobs.launch(
        "docker compose -f ops/ci/e2e.yml up --abort-on-container-exit",
        cwd=tmp_path,
        background=True,
        purpose="running   the end-to-end\nsuite",
    )
    try:
        # Without a purpose nothing changes: the command is the label.
        assert plain.label() == "sleep 60"
        assert plain.summary().startswith("[j1] sleep 60 → running · ")
        # Whitespace is normalized so a multi-line value cannot break a row.
        assert labelled.label() == "running the end-to-end suite"
        # The inventory keeps the command: a stated intention is not evidence
        # of what is actually running, and this is where you decide to stop it.
        assert "running the end-to-end suite · docker compose -f ops/ci/e2e.yml" in (
            labelled.summary()
        )
    finally:
        jobs.reset()


def test_purpose_leads_the_tool_row_but_never_replaces_the_command():
    from pcode.tool_display import target

    args = {"command": "make test", "purpose": "running the test suite"}
    assert target("shell", args) == "running the test suite · make test"
    assert target("shell", {"command": "make test"}) == "make test"
    # A non-string or blank purpose is ignored rather than rendered.
    assert target("shell", {"command": "make test", "purpose": "  "}) == "make test"
    assert target("shell", {"command": "make test", "purpose": 3}) == "make test"


def test_notice_prefers_the_purpose_over_the_command(tmp_path):
    jobs = JobRegistry()
    job = jobs.launch("sleep 1", cwd=tmp_path, background=True, purpose="warming the cache")
    job.exit_code, job.ended_at = 0, job.started_at + 1
    assert notice(job).startswith("[j1] warming the cache → exit 0 after 1.0s.")
    jobs.reset()


def test_notice_text_names_the_job_and_how_to_read_it():
    jobs = JobRegistry()
    job = jobs.launch("true", cwd=".", background=True)
    job.exit_code, job.ended_at = 0, job.started_at + 2
    assert notice(job) == '[j1] true → exit 0 after 2.0s. Read its output with job_output("j1").'
    job.stopped = True
    assert "stopped" in notice(job)
    jobs.reset()


def test_registry_is_shared_so_jobs_outlive_the_run(tmp_path):
    shell = next(c for c in create_coder(tmp_path).capabilities if type(c).__name__ == "JobShell")
    assert shell.get_toolset()._jobs is registry()


@pytest.mark.parametrize("policy", ["detach", "stop"])
def test_cancel_policy_defaults_back_to_detach(policy):
    jobs = JobRegistry()
    jobs.cancel_policy = policy
    jobs.reset()
    assert jobs.cancel_policy == "detach"
