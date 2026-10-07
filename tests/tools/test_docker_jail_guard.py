"""Jail duplicate-mount guard (t_44fdd37a): a spawn whose task bucket misses label reuse must
adopt, refuse, or proceed based on the host paths running hermes containers already mount."""
import json
import logging
import os
import subprocess

import pytest

from tools.environments import base as env_base
from tools.environments import docker as docker_env
from tools.environments.path_utils import sanitize_task_id_for_path


# --- Jail duplicate-mount guard (t_44fdd37a) ---

def _jail_candidate(sandbox, *, profile="bot_1", egress="off", image="python:3.11", net="default",
                    task="default", extra=()):
    """Inspect answers for a running persistent jail held under *task*'s sandbox bucket."""
    bucket = sandbox / "docker" / sanitize_task_id_for_path(task)
    for sub in ("home", "workspace"):
        (bucket / sub).mkdir(parents=True, exist_ok=True)
    binds = [(str(bucket / "home"), "/root"), (str(bucket / "workspace"), "/workspace"),
             ("/srv/jail", "/home/bot"), *extra]
    return {
        "labels": {"hermes-agent": "1", "hermes-profile": profile, "hermes-task-id": task,
                   "hermes-egress": egress},
        "mounts": [{"Type": "bind", "Source": s, "Destination": d, "RW": True} for s, d in binds],
        "image": image, "net": net}


def _mock_jail_guard(monkeypatch, tmp_path, candidates, *, conflict_ps_rc=0):
    """Label-reuse probe always misses; the conflict probe lists *candidates* (cid -> answers).
    Returns (calls, sandbox root)."""
    sandbox = tmp_path / "sandboxes"
    sandbox.mkdir(exist_ok=True)
    monkeypatch.setattr(env_base, "get_sandbox_dir", lambda: sandbox)
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_get_active_profile_name", lambda: "bot_1")
    monkeypatch.setattr(docker_env, "_readonly_skill_mount_args", lambda: [])
    docker_env._cgroup_limits_ok = True
    calls = []
    formats = {"{{json .Config.Labels}}": lambda c: json.dumps(c["labels"]),
               "{{json .Mounts}}": lambda c: c["mounts"] if isinstance(c["mounts"], str) else json.dumps(c["mounts"]),
               "{{.Config.Image}}": lambda c: c["image"],
               "{{.HostConfig.NetworkMode}}": lambda c: c["net"]}

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
            return done(out=formats[cmd[cmd.index("--format") + 1]](candidates[cmd[-1]]))
        if sub == "run":
            return done(out="fresh-cid\n")
        return done()

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    return calls, sandbox


def _jail_spawn(**kwargs):
    kwargs = {"image": "python:3.11", "task_id": "profile:forge", "persistent_filesystem": True,
              "volumes": ["/srv/jail:/home/bot"], **kwargs}
    return docker_env.DockerEnvironment(**kwargs)


def _docker_runs(calls):
    return [c for c in calls if c[1] == "run"]


def test_jail_guard_adopts_when_only_task_bucket_differs(monkeypatch, tmp_path):
    """The live leak: a ``profile:forge`` spawn missed the ``default``-labeled jail and
    double-mounted /srv/jail. Identical mounts modulo the sandbox bucket must adopt it."""
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox)})

    env = _jail_spawn()

    assert "hermes-environment" in env._labels  # adoption must not require that label
    assert env._container_id == "jail-cid"
    assert not _docker_runs(calls)


def test_jail_guard_adopts_regardless_of_inspect_mount_order(monkeypatch, tmp_path):
    """``docker inspect`` lists ``.Mounts`` in map order, not argv order: a live jail with the
    skills/cache binds came back shuffled, so an argv-ordered comparison never adopted."""
    sandbox = tmp_path / "sandboxes"
    candidate = _jail_candidate(sandbox)
    candidate["mounts"].reverse()
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": candidate})

    assert _jail_spawn()._container_id == "jail-cid"
    assert not _docker_runs(calls)


def test_jail_guard_same_profile_egress_mismatch_refuses(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, egress="eg123")})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write") as err:
        _jail_spawn()
    assert "/srv/jail" in str(err.value) and "jail-cid" in str(err.value)
    assert not _docker_runs(calls)


