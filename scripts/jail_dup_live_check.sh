#!/usr/bin/env bash
# Live-daemon repro for duplicate jail mounts (t_44fdd37a). Touches only containers it created
# that mount a path under its own temp root. Exit: 0 pass, 1 fail, 77 skip.
#   A: a spawn under another task bucket adopts the running default jail.
#   B: a conflicting same-profile spawn sharing the jail path is refused.
#   C: the reverse leak order (forge first, then default) converges on one container.
#   D: per-task buckets sharing only sandbox dirs coexist (D1), a non-default spawn sharing a
#      real jail path is still refused (D2), and a default spawn whose volumes diverge from the
#      running default jail is refused rather than replacing it (D3).
#   E: daemon-restart ordering — with the default jail stopped, forge comes up first; the default
#      spawn's stopped by-label hit must adopt the running forge container, never start beside it.
#   E2: exec recovery inside a live process restarts its own (adopted, foreign-labeled) container.
set -u
cd "$(dirname "$0")/.."
PY=${HERMES_PYTHON:-.venv/bin/python}
export IMAGE=bash:5  # alpine + bash: execute() runs every command through bash (E2)

skip() { echo "SKIP: $*"; exit 77; }
fail() { echo "FAIL: $*"; exit 1; }

docker info >/dev/null 2>&1 || skip "no daemon/image"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null 2>&1 \
  || skip "no daemon/image"

T=$(realpath "$(mktemp -d)")
A=$T/jailA B=$T/jailB C=$T/jailC E=$T/jailE
mkdir -p "$A" "$B" "$C" "$E" "$T/tmp"
docker ps -aq --no-trunc --filter label=hermes-agent=1 >"$T/pre.ids"
export CREATED_IDS=$T/created.ids
: >"$CREATED_IDS"

cleanup() {
  local rc=$? id
  while read -r id; do
    grep -q "^$id" "$T/pre.ids" && continue  # adopted ids are the short form
    docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$id" 2>/dev/null \
      | grep -q "^$T/" && docker rm -f "$id" >/dev/null
  done <"$CREATED_IDS"
  # The containers ran as root and wrote into their bind-mounted sandbox dirs.
  rm -rf "$T" 2>/dev/null || { docker run --rm -v "$T:/t" "$IMAGE" find /t -mindepth 1 -delete; rm -rf "$T"; }
  exit "$rc"
}
trap cleanup EXIT

export HERMES_HOME=$T/home TERMINAL_SANDBOX_DIR=$T/sandboxes
# Python's tempdir must not contain the jails: process-tempdir mounts are volatile by design
# and carry no jail identity, which would hide every mount under $T from the guard.
export TMPDIR=$T/tmp

# spawn <task_id> <volume>...: one process per spawn (the cross-process case), printing the
# container id or "REFUSED <message>". The id is recorded at once so a later crash cannot leak it.
spawn() {
  "$PY" -c '
import os, sys
from tools.environments.docker import DockerEnvironment
try:
    env = DockerEnvironment(image=os.environ["IMAGE"], cwd="/", task_id=sys.argv[1],
                            volumes=sys.argv[2:], persistent_filesystem=True)
except RuntimeError as e:
    print("REFUSED", e)
else:
    with open(os.environ["CREATED_IDS"], "a") as f:
        f.write(env._container_id + "\n")
    print(env._container_id)' "$@"
}

# Adoption keeps the short id `docker ps` reports; a fresh `docker run` returns the long one.
same_container() { [ -n "$1" ] && [ "${1:0:12}" = "${2:0:12}" ]; }

holders() {  # running hermes containers bind-mounting $1
  local n=0 id
  for id in $(docker ps -q --filter label=hermes-agent=1 --filter status=running); do
    docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$id" | grep -qxF "$1" && n=$((n + 1))
  done
  echo "$n"
}

assert_single_jail() {
  local jail n
  for jail in "$@"; do
    n=$(holders "$jail")
    [ "$n" = 1 ] || fail "$n running containers mount $jail (want 1)"
  done
  echo "PASS single-jail-invariant"
}

# A: the live leak shape — "default" holds the jail, a "profile:forge" spawn must adopt it.
id1=$(spawn default "$A:/home/bot") || fail "scenario A default spawn crashed"
id2=$(spawn profile:forge "$A:/home/bot") || fail "scenario A forge spawn crashed"
same_container "$id1" "$id2" || fail "profile:forge got $id2, not the default jail $id1"
echo "PASS adopt-across-buckets"
assert_single_jail "$A"

# B: same profile, mounts differ beyond the sandbox bucket while sharing jail A read-write.
out=$(spawn second:bucket "$A:/home/bot" "$B:/extra") || fail "scenario B spawn crashed: $out"
case "$out" in
  "REFUSED "*"already bind-mounted read-write"*) echo "PASS refuse-on-conflicting-identity" ;;
  *) fail "expected a refusal, got: $out" ;;
esac
[ "$(holders "$B")" = 0 ] || fail "refused spawn left a container on $B"
assert_single_jail "$A"

