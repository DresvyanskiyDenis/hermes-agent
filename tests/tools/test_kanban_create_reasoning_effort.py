"""``kanban_create`` forwards ``reasoning_effort`` to the task row.

The ``_kanban_handler`` wrapper rejects any argument the schema doesn't declare, so the
schema property and the handler pass-through must ship together.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def kanban_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _stored_effort(task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, task_id).reasoning_effort


def test_schema_declares_reasoning_effort():
    import tools.kanban_tools  # noqa: F401  (registers the tools)
    from tools.registry import registry
    props = registry.get_schema("kanban_create")["parameters"]["properties"]
    assert props["reasoning_effort"]["type"] == "string"


@pytest.mark.parametrize("args, stored", [({"reasoning_effort": "minimal"}, "minimal"), ({}, None)])
def test_create_passes_reasoning_effort_through(kanban_env, args, stored):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "child", "assignee": "peer", **args}))
    assert d["ok"] is True
    assert _stored_effort(d["task_id"]) == stored


def test_create_rejects_typo_without_creating(kanban_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": "child", "assignee": "peer", "reasoning_effort": "hgih"})
    assert "reasoning_effort" in json.loads(out)["error"]
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []
