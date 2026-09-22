"""`/jobs`, and the terminal's side of job reporting."""

import shlex
import sys

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
    assert app.report_finished_jobs() is False


def test_jobs_rejects_unknown_actions_and_ids(tmp_path):
    app, jobs = app_with_jobs()
    with pytest.raises(ValueError, match="list, stop ID, or stop all"):
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
    assert PreviewApp().report_finished_jobs() is False
