#!/usr/bin/env bash
# Live-daemon repro for duplicate jail mounts (t_44fdd37a). Touches only containers it created
# that mount a path under its own temp root. Exit: 0 pass, 1 fail, 77 skip.
#   A: a spawn under another task bucket adopts the running default jail.
#   B: a conflicting same-profile spawn sharing the jail path is refused.
#   C: the reverse leak order (forge first, then default) converges on one container.
#   D: per-task buckets sharing only sandbox dirs coexist (D1), a non-default spawn sharing a
#      real jail path is still refused (D2), and default-bucket config drift replaces (D3).
set -u
cd "$(dirname "$0")/.."
PY=${HERMES_PYTHON:-.venv/bin/python}
export IMAGE=alpine

skip() { echo "SKIP: $*"; exit 77; }
fail() { echo "FAIL: $*"; exit 1; }

docker info >/dev/null 2>&1 || skip "no daemon/image"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null 2>&1 \
  || skip "no daemon/image"

T=$(realpath "$(mktemp -d)")
A=$T/jailA B=$T/jailB C=$T/jailC
mkdir -p "$A" "$B" "$C" "$T/tmp"
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
out=$(spawn rollout:three "$A:/home/bot" "$B:/extra") || fail "scenario D2 spawn crashed: $out"
case "$out" in
  "REFUSED "*"already bind-mounted read-write"*) echo "PASS refuse-non-default-side" ;;
  *) fail "expected a refusal, got: $out" ;;
esac
[ "$(holders "$B")" = 0 ] || fail "refused spawn left a container on $B"

# D3: config evolution on the default bucket — an edited docker_volumes replaces the jail.
export TERMINAL_SANDBOX_DIR=$T/sandboxes
id5=$(spawn default "$A:/home/bot") || fail "scenario D3 default respawn crashed"
same_container "$id1" "$id5" || fail "unchanged default config got $id5, not the jail $id1"
id6=$(spawn default "$A:/home/bot" "$B:/extra") || fail "scenario D3 drifted spawn crashed: $id6"
case "$id6" in REFUSED*) fail "config drift must replace, got: $id6" ;; esac
same_container "$id1" "$id6" && fail "config drift reused the stale jail $id1"
[ "$(holders "$A")" = 1 ] && [ "$(holders "$B")" = 1 ] || fail "replacement left $(holders "$A") holders of $A"
echo "PASS replace-on-config-evolution"
assert_single_jail "$A" "$B" "$C"

echo "ALL PASS"
