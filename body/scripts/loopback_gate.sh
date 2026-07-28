#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/cargo/bin:$PATH"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/tmp/praxis-body-target}"

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATE=$(mktemp -d)
BRIDGE_PID=""
BODY_PID=""

cleanup() {
  if [ -n "$BODY_PID" ]; then kill "$BODY_PID" 2>/dev/null || true; fi
  if [ -n "$BRIDGE_PID" ]; then kill "$BRIDGE_PID" 2>/dev/null || true; fi
  rm -rf "$STATE"
}
trap cleanup EXIT

command -v curl >/dev/null
cd "$ROOT"
cargo build --workspace

export PRAXIS_BRIDGE_DEVICE_TOKEN=device-loopback-secret
export PRAXIS_BRIDGE_CONTROLLER_TOKEN=controller-loopback-secret
"$CARGO_TARGET_DIR/debug/praxis-bridge" --listen 127.0.0.1:9473 --state-dir "$STATE/bridge" \
  >"$STATE/bridge.log" 2>&1 &
BRIDGE_PID=$!

for _ in $(seq 1 100); do
  curl -fsS http://127.0.0.1:9473/healthz >/dev/null 2>&1 && break
  sleep .05
done
curl -fsS http://127.0.0.1:9473/healthz | grep -q 'praxis.body.v1'

mkdir -p "$STATE/work"
printf 'artifact-roundtrip\n' >"$STATE/work/source.txt"
cat >"$STATE/body.json" <<EOF
{
  "device_id": "loopback-pc",
  "bridge_ws_url": "ws://127.0.0.1:9473",
  "artifact_base_url": "http://127.0.0.1:9473",
  "token": "device-loopback-secret",
  "state_dir": "$STATE/body",
  "artifact_chunk_size": 65536
}
EOF
"$CARGO_TARGET_DIR/debug/praxis-body" connect --config "$STATE/body.json" >"$STATE/body.log" 2>&1 &
BODY_PID=$!
sleep .3

controller() {
  curl -fsS -H 'Authorization: Bearer controller-loopback-secret' \
    -H 'Content-Type: application/json' "$@"
}

invoke() {
  local request_id=$1 capability=$2 args=$3 operation_id=${4:-op-$1}
  controller -X POST "http://127.0.0.1:9473/v1/controller/loopback-pc/invoke" \
    --data "{\"request_id\":\"$request_id\",\"operation_id\":\"$operation_id\",\"execution\":\"system\",\"capability\":\"$capability\",\"args\":$args}"
}

wait_response() {
  local request_id=$1 pattern=$2 payload=""
  for _ in $(seq 1 200); do
    payload=$(controller "http://127.0.0.1:9473/v1/controller/loopback-pc/requests/$request_id")
    if printf '%s' "$payload" | grep -q "$pattern"; then
      printf '%s' "$payload"
      return 0
    fi
    sleep .05
  done
  printf 'timeout waiting for %s (%s)\nlast=%s\n' "$request_id" "$pattern" "$payload" >&2
  return 1
}

invoke req-status body.status '{}' >/dev/null
wait_response req-status '"type":"result"' >/dev/null

TARGET="$STATE/work/once.txt"
invoke req-write fs.write_atomic "{\"path\":\"$TARGET\",\"content\":\"alpha\"}" >/dev/null
wait_response req-write '"type":"result"' >/dev/null
invoke req-write fs.write_atomic "{\"path\":\"$TARGET\",\"content\":\"beta\"}" >/dev/null
wait_response req-write 'id_conflict' >/dev/null
test "$(cat "$TARGET")" = alpha

SOURCE="$STATE/work/source.txt"
SHA=$(sha256sum "$SOURCE" | awk '{print $1}')
SIZE=$(wc -c <"$SOURCE" | tr -d ' ')
invoke req-export fs.export "{\"path\":\"$SOURCE\"}" >/dev/null
wait_response req-export '"type":"result"' >/dev/null

DEST="$STATE/work/restored.txt"
invoke req-import fs.import "{\"artifact\":{\"sha256\":\"$SHA\",\"size\":$SIZE,\"name\":\"source.txt\",\"mime\":\"text/plain\",\"source_device\":\"loopback-pc\"},\"path\":\"$DEST\"}" >/dev/null
wait_response req-import '"type":"result"' >/dev/null
cmp "$SOURCE" "$DEST"

invoke req-process process.start '{"program":"/bin/sh","args":["-c","printf process-ok"]}' op-process >/dev/null
wait_response req-process '"type":"accepted"' >/dev/null
PROCESS_OK=0
for index in $(seq 1 100); do
  request_id="req-poll-$index"
  invoke "$request_id" process.status '{"operation_id":"op-process","tail":4096}' "op-poll-$index" >/dev/null
  payload=$(wait_response "$request_id" '"type":"result"')
  if printf '%s' "$payload" | grep -q 'succeeded'; then
    printf '%s' "$payload" | grep -q 'process-ok'
    PROCESS_OK=1
    break
  fi
  sleep .05
done
test "$PROCESS_OK" = 1

printf 'PASS praxis-body loopback: transport idempotency process files artifacts\n'
