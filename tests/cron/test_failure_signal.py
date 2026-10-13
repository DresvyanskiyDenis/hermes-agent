"""The open-failure signal file an external watchdog reads (cron/failure_signal.py).

Contract: ``mark_job_run`` leaves ``<store>/cron/failures/<job_id>.json`` for every open
failure — except while an unreachable-model re-run is parked — and deletes it on the next
green run.
"""

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from cron import failure_signal as fs
from cron import unreachable_retry as ur
from cron.incidents import _error_signature
from cron.jobs import create_job, get_job, mark_job_run, use_cron_store


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _path(home, job_id):
    return home / "cron" / "failures" / f"{job_id}.json"


def _signal(home, job_id):
    path = _path(home, job_id)
    return json.loads(path.read_text()) if path.exists() else None


def _transport_death() -> RuntimeError:
    exc = RuntimeError("Hermes can't reach the model provider")
    exc.failure_reason = "timeout"
    exc.api_calls = 4
    return exc


def test_failed_run_writes_signal_and_green_run_clears_it(tmp_cron_home):
    job_id = create_job("nightly report", "every 24h")["id"]
    error = "ValueError: the report template is missing"

    assert mark_job_run(job_id, False, error)
    job = get_job(job_id)
    sig = _signal(tmp_cron_home, job_id)
    assert sig == {
        "schema": "cron-failure-signal/1",
        "job_id": job_id,
        "job_name": "nightly report",
        "profile": "default",
        "at": job["last_run_at"],
        "kind": "error",
        "error": error,
        "error_sig": _error_signature(job_id, error),
        "failure_streak": 1,
        "next_run_at": job["next_run_at"],
        "retry_ladder": None,
    }
    path = _path(tmp_cron_home, job_id)
    assert path.stat().st_mode & 0o777 == 0o600

    assert mark_job_run(job_id, False, error)
    assert _signal(tmp_cron_home, job_id)["failure_streak"] == 2  # latest failure wins

    assert mark_job_run(job_id, True)
    assert not path.exists()


def test_explicit_status_is_the_signal_kind(tmp_cron_home):
    job_id = create_job("mcp digest", "every 24h")["id"]
    assert mark_job_run(job_id, False, "[blocked_config] server gone", status="blocked_config")
    assert _signal(tmp_cron_home, job_id)["kind"] == "blocked_config"


def test_delivery_failure_is_an_open_failure(tmp_cron_home):
    job_id = create_job("inbox digest", "every 24h")["id"]
    assert mark_job_run(job_id, False, "boom")
    assert mark_job_run(job_id, True, delivery_error="telegram: chat not found")
    sig = _signal(tmp_cron_home, job_id)
    assert sig["kind"] == "delivery_failed"
    assert sig["error"] == "telegram: chat not found"
    assert sig["at"] == get_job(job_id)["last_run_at"]


def test_delivery_queued_neither_opens_nor_clears_a_signal(tmp_cron_home):
    job_id = create_job("queued digest", "every 24h")["id"]
    assert mark_job_run(job_id, True, status="delivery_queued")
    assert not _path(tmp_cron_home, job_id).exists()

    assert mark_job_run(job_id, False, "boom")
    before = _signal(tmp_cron_home, job_id)
    assert mark_job_run(job_id, True, status="delivery_queued")
    assert _signal(tmp_cron_home, job_id) == before


@pytest.mark.parametrize("exc", [
    socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
    _transport_death(),  # patch 0020: a mid-run transport death rides the same ladder
], ids=["unreachable", "midrun-transport-death"])
def test_pending_rungs_stay_silent_exhausted_ladder_signals(tmp_cron_home, exc):
    # The scheduler's classification (run_job) that sets mark_job_run's model_unreachable flag.
    flagged = ur.is_model_unreachable_failure(exc) or ur.is_midrun_transport_death_failure(exc)
    assert flagged
    error = f"{type(exc).__name__}: {exc}"
    job_id = create_job("weekly market watch", "every 24h")["id"]
    now = datetime.now(timezone.utc)

    for _ in ur.RETRY_DELAYS_SECONDS:
        assert mark_job_run(job_id, False, error, model_unreachable=flagged)
        assert get_job(job_id)[ur.STATE_KEY]
        assert _signal(tmp_cron_home, job_id) is None

    assert mark_job_run(job_id, False, error, model_unreachable=flagged)
    job = get_job(job_id)
    assert job.get(ur.STATE_KEY) is None
    assert datetime.fromisoformat(job["next_run_at"]) - now > timedelta(hours=1)  # natural schedule
    sig = _signal(tmp_cron_home, job_id)
    assert sig["retry_ladder"] == {
        "attempt": len(ur.RETRY_DELAYS_SECONDS),
        "delays_seconds": list(ur.RETRY_DELAYS_SECONDS),
    }
    assert sig["next_run_at"] == job["next_run_at"]


def test_secrets_are_redacted(tmp_cron_home):
    key = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 3
    job_id = create_job("leaky", "every 24h")["id"]
    assert mark_job_run(job_id, False, f"AuthenticationError: bad key {key}")
    path = _path(tmp_cron_home, job_id)
    assert key not in path.read_text()


def test_signal_follows_the_active_cron_store(tmp_cron_home, tmp_path):
    other = tmp_path / ".hermes" / "profiles" / "work"
    other.mkdir(parents=True)
    with use_cron_store(other):
        job_id = create_job("work brief", "every 24h")["id"]
        assert mark_job_run(job_id, False, "boom")
        assert fs.signal_path_for(job_id) == _path(other, job_id)
    assert _signal(other, job_id)["profile"] == "work"
    assert not (tmp_cron_home / "cron" / "failures").exists()


def test_signal_write_failure_never_breaks_mark_job_run(tmp_cron_home, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fs, "atomic_write_text", _boom)
    job_id = create_job("unlucky", "every 24h")["id"]
    assert mark_job_run(job_id, False, "boom")
    assert get_job(job_id)["last_status"] == "error"
    assert _signal(tmp_cron_home, job_id) is None