# C: reverse order in a fresh sandbox root — forge creates first, default must adopt it.
export TERMINAL_SANDBOX_DIR=$T/sbxT
idF=$(spawn profile:forge "$C:/home/bot") || fail "scenario C forge spawn crashed"
idD=$(spawn default "$C:/home/bot") || fail "scenario C default spawn crashed"
same_container "$idF" "$idD" || fail "default got $idD, not the forge jail $idF"
echo "PASS selfheal-default-adopts-leaked-forge-jail"
assert_single_jail "$A" "$C"

# D1: rollouts under one profile, no volumes — only per-task sandbox dirs, which coexist.
export TERMINAL_SANDBOX_DIR=$T/sbxT2
id3=$(spawn rollout:one) || fail "scenario D1 rollout:one spawn crashed"
id4=$(spawn rollout:two) || fail "scenario D1 rollout:two spawn crashed"
[ "${#id3}" = 64 ] && [ "${#id4}" = 64 ] && [ "$id3" != "$id4" ] \
  || fail "rollouts must get fresh containers of their own, got $id3 / $id4"
for id in "$id3" "$id4"; do
  [ "$(docker inspect --format '{{.State.Running}}' "$id")" = true ] || fail "rollout container $id not running"
done
echo "PASS rollout-isolation"

# D2: a non-default bucket sharing jail A's real path is refused (A's default side is theirs).
# Under A's sandbox root, as one profile's processes are: the jail shape is recognised by the
# candidate's sandbox binds, which a spawn only sees as such under its own root.
export TERMINAL_SANDBOX_DIR=$T/sandboxes
out=$(spawn rollout:three "$A:/home/bot" "$B:/extra") || fail "scenario D2 spawn crashed: $out"
case "$out" in
  "REFUSED "*"already bind-mounted read-write"*) echo "PASS refuse-non-default-side" ;;
  *) fail "expected a refusal, got: $out" ;;
esac
[ "$(holders "$B")" = 0 ] || fail "refused spawn left a container on $B"

# D3: an edited docker_volumes on the default bucket diverges from the running jail's binds; that
# sibling may hold a live sandbox, so the spawn refuses instead of replacing it. The genuine replace
# (identical binds, egress/image drift) needs a config mutation mid-run — the unit tests cover it.
id5=$(spawn default "$A:/home/bot") || fail "scenario D3 default respawn crashed"
same_container "$id1" "$id5" || fail "unchanged default config got $id5, not the jail $id1"
out=$(spawn default "$A:/home/bot" "$B:/extra") || fail "scenario D3 drifted spawn crashed: $out"
case "$out" in
  "REFUSED "*"already bind-mounted read-write"*) echo "PASS refuse-on-default-bind-divergence" ;;
  *) fail "expected a refusal, got: $out" ;;
esac
[ "$(holders "$B")" = 0 ] || fail "refused spawn left a container on $B"
assert_single_jail "$A"

# E: daemon restart leaves the default jail stopped and forge recovers first (fresh: nothing runs
# to adopt). The default spawn then label-hits its stopped jail; the start is mount-gated and must
# adopt forge's running container instead of making a second holder of the jail path.
export TERMINAL_SANDBOX_DIR=$T/sbxE
idX=$(spawn default "$E:/home/bot") || fail "scenario E default spawn crashed"
docker stop "$idX" >/dev/null || fail "could not stop $idX"
idY=$(spawn profile:forge "$E:/home/bot") || fail "scenario E forge spawn crashed"
case "$idY" in REFUSED*) fail "forge must start fresh with nothing running, got: $idY" ;; esac
same_container "$idX" "$idY" && fail "forge started the stopped default jail $idX"
[ "$(docker inspect --format '{{.State.Running}}' "$idY")" = true ] || fail "forge container $idY not running"
idZ=$(spawn default "$E:/home/bot") || fail "scenario E default respawn crashed"
same_container "$idY" "$idZ" || fail "default got $idZ, not the running forge jail $idY"
[ "$(holders "$E")" = 1 ] || fail "$(holders "$E") running containers mount $E (want 1)"
echo "PASS restart-order-forge-first-default-converges"

# E2: the daemon restarts under a live process. Its default env adopted forge's container (labels
# not its own), so label search would miss it: recovery must restart that container, not run anew.
out=$("$PY" -c '
import os, subprocess, sys
from tools.environments.docker import DockerEnvironment
env = DockerEnvironment(image=os.environ["IMAGE"], cwd="/", task_id="default",
                        volumes=sys.argv[1:], persistent_filesystem=True)
with open(os.environ["CREATED_IDS"], "a") as f:
    f.write(env._container_id + "\n")
before = env._container_id
subprocess.run(["docker", "stop", before], check=True, capture_output=True)
result = env.execute("echo alive")
print(before, env._container_id, "alive" in result.get("output", ""))' "$E:/home/bot") \
  || fail "scenario E2 crashed: $out"
read -r before after alive <<<"$out"
[ "$alive" = True ] || fail "exec after recovery did not run: $out"
same_container "$idY" "$before" || fail "E2 env attached $before, not the jail $idY"
[ "$before" = "$after" ] || fail "recovery switched $before to $after instead of restarting it"
[ "$(holders "$E")" = 1 ] || fail "$(holders "$E") running containers mount $E after recovery (want 1)"
echo "PASS recovery-restarts-adopted-jail"
assert_single_jail "$A" "$C" "$E"

echo "ALL PASS"
