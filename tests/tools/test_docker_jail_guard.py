"""One identity per sandbox container (t_44fdd37a): the reuse fingerprint names the container, so
reuse, the cold-start race, recovery and config drift all resolve through ``docker run --name`` and
duplicates cannot arise; the one refusal left is another identity holding a real host path RW."""
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from tools.environments import docker as docker_env

_FINISHED_LONG_AGO = "2000-01-01T00:00:00.000000000Z"


@pytest.fixture(autouse=True)
def _stable_tempdir(monkeypatch, tmp_path):
    """pytest's tmp_path sits under the system tempdir, whose mounts count as volatile: that would
    hide every jail path and sandbox dir from the fingerprint and the refusal probe."""
    (tmp_path / "proc-tmp").mkdir()
    monkeypatch.setattr(docker_env.tempfile, "gettempdir", lambda: str(tmp_path / "proc-tmp"))


class _FakeDaemon:
    """In-memory Docker daemon: container names are unique, ``ps`` honours label/status filters,
    and run/rename/start/stop/rm/exec behave as the real CLI does for the calls hermes makes."""

    def __init__(self, monkeypatch, tmp_path):
        self.containers: dict[str, dict] = {}
        self.create_window: float = 0.0  # name reserved, container not yet inspectable (daemon create)
        self.reserved: dict[str, str] = {}
        self.timers: list[threading.Timer] = []
        self.calls: list[list[str]] = []
        self.run_barrier: threading.Barrier | None = None
        self.start_error = ""
        self._lock = threading.Lock()
        monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))
        monkeypatch.setattr(docker_env.DockerEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
        monkeypatch.setattr(docker_env, "_get_active_profile_name", lambda: "bot_1")
        monkeypatch.setattr(docker_env, "_readonly_skill_mount_args", lambda: [])
        monkeypatch.setattr(docker_env, "_cgroup_limits_ok", True)
        monkeypatch.setattr(docker_env.subprocess, "run", self)
        monkeypatch.setattr(docker_env, "_popen_bash", self._exec)

    def add(self, name, labels, *, binds=(), image="python:3.11") -> str:
        cid = uuid.uuid4().hex + uuid.uuid4().hex
        self.containers[cid] = {
            "name": name, "labels": dict(labels), "state": "running", "image": image,
            "finished": "0001-01-01T00:00:00Z",
            "mounts": [{"Type": "bind" if src.startswith("/") else "volume", "Source": src,
                        "Destination": dst, "RW": rw} for src, dst, rw in binds]}
        return cid

    def find(self, ref):
        return next((cid for cid, c in self.containers.items() if ref in (c["name"], cid[:len(ref)])), None)

    def stop(self, cid):
        self.containers[cid].update(state="exited", finished=_FINISHED_LONG_AGO)

    def _create(self, name, cid, container):
        with self._lock:
            del self.reserved[name]
            self.containers[cid] = container

    def settle(self):
        """Wait out every pending create window."""
        for timer in self.timers:
            timer.join()

    def subcommands(self, *subs):
        return [c for c in self.calls if c[1] in subs]

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        if cmd[1] == "run" and self.run_barrier is not None:
            self.run_barrier.wait(timeout=10)  # every racer has probed before anyone runs
        with self._lock:
            self.calls.append(cmd)
            rc, out, err = self._answer(cmd)
        if kwargs.get("check") and rc:
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def _answer(self, cmd):
        sub = cmd[1]
        ref = cmd[2] if sub == "rename" else cmd[-1]
        if sub in ("version", "image"):
            return 0, "null", ""
        if sub == "ps":
            labels = dict(f.removeprefix("label=").split("=", 1) for f in _flag_values(cmd, "--filter")
                          if f.startswith("label="))
            states = {f.removeprefix("status=") for f in _flag_values(cmd, "--filter") if f.startswith("status=")}
            hits = [(cid, c) for cid, c in self.containers.items()
                    if ("-a" in cmd or c["state"] == "running") and (not states or c["state"] in states)
                    and labels.items() <= c["labels"].items()]
            with_state = "{{.State}}" in cmd[cmd.index("--format") + 1]
            return 0, "".join(f"{cid}\t{c['state']}\n" if with_state else f"{cid}\n" for cid, c in hits), ""
        if sub == "run":
            name = cmd[cmd.index("--name") + 1]
            if (holder := self.find(name) or self.reserved.get(name)) is not None:
                return 125, "", (f'docker: Error response from daemon: Conflict. The container name "/{name}" '
                                 f'is already in use by container "{holder}".')
            labels = dict(label.split("=", 1) for label in _flag_values(cmd, "--label"))
            specs = [spec.split(":") for spec in _flag_values(cmd, "-v")]
            binds = [(spec[0], spec[1], spec[2:] != ["ro"]) for spec in specs]
            cid = self.add(name, labels, binds=binds, image=cmd[-3])
            if self.create_window > 0:
                self.reserved[name] = cid
                timer = threading.Timer(self.create_window, self._create, (name, cid, self.containers.pop(cid)))
                self.timers.append(timer)
                timer.start()
            return 0, cid + "\n", ""
        if sub == "inspect" and cmd[2:4] == ["--type", "container"]:  # one JSON line per container found
            found = [cid for cid in map(self.find, cmd[cmd.index("--format") + 2:]) if cid is not None]
            out = "".join(json.dumps({"id": cid, **{k: self.containers[cid][k] for k in ("state", "labels", "mounts")}})
                          + "\n" for cid in found)
            return (0 if len(found) == len(cmd) - cmd.index("--format") - 2 else 1), out, ""
        cid = self.find(ref)
        if cid is None:
            return 1, "", f"Error response from daemon: No such container: {ref}"
        c = self.containers[cid]
        if sub == "inspect":
            fmt = cmd[cmd.index("--format") + 1]
            if fmt == "{{.State.FinishedAt}}":
                return 0, c["finished"] + "\n", ""
            assert fmt == "{{.Config.Image}}", fmt
            return 0, c["image"] + "\n", ""
        if sub == "rename":
            if self.find(cmd[3]) is not None:
                return 1, "", f'Error response from daemon: Conflict. The container name "/{cmd[3]}" is already in use'
            c["name"] = cmd[3]
        elif sub == "start":
            if self.start_error:
                return 1, "", self.start_error
            c["state"] = "running"
        elif sub == "rm":
            if c["state"] == "running" and "-f" not in cmd:
                return 1, "", "Error response from daemon: cannot remove container: container is running"
            del self.containers[cid]
        return 0, "", ""

    def _exec(self, cmd, stdin_data=None, **kwargs):
        self.calls.append(list(cmd))
        cid = self.find(cmd[cmd.index("bash") - 1])
        alive = cid is not None and self.containers[cid]["state"] == "running"
        script = "print('alive')" if alive else (
            "print('Error response from daemon: No such container')\nraise SystemExit(1)")
        return _real_popen_bash([sys.executable, "-c", script], stdin_data)


_real_popen_bash = docker_env._popen_bash


def _flag_values(cmd, flag):
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == flag]


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    return _FakeDaemon(monkeypatch, tmp_path)


