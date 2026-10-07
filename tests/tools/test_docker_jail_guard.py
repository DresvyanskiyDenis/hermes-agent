"""Jail duplicate-mount guard (t_44fdd37a): a spawn whose task bucket misses label reuse must
adopt, refuse, or proceed based on the host paths running hermes containers already mount."""
import json
import logging
import os
import subprocess

import pytest

from tools.environments import docker as docker_env
from tools.environments.path_utils import sanitize_task_id_for_path


def _jail_candidate(tmp_path, *, profile="bot_1", egress="off", image="python:3.11", net="default",
                    task="default", jail="/srv/jail", extra=()):
    """Inspect answers for a running persistent jail held under *task*'s sandbox bucket
    (``jail=None``: only the per-task sandbox dirs, the RL rollout / per-session shape)."""
    bucket = tmp_path / "sandboxes" / "docker" / sanitize_task_id_for_path(task)
    for sub in ("home", "workspace"):
        (bucket / sub).mkdir(parents=True, exist_ok=True)
    binds = [(str(bucket / "home"), "/root"), (str(bucket / "workspace"), "/workspace"),
             *([(jail, "/home/bot")] if jail else []), *extra]
    return {
        "labels": {"hermes-agent": "1", "hermes-profile": profile, "hermes-task-id": task,
                   "hermes-egress": egress},
        "mounts": [{"Type": "bind", "Source": s, "Destination": d, "RW": True} for s, d in binds],
        "image": image, "net": net}


def _mock_jail_guard(monkeypatch, tmp_path, candidates, *, conflict_ps_rc=0):
    """Label-reuse probe always misses; the conflict probe lists *candidates* (cid -> answers,
    ``"raw"`` overriding the labels+mounts inspect output). Returns the captured argv list."""
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))
    # pytest's tmp_path sits under the system tempdir, whose mounts count as volatile and would
    # hide every per-task sandbox dir from the guard; point the process tempdir elsewhere.
    (tmp_path / "proc-tmp").mkdir()
    monkeypatch.setattr(docker_env.tempfile, "gettempdir", lambda: str(tmp_path / "proc-tmp"))
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_get_active_profile_name", lambda: "bot_1")
    monkeypatch.setattr(docker_env, "_readonly_skill_mount_args", lambda: [])
    docker_env._cgroup_limits_ok = True
    calls = []

    def _inspect(c, fmt):
        if fmt == "{{.Config.Image}}":
            return c["image"]
        if fmt == "{{.HostConfig.NetworkMode}}":
            return c["net"]
        return c.get("raw") or json.dumps({"labels": c["labels"], "mounts": c["mounts"]})

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        done = lambda rc=0, out="": subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
        sub = cmd[1]
        if sub == "version":
            return done(out="Docker version")
        if sub == "ps" and "-a" in cmd:
            return done()  # label-keyed reuse misses: the task bucket differs
        if sub == "ps":
            assert "status=running" in cmd and not any("hermes-task-id=" in a for a in cmd)
            return done(conflict_ps_rc, "\n".join(candidates))
        if sub == "inspect" and cmd[-1] in candidates:
            return done(out=_inspect(candidates[cmd[-1]], cmd[cmd.index("--format") + 1]))
        if sub == "run":
            return done(out="fresh-cid\n")
        return done()

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    return calls


def _jail_spawn(**kwargs):
    kwargs = {"image": "python:3.11", "task_id": "profile:forge", "persistent_filesystem": True,
              "volumes": ["/srv/jail:/home/bot"], **kwargs}
    return docker_env.DockerEnvironment(**kwargs)


def _docker_runs(calls):
    return [c for c in calls if c[1] == "run"]


def test_jail_guard_adopts_when_only_task_bucket_differs(monkeypatch, tmp_path):
    """The live leak: a ``profile:forge`` spawn missed the ``default``-labeled jail and
    double-mounted /srv/jail. Identical mounts modulo the sandbox bucket must adopt it."""
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path)})

    env = _jail_spawn()

    assert "hermes-environment" in env._labels  # adoption must not require that label
    assert env._container_id == "jail-cid"
    assert not _docker_runs(calls)


def test_jail_guard_adopts_regardless_of_inspect_mount_order(monkeypatch, tmp_path):
    """``docker inspect`` lists ``.Mounts`` in map order, not argv order: a live jail with the
    skills/cache binds came back shuffled, so an argv-ordered comparison never adopted."""
    candidate = _jail_candidate(tmp_path)
    candidate["mounts"].reverse()
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": candidate})

    assert _jail_spawn()._container_id == "jail-cid"
    assert not _docker_runs(calls)


