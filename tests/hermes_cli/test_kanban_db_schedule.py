"""The ``scheduled`` park's deadline timer: ``schedule_task`` records
``scheduled_until`` and the dispatcher tick (``recompute_ready``) wakes due
parks without any external unblock."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_schedule as kbs

PAST = "2000-01-01T00:00:00Z"
FUTURE = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _woken_events(conn, tid):
    return [e for e in kb.list_events(conn, tid)
            if e.kind == "unblocked" and (e.payload or {}).get("reason") == "scheduled deadline elapsed"]


def test_reason_marker_sets_deadline(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, reason="SCHEDULED_UNTIL=2026-10-14T09:30:00Z retry")
        expected = int(datetime.datetime(2026, 10, 14, 9, 30, tzinfo=datetime.UTC).timestamp())
        assert kb.get_task(conn, t).scheduled_until == expected
        assert any(e.kind == "scheduled" and e.payload["scheduled_until"] == expected
                   for e in kb.list_events(conn, t))


def test_naive_stamp_is_machine_local(kanban_home):
    stamp = "2026-10-14T09:30:00"
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, until=stamp)
        expected = int(datetime.datetime.fromisoformat(stamp).astimezone().timestamp())
        assert kb.get_task(conn, t).scheduled_until == expected


def test_explicit_until_beats_reason_marker(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, reason=f"SCHEDULED_UNTIL={PAST}", until=FUTURE)
        assert kb.get_task(conn, t).scheduled_until == int(
            datetime.datetime.fromisoformat(FUTURE).timestamp())


def test_garbage_until_raises_without_parking(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        with pytest.raises(ValueError, match="unparseable until"):
            kb.schedule_task(conn, t, until="next tuesday")
        assert kb.get_task(conn, t).status == "ready"


def test_garbage_reason_marker_parks_without_timer(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, reason="SCHEDULED_UNTIL=9999-99-99T99")
        task = kb.get_task(conn, t)
        assert (task.status, task.scheduled_until) == ("scheduled", None)


def test_future_deadline_stays_parked(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, until=FUTURE)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, t).status == "scheduled"
        assert kb.claim_task(conn, t) is None


def test_wake_fires_at_deadline_not_before(kanban_home):
    stamp = "2030-06-01T12:00:00+00:00"
    deadline = int(datetime.datetime.fromisoformat(stamp).timestamp())
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, until=stamp)
        with kb.write_txn(conn):
            assert kbs.wake_due_scheduled(conn, deadline - 1) == 0
        assert kb.get_task(conn, t).status == "scheduled"
        assert not _woken_events(conn, t)
        with kb.write_txn(conn):
            assert kbs.wake_due_scheduled(conn, deadline) == 1
        task = kb.get_task(conn, t)
        assert (task.status, task.scheduled_until) == ("ready", None)
        assert _woken_events(conn, t)


def test_markerless_park_is_not_woken(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, reason="waiting on vendor")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "scheduled"
        assert not _woken_events(conn, t)


def test_wake_with_open_parent_lands_todo_then_promotes(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="upstream", assignee="ops")
        child = kb.create_task(conn, title="downstream", assignee="ops", parents=[parent])
        assert kb.schedule_task(conn, child, until=PAST)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"
        assert kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_unblock_then_markerless_repark_clears_deadline(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, until=FUTURE)
        assert kb.unblock_task(conn, t)
        assert kb.get_task(conn, t).scheduled_until is None
        assert kb.schedule_task(conn, t, reason="event park")
        assert kb.get_task(conn, t).scheduled_until is None
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "scheduled"


def test_dispatcher_tick_wakes_due_park(kanban_home):
    """No unblock anywhere: one ``dispatch_once`` tick is the only actor."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="wait", assignee="ops")
        assert kb.schedule_task(conn, t, until=PAST)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert res.promoted >= 1
        task = kb.get_task(conn, t)
        assert (task.status, task.scheduled_until) == ("ready", None)
        assert _woken_events(conn, t)