def _spawn(**kwargs):
    kwargs = {"image": "python:3.11", "task_id": "default", "persistent_filesystem": True, "volumes": [], **kwargs}
    return docker_env.DockerEnvironment(**kwargs)


def _jail(tmp_path):
    path = tmp_path / "jail"
    path.mkdir()
    return str(path)


def _foreign_labels(**overrides):
    return {"hermes-agent": "1", "hermes-profile": "bot_1", "hermes-task-id": "default",
            "hermes-egress": "off", "hermes-environment": "f" * 24, **overrides}


# --- identity -------------------------------------------------------------------------------

_BASE_CONFIG = {"image": "python:3.11", "mount_args": ["-v", "/srv/jail:/home/bot"],
                "hermes_home": "/profiles/alpha", "egress": "off"}
_CHANGED_CONFIG = {"image": "python:3.12", "mount_args": ["-v", "/srv/jail:/home/bot", "-v", "data:/data"],
                   "hermes_home": "/profiles/beta", "egress": "0123456789abcdef01234567"}


@pytest.mark.parametrize("field", sorted(_CHANGED_CONFIG))
def test_fingerprint_is_the_single_identity_source(field):
    """Every jail-relevant input moves both the fingerprint and the name derived from it; an equal
    configuration lands on one name."""
    fingerprint = docker_env._reuse_environment_fingerprint(**_BASE_CONFIG)
    changed = docker_env._reuse_environment_fingerprint(**{**_BASE_CONFIG, field: _CHANGED_CONFIG[field]})

    assert fingerprint == docker_env._reuse_environment_fingerprint(**dict(_BASE_CONFIG))
    assert changed != fingerprint
    assert docker_env._canonical_container_name(changed) != docker_env._canonical_container_name(fingerprint)