@pytest.mark.parametrize("candidate_kw, spawn_kw", [
    ({"egress": "eg123"}, {}),
    ({"extra": [("/srv/extra", "/data")]}, {}),
    # tmpfs /root cannot be proven equivalent to a bound /root, so adoption is impossible.
    ({}, {"persistent_filesystem": False}),
    ({"net": "bridge"}, {"network": False}),
    ({"image": "other:tag"}, {"image_pinned": True}),
], ids=["egress-mismatch", "extra-mount", "tmpfs-spawn", "air-gap-vs-bridge", "pinned-image-mismatch"])
def test_jail_guard_same_profile_conflicting_identity_refuses(monkeypatch, tmp_path, candidate_kw, spawn_kw):
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, **candidate_kw)})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write") as err:
        _jail_spawn(**spawn_kw)
    assert "/srv/jail" in str(err.value) and "jail-cid" in str(err.value)
    assert not _docker_runs(calls)


@pytest.mark.parametrize("candidate_kw, spawn_kw, mock_kw", [
    # A read-only second reader is a legitimate access pattern, never a conflict.
    ({"egress": "eg123"}, {"volumes": ["/srv/jail:/home/bot:ro"]}, {}),
    ({"egress": "eg123"}, {}, {"conflict_ps_rc": 125}),
    ({"egress": "eg123", "raw": "not-json"}, {}, {}),
    ({"mounts": [{"Type": "bind", "Source": "/srv/other", "Destination": "/data", "RW": True}]}, {}, {}),
], ids=["readonly-spawn", "probe-failure", "unparseable-candidate", "disjoint-jail"])
def test_jail_guard_proceeds_with_fresh_container(monkeypatch, tmp_path, candidate_kw, spawn_kw, mock_kw):
    candidate = _jail_candidate(tmp_path, egress=candidate_kw.pop("egress", "off"))
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": {**candidate, **candidate_kw}}, **mock_kw)

    env = _jail_spawn(**spawn_kw)

    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_foreign_profile_overlap_warns_and_proceeds(monkeypatch, tmp_path, caplog):
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, profile="other")})

    with caplog.at_level(logging.WARNING, logger="tools.environments.docker"):
        env = _jail_spawn()

    assert env._container_id == "fresh-cid" and _docker_runs(calls)
    assert "/srv/jail" in caplog.text


def test_jail_guard_persist_across_processes_false_skips_probe(monkeypatch, tmp_path):
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, egress="eg123")})

    env = _jail_spawn(persist_across_processes=False)

    assert not [c for c in calls if c[1] == "ps" and "status=running" in c]
    assert env._container_id == "fresh-cid"


def test_jail_guard_two_adoptables_take_first_and_warn_second(monkeypatch, tmp_path, caplog):
    candidates = {"cidA": _jail_candidate(tmp_path), "cidB": _jail_candidate(tmp_path)}
    calls = _mock_jail_guard(monkeypatch, tmp_path, candidates)

    with caplog.at_level(logging.WARNING, logger="tools.environments.docker"):
        env = _jail_spawn()

    assert env._container_id == "cidA" and not _docker_runs(calls)
    assert "cidB" in caplog.text


def test_jail_guard_unpinned_image_mismatch_adopts(monkeypatch, tmp_path, caplog):
    """Keep-existing-sandbox policy, as label reuse applies to a default-image flip."""
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, image="other:tag")})

    with caplog.at_level(logging.WARNING, logger="tools.environments.docker"):
        env = _jail_spawn(image_pinned=False)

    assert env._container_id == "jail-cid" and not _docker_runs(calls)
    assert "other:tag" in caplog.text


@pytest.mark.parametrize("theirs, ours", [
    ("rollout:one", "rollout:two"), ("session:1", "session:2"), ("rollout:one", "default")])
def test_jail_guard_per_task_buckets_coexist(monkeypatch, tmp_path, theirs, ours):
    """RL/override rollouts and per-session isolation run distinct task buckets under one profile on
    purpose: an overlap of only the (tokenized) per-task sandbox dirs neither adopts nor refuses —
    not even for a default spawn, which would otherwise run inside the rollout's container."""
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"cid": _jail_candidate(tmp_path, task=theirs, jail=None)})

    env = _jail_spawn(task_id=ours, volumes=[])

    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_non_default_real_path_still_refuses(monkeypatch, tmp_path):
    """No default side: never adoptable, but a shared real host path still refuses."""
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, task="rollout:seven")})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write"):
        _jail_spawn()
    assert not _docker_runs(calls)


