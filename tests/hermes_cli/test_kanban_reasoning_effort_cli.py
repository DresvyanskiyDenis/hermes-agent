"""`kanban create/edit --reasoning-effort` — per-task thinking depth on the CLI surfaces.

The backend (``create_task(reasoning_effort=)``, ``set_reasoning_effort``) normalizes and
validates; these tests pin that the CLI forwards the flag, renders typos as a clean error
instead of a traceback, and lets an edit touch ONLY the effort (even on a running card).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _effort(task_id):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id).reasoning_effort


def _create_id(flags: str) -> str:
    out = json.loads(kc.run_slash(f"create 'x' --assignee p --json {flags}"))
    return out["id"]


@pytest.mark.parametrize("given, stored", [("low", "low"), ("XHigh", "xhigh"), ("none", "none")])
def test_create_stores_normalized_effort_and_json_exposes_it(kanban_home, given, stored):
    out = json.loads(kc.run_slash(f"create 'x' --assignee p --json --reasoning-effort {given}"))
    assert out["reasoning_effort"] == stored
    assert _effort(out["id"]) == stored


def test_create_without_flag_leaves_profile_default(kanban_home):
    assert _effort(_create_id("")) is None


def test_create_rejects_unknown_level_cleanly(kanban_home):
    out = kc.run_slash("create 'x' --assignee p --reasoning-effort bananas")
    assert "reasoning_effort must be one of" in out
    assert "Traceback" not in out
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_edit_sets_and_clears_effort_on_running_card(kanban_home):
    tid = _create_id("")
    with kbc.connect_closing() as conn:
        kb.claim_task(conn, tid)
        assert kb.get_task(conn, tid).status == "running"

    out = kc.run_slash(f"edit {tid} --reasoning-effort high")
    assert "provide --title" not in out
    assert "reasoning-effort = high" in out
    assert _effort(tid) == "high"

    out = kc.run_slash(f"edit {tid} --reasoning-effort clear")
    assert "cleared" in out
    assert _effort(tid) is None


def test_edit_rejects_unknown_level_and_unknown_id(kanban_home):
    tid = _create_id("--reasoning-effort low")
    out = kc.run_slash(f"edit {tid} --reasoning-effort bananas")
    assert "reasoning_effort must be one of" in out and "Traceback" not in out
    assert _effort(tid) == "low"
    assert "cannot edit t_missing" in kc.run_slash("edit t_missing --reasoning-effort low")
