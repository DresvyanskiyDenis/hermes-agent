"""Release of a restart-safe cron worker's transient scope unit once its ledger row is terminal.

A ``--collect`` scope stays ``loaded active running`` while anything lives in its cgroup, so a
background job that outlived the execution held the unit alive for days after the row
completed. The gateway's waiter releases it, by the exact unit name only.
"""

from __future__ import annotations

import subprocess
import threading
from unittest.mock import Mock

from cron import scheduler_detached_worker as detached


class _FakeProcess:
    def __init__(self, pid: int, *, hangs: bool = False):
        self.pid = pid
        self._hangs = hangs
        self.killed = False

    def wait(self, timeout=None):
        if self._hangs and not self.killed:
            raise subprocess.TimeoutExpired("worker", timeout)
        return 0

    def kill(self):
        self.killed = True


def _run_release(monkeypatch, process, unit, *, active: bool, **kwargs):
    stops: list = []
    monkeypatch.setattr(detached, "_scope_unit_is_active", lambda name: active)
    monkeypatch.setattr("tools.process_registry._stop_systemd_unit", lambda *a: stops.append(a) or True)
    started: list[threading.Thread] = []

    class _RecordingThread(threading.Thread):
        def start(self):
            started.append(self)
            super().start()

    monkeypatch.setattr(detached.threading, "Thread", _RecordingThread)
    detached.schedule_scope_unit_release(process, unit, grace_seconds=kwargs.get("grace", 0.05),
                                         poll_seconds=0.01, deadline_seconds=0.01)
    [thread] = started
    assert thread.name == f"cron-scope-release-{process.pid}"
    thread.join(timeout=5)
    assert not thread.is_alive()
    return stops


def test_release_stops_exact_unit_when_active_after_worker_exit(monkeypatch):
    unit = "hermes-worker-cron-j1-exec-e1.scope"
    stops = _run_release(monkeypatch, _FakeProcess(4101), unit, active=True)
    assert stops == [(unit,)]
    assert not any(ch in stops[0][0] for ch in "*?[")


def test_release_skips_when_unit_already_inactive(monkeypatch):
    stops = _run_release(monkeypatch, _FakeProcess(4102), "hermes-worker-cron-j1-exec-e2.scope", active=False)
    assert stops == []


def test_release_kills_worker_at_deadline(monkeypatch):
    process = _FakeProcess(4103, hangs=True)
    unit = "hermes-worker-cron-j1-exec-e3.scope"
    stops = _run_release(monkeypatch, process, unit, active=True, grace=0.02)
    assert process.killed
    assert stops == [(unit,)]


def test_body_schedules_release_on_terminal(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "get_execution", lambda _eid: {"id": "e1", "status": "completed"},
                        raising=False)
    release, reap = Mock(), Mock()
    monkeypatch.setattr(detached, "schedule_scope_unit_release", release)
    monkeypatch.setattr(detached, "reap_terminal_worker_in_background", reap)
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 1.0)
    process.poll.return_value = None

    unit = "hermes-worker-cron-j1-exec-e1.scope"
    assert scheduler._wait_for_external_cron_worker_body(process, execution_id="e1", scope_unit=unit)
    release.assert_called_once_with(process, unit)
    reap.assert_not_called()

    release.reset_mock()
    assert scheduler._wait_for_external_cron_worker_body(process, execution_id="e1", scope_unit=None)
    release.assert_not_called()
    reap.assert_called_once_with(process)


def test_body_schedules_release_after_worker_exit_with_terminal_row(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "get_execution", lambda _eid: {"id": "e1", "status": "failed"},
                        raising=False)
    release = Mock()
    monkeypatch.setattr(detached, "schedule_scope_unit_release", release)
    process = Mock()
    process.wait.return_value = 1

    unit = "hermes-worker-cron-j1-exec-e1.scope"
    assert scheduler._wait_for_external_cron_worker_body(process, execution_id="e1", scope_unit=unit)
    release.assert_called_once_with(process, unit)


def test_scope_unit_name_matches_dispatch_argv(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    argv = pr._build_systemd_scope_argv(["true"], unit_suffix="cron-j1-exec-e1")
    # systemd appends ``.scope`` to a ``--scope`` unit name given without a suffix.
    assert f"{argv[argv.index('--unit') + 1]}.scope" == detached.build_cron_worker_unit_name("j1", "e1")
