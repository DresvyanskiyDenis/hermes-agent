#!/usr/bin/env bash
# Live-daemon repro for duplicate jail mounts (t_44fdd37a): a spawn under another task bucket
# must adopt the running jail (A), a conflicting same-profile spawn must be refused (B), and the
# reverse leak order must converge on one container (C). Touches only containers it created
# that mount a path under its own temp root. Exit: 0 pass, 1 fail, 77 skip.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
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

# Every spawn records its id the moment it exists, so a later crash cannot leak it.
SPAWN_PY='
import os, sys
from tools.environments.docker import DockerEnvironment

def spawn(task_id, *volumes):
    env = DockerEnvironment(image=os.environ["IMAGE"], cwd="/", task_id=task_id,
                            volumes=list(volumes), persistent_filesystem=True)
    with open(os.environ["CREATED_IDS"], "a") as f:
        f.write(env._container_id + "\n")
    print(env._container_id, flush=True)
'

# spawn <task_id> <volume>...: prints the container id, or "REFUSED <message>".
spawn() {
  "$PY" -c "$SPAWN_PY
try:
    spawn(*sys.argv[1:])
except RuntimeError as e:
    print('REFUSED', e)" "$@"
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
  [ "$(holders "$B")" = 0 ] || fail "refused spawn left a container on $B"
  echo "PASS single-jail-invariant"
}

# A: the live leak shape — "default" holds the jail, a "profile:forge" spawn must adopt it.
out=$("$PY" -c "$SPAWN_PY
spawn('default', sys.argv[1])
spawn('profile:forge', sys.argv[1])" "$A:/home/bot") || fail "scenario A spawn crashed: $out"
id1=$(sed -n 1p <<<"$out") id2=$(sed -n 2p <<<"$out")
same_container "$id1" "$id2" || fail "profile:forge got $id2, not the default jail $id1"
echo "PASS adopt-across-buckets"
assert_single_jail "$A"

# B: same profile, mounts differ beyond the sandbox bucket while sharing jail A read-write.
out=$(spawn second:bucket "$A:/home/bot" "$B:/extra") || fail "scenario B spawn crashed: $out"
case "$out" in
  "REFUSED "*"already bind-mounted read-write"*) echo "PASS refuse-on-conflicting-identity" ;;
  *) fail "expected a refusal, got: $out" ;;
esac
assert_single_jail "$A"

# C: reverse order in a fresh sandbox root — forge creates first, default must adopt it.
export TERMINAL_SANDBOX_DIR=$T/sbxT
idF=$(spawn profile:forge "$C:/home/bot") || fail "scenario C forge spawn crashed"
idD=$(spawn default "$C:/home/bot") || fail "scenario C default spawn crashed"
same_container "$idF" "$idD" || fail "default got $idD, not the forge jail $idF"
echo "PASS selfheal-default-adopts-leaked-forge-jail"
assert_single_jail "$A" "$C"

echo "ALL PASS"
