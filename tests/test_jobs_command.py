"""`/jobs`, and the terminal's side of job reporting."""

import shlex
import sys
import time

import pytest

from pcode.app import PreviewApp
from pcode.jobs import JobRegistry


class Runtime:
    """Enough runtime for the app's job paths; no model, no session."""

    session = None
    history: list = []

    def __init__(self):
        self.jobs = JobRegistry()


def command(source):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def app_with_jobs():
    app = PreviewApp(model="test:local", runtime=Runtime())
    return app, app.runtime.jobs


def test_jobs_lists_running_first_then_stops_them(tmp_path):
    app, jobs = app_with_jobs()
    jobs.launch(command("pass"), cwd=tmp_path)
    live = jobs.launch(command("import time; time.sleep(60)"), cwd=tmp_path)
    app.jobs("")
    app.jobs("stop all")
    assert not live.running and live.outcome() == "stopped"
    # Stopping reported it, so the idle watcher must not say it again.
    assert app.report_finished_jobs() == []


def test_jobs_arguments_offer_running_ids_for_stop_and_watch(tmp_path):
    app, jobs = app_with_jobs()
    assert app.jobs_arguments() == ("list", "unwatch", "stop all")
    live = jobs.launch(command("import time; time.sleep(60)"), cwd=tmp_path)
    done = jobs.launch(command("pass"), cwd=tmp_path)
    until_finished(jobs, done)
    assert app.jobs_arguments() == (
        "list",
        "unwatch",
        "stop all",
        f"stop {live.id}",
        f"watch {live.id}",
    )
    jobs.stop(live)


def test_jobs_rejects_unknown_actions_and_ids(tmp_path):
    app, jobs = app_with_jobs()
    with pytest.raises(ValueError, match="list, stop ID, stop all, watch ID, or unwatch"):
        app.jobs("burn")
    with pytest.raises(ValueError, match="No job 'j9'"):
        app.jobs("stop j9")


def test_cancel_policy_is_set_for_the_registry_and_survives_a_bare_runtime():
    app, jobs = app_with_jobs()
    app.set_cancel_policy("stop")
    assert jobs.cancel_policy == "stop"
    app.set_cancel_policy("detach")
    assert jobs.cancel_policy == "detach"
    # A preview app without a live runtime has no registry; this must not raise.
    PreviewApp().set_cancel_policy("stop")
    assert PreviewApp().report_finished_jobs() == []
    assert PreviewApp().refresh_jobs() is False
    assert PreviewApp().wake_prompt() is None


def until_finished(jobs, *watched):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        jobs.refresh()
        if all(not job.running for job in watched):
            return
        time.sleep(0.02)
    raise AssertionError("jobs did not finish")


def test_jobs_rows_show_background_work_and_unreported_exits(tmp_path):
    app, jobs = app_with_jobs()
    live = jobs.launch(command("import time; time.sleep(60)"), cwd=tmp_path, purpose="serving")
    assert app.refresh_jobs() is True
    assert [text[:17] for _, text in app.activity.jobs] == ["\u27f3 j1 \u00b7 serving \u00b7 "]
    # A tool call blocking on it is already the spinner row's business.
    live.waiting = True
    app.refresh_jobs()
    assert app.activity.jobs == []
    live.waiting = False
    failed = jobs.launch(command("import sys; sys.exit(2)"), cwd=tmp_path, background=True)
    until_finished(jobs, failed)
    app.refresh_jobs()
    texts = [text for _, text in app.activity.jobs]
    assert any(text.startswith("\u2717 j2 ") and "exit 2" in text for text in texts), texts
    # Once scrollback has it, the row is gone: the exit is no longer news.
    assert [job.id for job in app.report_finished_jobs()] == ["j2"]
    app.refresh_jobs()
    assert [text[:17] for _, text in app.activity.jobs] == ["\u27f3 j1 \u00b7 serving \u00b7 "]
    jobs.stop(live)


def test_jobs_rows_put_running_work_ahead_of_older_exits(tmp_path):
    app, jobs = app_with_jobs()
    done = jobs.launch(command("import sys; sys.exit(3)"), cwd=tmp_path, background=True)
    until_finished(jobs, done)
    live = jobs.launch(command("import time; time.sleep(60)"), cwd=tmp_path, purpose="serving")
    app.refresh_jobs()
    # The exit is older, but the folded rows must not spend themselves on it.
    assert [text.split(" \u00b7 ")[0] for _, text in app.activity.jobs] == [
        f"\u27f3 {live.id}",
        f"\u2717 {done.id}",
    ]
    jobs.stop(live)


def test_wake_prompt_is_the_notice_for_jobs_the_model_launched(tmp_path, monkeypatch):
    app, jobs = app_with_jobs()
    background = jobs.launch(
        command("import sys; print('boom'); sys.exit(1)"), cwd=tmp_path, background=True
    )
    foreground = jobs.launch(command("pass"), cwd=tmp_path)
    until_finished(jobs, background, foreground)
    prompt = app.wake_prompt()
    # The failed background job wakes the model with its tail; the foreground
    # command reported itself inside its own call and is nobody's news.
    assert prompt is not None and "[j1]" in prompt and "boom" in prompt and "j2" not in prompt
    # Waking is delivery: the next request must not repeat it.
    assert app.wake_prompt() is None
    assert [job.id for job in jobs.take_announcements("model")] == []
    adopted = jobs.launch(command("pass"), cwd=tmp_path, background=True)
    adopted.adopted = True
    stopped = jobs.launch(command("import time; time.sleep(60)"), cwd=tmp_path, background=True)
    until_finished(jobs, adopted)
    jobs.stop(stopped)
    assert app.wake_prompt() is None
    fresh = jobs.launch(command("pass"), cwd=tmp_path, background=True)
    until_finished(jobs, fresh)
    monkeypatch.setattr("pcode.app.load_preferences", lambda: {"job_wake": "off"})
    assert app.wake_prompt() is None


def test_jobs_watch_pins_the_tail_until_the_job_ends(tmp_path):
    app, jobs = app_with_jobs()
    live = jobs.launch(
        command("import time; print('serving on 8000', flush=True); time.sleep(60)"), cwd=tmp_path
    )
    with pytest.raises(ValueError, match="No job 'j9'"):
        app.jobs("watch j9")
    app.jobs("watch j1")
    assert app.activity.watched_job == "j1"

    def pinned():
        app.refresh_jobs()
        preview = app.activity.command_outputs.get("job:j1")
        return preview is not None and "serving on 8000" in preview.output

    deadline = time.monotonic() + 10
    while not pinned() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pinned()
    jobs.stop(live)
    app.refresh_jobs()
    assert app.activity.watched_job == "" and "job:j1" not in app.activity.command_outputs
    with pytest.raises(ValueError, match="has finished"):
        app.jobs("watch j1")