def test_fingerprint_name_is_stable_across_processes():
    """Two processes of one configuration must derive the same name: no per-process entropy
    (hash seed, dict order) may reach the hash."""
    code = ("from tools.environments import docker as d; import json, sys; "
            "print(d._canonical_container_name(d._reuse_environment_fingerprint(**json.loads(sys.argv[1]))))")
    names = {subprocess.run([sys.executable, "-c", code, json.dumps(_BASE_CONFIG)], capture_output=True, text=True,
                            check=True, cwd=Path(__file__).parents[2], env={**os.environ, "PYTHONHASHSEED": seed},
                            stdin=subprocess.DEVNULL).stdout.strip() for seed in ("1", "2")}

    assert names == {docker_env._canonical_container_name(docker_env._reuse_environment_fingerprint(**_BASE_CONFIG))}


def test_volatile_tempdir_mount_source_keeps_the_name(tmp_path):
    """The symlink-safe skills copy is a fresh mkdtemp per process: its source must not move the
    name, or every process would spawn its own container. Where it lands still does."""
    proc_tmp = tmp_path / "proc-tmp"

    def name(source, dest="/root/.hermes/skills"):
        return docker_env._canonical_container_name(docker_env._reuse_environment_fingerprint(
            **{**_BASE_CONFIG, "mount_args": ["-v", f"{source}:{dest}:ro"]}))

    assert name(proc_tmp / "hermes-skills-safe-a1b2") == name(proc_tmp / "hermes-skills-safe-c3d4")
    assert name(proc_tmp / "hermes-skills-safe-a1b2") != name(proc_tmp / "hermes-skills-safe-a1b2", "/skills")


def test_tmpfs_sandboxes_of_distinct_tasks_get_distinct_names(daemon):
    """A tmpfs sandbox has no host path carrying its task bucket, so the bucket is hashed in —
    injectively: ``a:b`` and ``a_b`` share a label value but must not share a container."""
    one = _spawn(task_id="rollout:one", persistent_filesystem=False)
    two = _spawn(task_id="rollout_one", persistent_filesystem=False)

    assert one._name != two._name and one._container_id != two._container_id
    assert _spawn(task_id="rollout:one", persistent_filesystem=False)._container_id == one._container_id


def test_session_scoped_container_takes_no_shared_identity(daemon):
    """Without cross-process persistence the container is the session's alone: a unique name, no
    lookup, and no probe of other containers."""
    first = _spawn(persist_across_processes=False)
    second = _spawn(persist_across_processes=False)

    assert first._name != second._name
    assert first._labels["hermes-environment"] == second._labels["hermes-environment"]
    assert not daemon.subcommands("inspect", "ps")


# --- spawn ----------------------------------------------------------------------------------

def test_spawn_runs_under_the_canonical_name_with_every_label(daemon):
    env = _spawn()

    container = daemon.containers[daemon.find(env._name)]
    assert env._name == docker_env._canonical_container_name(env._labels["hermes-environment"])
    assert container["labels"] == env._labels
    assert container["labels"].keys() == {
        "hermes-agent", "hermes-task-id", "hermes-profile", "hermes-egress", "hermes-environment"}


def test_respawn_attaches_by_name_and_starts_a_stopped_container(daemon):
    first = _spawn()
    daemon.stop(first._container_id)

    second = _spawn()

    assert second._container_id == first._container_id
    assert daemon.containers[first._container_id]["state"] == "running"
    assert len(daemon.subcommands("run")) == 1


def test_cold_start_race_converges_on_one_container(daemon):
    """Two processes of one configuration probe before either runs: one ``docker run`` wins the
    name, the loser's fails "already in use" and it attaches to the winner's container."""
    daemon.run_barrier = threading.Barrier(2)
    envs, errors = [], []

    def spawn():
        try:
            envs.append(_spawn())
        except Exception as e:  # surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=spawn) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert len(daemon.containers) == 1
    assert {env._container_id for env in envs} == set(daemon.containers)
    assert len(daemon.subcommands("run")) == 2