def test_jail_guard_default_spawn_adopts_forge_jail(monkeypatch, tmp_path):
    """The default side may be ours: a default spawn re-joins a leaked ``profile:forge`` jail."""
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, task="profile:forge")})

    assert _jail_spawn(task_id="default")._container_id == "jail-cid"
    assert not _docker_runs(calls)


@pytest.mark.parametrize("candidate_kw, spawn_kw", [
    ({"egress": "eg123"}, {}),
    ({"image": "other:tag"}, {"image_pinned": True}),
], ids=["egress-change", "pinned-image-change"])
def test_jail_guard_default_config_drift_replaces(monkeypatch, tmp_path, candidate_kw, spawn_kw):
    """``hermes egress enable`` / a re-pinned image on a live profile: the stale default container
    is removed and recreated, as label reuse does, instead of wedging every terminal call."""
    calls = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(tmp_path, **candidate_kw)})

    env = _jail_spawn(task_id="default", **spawn_kw)

    assert ["/usr/bin/docker", "rm", "-f", "jail-cid"] in calls
    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_drift_replacement_waits_for_refusal(monkeypatch, tmp_path):
    """A leaked same-profile holder of the jail path refuses before the default one is removed:
    replacing would only double-mount the path the refusal names."""
    candidates = {"jail-cid": _jail_candidate(tmp_path, egress="eg123"),
                  "leak-cid": _jail_candidate(tmp_path, task="rollout:seven", extra=[("/srv/x", "/x")])}
    calls = _mock_jail_guard(monkeypatch, tmp_path, candidates)

    with pytest.raises(RuntimeError, match="leak-cid"):
        _jail_spawn(task_id="default")
    assert not [c for c in calls if c[1] in ("rm", "run")]


def test_jail_guard_recovery_refuses_instead_of_replacing(monkeypatch, tmp_path):
    """Exec recovery runs with labels from before the config change: removing the drifted default
    container there would delete the one its successor just recreated, and the two would trade
    removals. Recovery refuses (and fails the exec) instead."""
    candidates = {}
    calls = _mock_jail_guard(monkeypatch, tmp_path, candidates)
    env = _jail_spawn(task_id="default")
    candidates["new-cid"] = _jail_candidate(tmp_path, egress="eg123")

    assert env._recreate_container() is False
    assert not [c for c in calls if c[1] == "rm"]
    assert len(_docker_runs(calls)) == 1


def test_jail_guard_canonical_bind_source_tokenizes_sandbox_buckets(tmp_path):
    root = tmp_path / "sandboxes" / "docker"
    home = root / "profile_forge-abc" / "home"
    default_home = root / "default" / "home"
    home.mkdir(parents=True)
    default_home.mkdir(parents=True)
    resolved_root = os.path.realpath(root)
    canonical = docker_env._canonical_bind_source

    assert canonical(str(home), resolved_root) == "<sandbox>/home"
    assert canonical(f"{home}/", resolved_root) == "<sandbox>/home"
    assert canonical(str(default_home), resolved_root) == canonical(str(home), resolved_root)
    assert canonical("/nonexistent/jail/", resolved_root) == "/nonexistent/jail"
    assert canonical("rel/dir/", resolved_root) == "rel/dir"


@pytest.mark.require_symlinks
def test_jail_guard_canonical_bind_source_resolves_symlinks(tmp_path):
    root = tmp_path / "sandboxes" / "docker"
    (root / "default" / "home").mkdir(parents=True)
    link = tmp_path / "home-link"
    link.symlink_to(root / "default" / "home")

    assert docker_env._canonical_bind_source(str(link), os.path.realpath(root)) == "<sandbox>/home"


def test_jail_guard_parse_mount_pair_args():
    binds, has_tmpfs = docker_env._parse_mount_pair_args([
        "-v", "/a:/x", "-v", "/b:/y:ro,z", "--mount", "type=bind,source=/c,destination=/z,readonly",
        "--mount", "type=bind,src=/d,dst=/w", "--mount", "type=volume,source=named,target=/v",
        "--tmpfs", "/tmp:rw,nosuid,size=512m", "--tmpfs", "/run:rw,noexec,nosuid,size=64m"])

    assert binds == [("/a", "/x", True), ("/b", "/y", False), ("/c", "/z", False), ("/d", "/w", True)]
    assert has_tmpfs is False  # hardening tmpfs every hermes container carries
    assert docker_env._parse_mount_pair_args(["--tmpfs", "/root:rw,exec,size=1g"])[1] is True
    assert docker_env._parse_mount_pair_args(["--mount", "type=tmpfs,destination=/root"])[1] is True
