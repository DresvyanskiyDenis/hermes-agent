"""The update that STOPPED the gateways starts them again in the same run.

``hermes update`` stops every gateway it owns at the commit point (``update_cmd_posix_pause``) and
resumes the set from the completion, before the product builds. When that resume deferred on a tree
gate, the run finished without a second attempt: the post-restart fleet sweep only enumerates ACTIVE
units, so a stopped unit was invisible to it, the plan's runtime came back "never touched", and the
update still reported success — the estate stayed down until someone started the unit by hand.

These tests pin the two halves of the repair:

* the completion's pre-build resume is the run that synced this tree's dependencies, so the tree
  gate never holds it for a currency probe (the transient ``deps_synced`` token flag);
* a set an earlier phase deferred is re-armed and retried after the builds, so the run that stopped
  it is the run that starts it.
"""

from __future__ import annotations

from types import SimpleNamespace

from hermes_cli import update_cmd
from hermes_cli import update_cmd_windows as windows
from hermes_cli import update_completion


def _outcome() -> SimpleNamespace:
    """``_GatewayRestartOutcome``'s fields the merge touches."""
    return SimpleNamespace(
        incomplete=False, phase_errors=[], restarted_services=[], relaunched_profiles=[],
        externally_supervised_profiles=[], killed_pids=set(), failed_or_stale_units=[],
    )


def _record_resumes(monkeypatch) -> list[dict]:
    """Patch the frozen ``hermes_cli.main`` helper seam with a recording stand-in."""
    calls: list[dict] = []

    def resume(token, **kwargs):
        # ``deps_synced`` is a transient flag on the token, popped by the real helper.
        calls.append({"token": token, "deps_synced": bool(token.get("deps_synced")), **kwargs})

    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
        _resume_windows_gateways_after_update=resume, _is_windows=lambda: False))
    return calls


def test_the_pre_build_resume_is_never_held_for_the_sync_it_follows(monkeypatch):
    """``_prepare`` synced this tree's dependencies before the completion child existed: the
    before_build resume says so, and only the recovery path is left to the currency probe."""
    calls = _record_resumes(monkeypatch)
    token = {"resume_needed": True, "platform": "posix", "pause_id": "p",
             "posix_units": [{"kind": "systemd", "scope": "user", "unit": "hermes-gateway.service",
                              "pid": 2522}]}

    update_completion._resume_paused_before_build({"windows_resume": token})

    assert calls == [{"token": token, "deps_synced": True}], (
        "the update's own resume was left to a dependency probe its sync already answered")


def test_the_dependency_flag_is_transient_and_never_reaches_the_record(monkeypatch):
    """The flag rides the frozen ``(token)`` seam, so it must be consumed even on the path that
    resumes nothing — a token written back to the pause record must not carry it."""
    token = {"resume_needed": False, "deps_synced": True}

    windows._resume_windows_gateways_after_update(token)

    assert "deps_synced" not in token


def test_a_deferred_paused_set_is_retried_after_the_builds(monkeypatch, capsys):
    """The incident: the resume deferred pre-build, the restart sweep found no ACTIVE unit to
    restart, and the run ended with the gateway stopped. The post-build phase now takes the set
    back instead of handing it to the next launch."""
    calls = _record_resumes(monkeypatch)
    token = {"resume_needed": False, "platform": "posix", "pause_id": "p",
             "resume_deferred": "dependencies are not current for the updated code yet",
             "posix_units": [{"kind": "systemd", "scope": "user",
                              "unit": "hermes-gateway.service", "pid": 2522}]}

    windows._resume_windows_gateways_and_merge_outcome(_outcome(), token, True)

    assert calls == [{"token": token, "deps_synced": True}], "the deferred set was never retried"
    assert "resume_deferred" not in token
    assert token["resume_needed"] is True, "the set stayed owed for the resume that just ran"
    assert "Retrying the paused gateway restart" in capsys.readouterr().out


def test_a_set_that_resumed_normally_is_not_resumed_twice(monkeypatch, capsys):
    """Only a deferral is re-armed: a set the pre-build resume already started must not be drained
    and restarted a second time by the post-build phase."""
    calls = _record_resumes(monkeypatch)
    token = {"resume_needed": False, "platform": "posix", "pause_id": "p",
             "posix_units": [], "restarted_services": ["hermes-gateway"]}

    windows._resume_windows_gateways_and_merge_outcome(_outcome(), token, True)

    assert calls == [{"token": token, "deps_synced": True}]
    assert token["resume_needed"] is False, "a resumed set was re-armed"
    assert "Retrying the paused gateway restart" not in capsys.readouterr().out


def test_a_resumed_set_forgets_the_deferral_it_carried(monkeypatch):
    """The deferral is a verdict of one attempt: it must not survive into the record and re-arm the
    next run's token for a set that is already back."""
    from hermes_cli import update_pause_record as pause_record

    monkeypatch.setattr(pause_record, "tree_is_whole", lambda *a, **k: (True, ""))
    monkeypatch.setattr(pause_record, "sync", lambda token: None)
    monkeypatch.setattr(windows, "_resume_paused_set", lambda token: None)
    token = {"resume_needed": True, "platform": "posix", "pause_id": "p", "posix_units": [],
             "resume_deferred": "dependencies are not current for the updated code yet"}

    windows._resume_unless_torn(token, deps_synced=True)

    assert "resume_deferred" not in token