def test_cold_start_race_attaches_through_the_create_window(daemon):
    """The daemon reserves the name before the winner's container is inspectable: the loser waits
    for it and attaches, and never removes by name — a plain ``rm`` would take the winner's
    still-created container (live scenario H, docker 29.1.3)."""
    daemon.run_barrier = threading.Barrier(2)
    daemon.create_window = 0.75
    envs, errors = [], []

    def spawn():
        try:
            envs.append(_spawn())
        except Exception as e:  # surfaced by the assertion below
            errors.append(e)

    threads = [threading.Thread(target=spawn) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    daemon.settle()

    assert not errors
    assert len(daemon.containers) == 1
    assert {env._container_id for env in envs} == set(daemon.containers)
    assert len(daemon.subcommands("run")) == 2
    assert not daemon.subcommands("rm")


def test_conflict_holder_that_never_appears_raises_without_removing(daemon, monkeypatch):
    """A name holder that stays uninspectable past the wait is a loud failure, never a removal."""
    monkeypatch.setattr(docker_env, "_CONFLICT_ATTACH_TIMEOUT", 0.5)
    daemon.create_window = 3.0
    winner = _spawn()

    with pytest.raises(RuntimeError, match=f"{winner._name}.*never became inspectable"):
        _spawn()
    assert not daemon.subcommands("rm")

    daemon.settle()
    assert daemon.containers[winner._container_id]["state"] == "running"


def test_name_holder_of_another_identity_refuses(daemon):
    """The name is derived from the fingerprint, so a holder labeled otherwise is not ours."""
    probe = _spawn()
    daemon.containers.clear()
    daemon.add(probe._name, _foreign_labels())
    daemon.calls.clear()

    with pytest.raises(RuntimeError, match=f"{probe._name}.*{'f' * 24}"):
        _spawn()
    assert not daemon.subcommands("run", "rename", "rm")


def test_failed_start_of_the_named_container_raises(daemon):
    """The name is taken, so there is no fresh container to fall back to: fail loudly."""
    first = _spawn()
    daemon.stop(first._container_id)
    daemon.start_error = "Error response from daemon: mount source missing"

    with pytest.raises(RuntimeError, match=f"{first._name}.*mount source missing"):
        _spawn()


# --- drift ----------------------------------------------------------------------------------

def test_drift_runs_a_new_name_and_leaves_the_stale_container_to_the_reaper(daemon):
    """A named volume added to the config is a new fingerprint, so a new name: the spawn never
    removes or adopts the stale container, which the orphan reaper takes once it has exited."""
    old = _spawn()
    new = _spawn(volumes=["gvol:/data"])

    assert new._name != old._name and new._container_id != old._container_id
    assert daemon.containers[old._container_id]["state"] == "running"
    assert not daemon.subcommands("rm", "stop")

    daemon.stop(old._container_id)
    removed = docker_env.reap_orphan_containers(max_age_seconds=60, profile_filter="bot_1")
    assert removed == 1 and set(daemon.containers) == {new._container_id}


# --- refusal --------------------------------------------------------------------------------

@pytest.mark.parametrize("holder_labels", [
    {"hermes-task-id": "profile_forge"},  # another task bucket of the profile: the live leak
    {"hermes-profile": "bot_2"},  # another profile
    {},  # the same bucket under drifted configuration
], ids=["other-bucket", "other-profile", "drifted"])
def test_another_identity_holding_a_real_rw_path_refuses(daemon, tmp_path, holder_labels):
    jail = _jail(tmp_path)
    labels = _foreign_labels(**holder_labels)
    holder = daemon.add("hermes-holder", labels, binds=[(jail, "/home/bot", True)])

    with pytest.raises(RuntimeError, match=f"{jail}.*{holder[:12]}") as refused:
        _spawn(volumes=[f"{jail}:/home/bot"])
    assert f"task={labels['hermes-task-id']!r}, profile={labels['hermes-profile']!r}" in str(refused.value)
    assert not daemon.subcommands("run")


@pytest.mark.parametrize("holder_rw, ours", [(False, ":/home/bot"), (True, ":/home/bot:ro")], ids=["theirs-ro", "ours-ro"])
def test_read_only_side_never_conflicts(daemon, tmp_path, holder_rw, ours):
    jail = _jail(tmp_path)
    daemon.add("hermes-holder", _foreign_labels(), binds=[(jail, "/home/bot", holder_rw)])

    _spawn(volumes=[jail + ours])

    assert len(daemon.subcommands("run")) == 1


def test_stopped_container_start_is_refused_while_another_identity_holds_its_path(daemon, tmp_path):
    """Daemon-restart ordering: a forge spawn brought the jail path up first; restarting the
    default jail beside it would double-mount the path."""
    jail = _jail(tmp_path)
    ours = _spawn(volumes=[f"{jail}:/home/bot"])
    daemon.stop(ours._container_id)
    daemon.add("hermes-forge", _foreign_labels(**{"hermes-task-id": "profile_forge"}), binds=[(jail, "/home/bot", True)])

    with pytest.raises(RuntimeError, match="already bind-mounted read-write"):
        _spawn(volumes=[f"{jail}:/home/bot"])
    assert daemon.containers[ours._container_id]["state"] == "exited"


# --- recovery -------------------------------------------------------------------------------

def test_recovery_starts_the_named_container(daemon):
    env = _spawn()
    daemon.stop(env._container_id)

    result = env.execute("echo alive")

    assert result["returncode"] == 0 and "alive" in result["output"]
    assert len(daemon.subcommands("run")) == 1


def test_recovery_runs_under_the_canonical_name_when_the_container_is_gone(daemon):
    env = _spawn()
    del daemon.containers[env._container_id]

    result = env.execute("echo alive")

    assert result["returncode"] == 0
    assert daemon.find(env._name) == env._container_id
    assert [_flag_values(c, "--name") for c in daemon.subcommands("run")] == [[env._name], [env._name]]


def test_jail_guard_refused_recovery_retries_on_next_exec(daemon, tmp_path):
    """A refused recovery keeps the gone id: the next exec fails as container-gone and recovers once
    the conflicting holder is gone (a ``None`` id would assert "Container not started" forever)."""
    jail = _jail(tmp_path)
    env = _spawn(volumes=[f"{jail}:/home/bot"])
    gone = env._container_id
    del daemon.containers[gone]
    holder = daemon.add("hermes-holder", _foreign_labels(), binds=[(jail, "/home/bot", True)])

    refused = env.execute("echo alive")
    assert refused["returncode"] != 0 and env._container_id == gone

    del daemon.containers[holder]
    recovered = env.execute("echo alive")
    assert recovered["returncode"] == 0 and "alive" in recovered["output"]
    assert env._container_id == daemon.find(env._name)


# --- legacy containers ----------------------------------------------------------------------

def test_legacy_container_is_adopted_by_label_and_renamed(daemon):
    """A container from before canonical names (random name, fingerprint label without the egress
    input) is found by the labels its creator used and renamed to the canonical name; every label
    stays, so label-keyed tooling still resolves it."""
    probe = _spawn()
    labels = {**probe._labels, "hermes-environment": probe._legacy_fingerprint}
    daemon.containers.clear()
    legacy = daemon.add("hermes-1a2b3c4d", labels)
    daemon.calls.clear()

    env = _spawn()

    assert env._container_id == legacy
    assert daemon.containers[legacy]["name"] == env._name
    assert daemon.containers[legacy]["labels"] == labels
    assert not daemon.subcommands("run")
    by_label = daemon(["docker", "ps", "-a", "--filter", "label=hermes-profile=bot_1",
                       "--filter", "label=hermes-task-id=default", "--format", "{{.ID}}"])
    assert by_label.stdout.split() == [legacy]
    # The renamed holder carries the legacy label: the next process attaches by name, no refusal.
    assert _spawn()._container_id == legacy


# --- mount parsing --------------------------------------------------------------------------

def test_parse_bind_args():
    binds = docker_env._parse_bind_args([
        "-v", "/a:/x", "-v", "/b:/y:ro,z", "--mount", "type=bind,source=/c,destination=/z,readonly",
        "--mount", "type=bind,src=/d,dst=/w", "--mount", "type=volume,source=named,target=/v",
        "--tmpfs", "/tmp:rw,nosuid,size=512m"])

    assert binds == [("/a", "/x", True), ("/b", "/y", False), ("/c", "/z", False), ("/d", "/w", True)]


def test_parse_bind_args_joined_flags_and_named_volumes():
    parse = docker_env._parse_bind_args

    assert parse(["--volume=/a:/x", "--mount=type=bind,source=/b,target=/y,ro"]) == parse(
        ["--volume", "/a:/x", "--mount", "type=bind,source=/b,target=/y,ro"])
    assert parse(["-v/a:/x"]) == parse(["-v=/a:/x"]) == parse(["-v", "/a:/x"]) == [("/a", "/x", True)]
    assert parse(["-v", "named:/x", "-v", "./rel:/y"]) == [("./rel", "/y", True)]


@pytest.mark.require_symlinks
def test_shared_rw_sources_drop_sandbox_tempdir_and_read_only(tmp_path):
    root = tmp_path / "sandboxes" / "docker"
    (root / "default" / "home").mkdir(parents=True)
    jail = Path(_jail(tmp_path))
    (tmp_path / "jail-link").symlink_to(jail)
    binds = [(str(root / "default" / "home"), "/root", True), (str(tmp_path / "proc-tmp" / "skills"), "/s", True),
             (str(tmp_path / "jail-link"), "/home/bot", True), ("/srv/ro", "/ro", False)]

    assert docker_env._shared_rw_sources(binds, os.path.realpath(root)) == {os.path.realpath(jail)}