def test_jail_guard_same_profile_extra_mount_refuses(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    candidate = _jail_candidate(sandbox, extra=[("/srv/extra", "/data")])
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": candidate})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write"):
        _jail_spawn()
    assert not _docker_runs(calls)


def test_jail_guard_foreign_profile_overlap_warns_and_proceeds(monkeypatch, tmp_path, caplog):
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, profile="other")})

    with caplog.at_level(logging.WARNING, logger="tools.environments.docker"):
        env = _jail_spawn()

    assert env._container_id == "fresh-cid" and _docker_runs(calls)
    assert "/srv/jail" in caplog.text


def test_jail_guard_readonly_spawn_vs_rw_jail_proceeds(monkeypatch, tmp_path):
    """A read-only second reader is a legitimate access pattern, never a conflict."""
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, egress="eg123")})

    env = _jail_spawn(volumes=["/srv/jail:/home/bot:ro"])

    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_persist_across_processes_false_skips_probe(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, egress="eg123")})

    env = _jail_spawn(persist_across_processes=False)

    assert not [c for c in calls if c[1] == "ps" and "status=running" in c]
    assert env._container_id == "fresh-cid"


def test_jail_guard_probe_failure_falls_back(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(
        monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, egress="eg123")}, conflict_ps_rc=125)

    env = _jail_spawn()

    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_unparseable_candidate_skipped(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    candidate = dict(_jail_candidate(sandbox, egress="eg123"), mounts="not-json")
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": candidate})

    env = _jail_spawn()

    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_disjoint_running_jail_proceeds(monkeypatch, tmp_path):
    candidate = {"labels": {"hermes-agent": "1", "hermes-profile": "bot_1", "hermes-task-id": "default",
                            "hermes-egress": "off"},
                 "mounts": [{"Type": "bind", "Source": "/srv/other", "Destination": "/data", "RW": True}],
                 "image": "python:3.11", "net": "default"}
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": candidate})

    env = _jail_spawn()

    assert env._container_id == "fresh-cid" and _docker_runs(calls)


def test_jail_guard_tmpfs_spawn_vs_persistent_jail_refuses(monkeypatch, tmp_path):
    """tmpfs /root cannot be proven equivalent to a bound /root, so adoption is impossible."""
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox)})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write"):
        _jail_spawn(persistent_filesystem=False)
    assert not _docker_runs(calls)


def test_jail_guard_two_adoptables_take_first_and_warn_second(monkeypatch, tmp_path, caplog):
    sandbox = tmp_path / "sandboxes"
    candidates = {"cidA": _jail_candidate(sandbox), "cidB": _jail_candidate(sandbox)}
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, candidates)

    with caplog.at_level(logging.WARNING, logger="tools.environments.docker"):
        env = _jail_spawn()

    assert env._container_id == "cidA" and not _docker_runs(calls)
    assert "cidB" in caplog.text


def test_jail_guard_air_gap_vs_bridge_candidate_refuses(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, net="bridge")})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write"):
        _jail_spawn(network=False)
    assert not _docker_runs(calls)


def test_jail_guard_pinned_image_mismatch_refuses(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, image="other:tag")})

    with pytest.raises(RuntimeError, match="already bind-mounted read-write"):
        _jail_spawn(image_pinned=True)
    assert not _docker_runs(calls)


def test_jail_guard_unpinned_image_mismatch_adopts(monkeypatch, tmp_path, caplog):
    """Keep-existing-sandbox policy, as label reuse applies to a default-image flip."""
    sandbox = tmp_path / "sandboxes"
    calls, _ = _mock_jail_guard(monkeypatch, tmp_path, {"jail-cid": _jail_candidate(sandbox, image="other:tag")})

    with caplog.at_level(logging.WARNING, logger="tools.environments.docker"):
        env = _jail_spawn(image_pinned=False)

    assert env._container_id == "jail-cid" and not _docker_runs(calls)
    assert "other:tag" in caplog.text


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
    assert canonical(str(home), None) == os.path.realpath(home)


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
