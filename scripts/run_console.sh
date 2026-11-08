#!/usr/bin/env bash
# One-command local operator console: backend API (uv run python -m backend.api) + Vite frontend.
#
#   scripts/run_console.sh [--mode mock|sandbox] [--db PATH] [--api-port N] [--ui-port N]
#
# Defaults: --mode mock, API http://127.0.0.1:8765, UI http://127.0.0.1:5173, DB var/settlewell.db.
# sandbox takes its DB from the hero run manifest, so --db is refused in sandbox mode.
# Ports must be free; an occupied port is refused, never killed or reused.
# On exit only the process groups this script started are stopped.
# The frontend never inherits Airwallex, Anthropic or GitHub credentials; the backend keeps its env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="mock"
DB=""
API_PORT=8765
UI_PORT=5173
DEFAULT_UI_PORT=5173
PIDS=()

die() {
  echo "refused: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: scripts/run_console.sh [--mode mock|sandbox] [--db PATH] [--api-port N] [--ui-port N]
  --mode      mock (default, synthetic local ledger) or sandbox (Airwallex sandbox only)
  --db        mock mode only: SQLite path (default var/settlewell.db)
  --api-port  backend port on 127.0.0.1 (default 8765)
  --ui-port   Vite port on 127.0.0.1 (default 5173)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --db) DB="${2:-}"; shift 2 ;;
    --api-port) API_PORT="${2:-}"; shift 2 ;;
    --ui-port) UI_PORT="${2:-}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

case "$MODE" in
  mock | sandbox) ;;
  *) die "--mode must be mock or sandbox" ;;
esac

valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1024 && 10#$1 <= 65535 )); }
valid_port "$API_PORT" || die "--api-port must be 1024-65535"
valid_port "$UI_PORT" || die "--ui-port must be 1024-65535"
API_PORT=$((10#$API_PORT))
UI_PORT=$((10#$UI_PORT))
(( API_PORT != UI_PORT )) || die "API and UI ports must differ"

# Resolve --db against the caller's directory, before we change directory.
if [[ "$MODE" == sandbox ]]; then
  [[ -z "$DB" ]] || die "sandbox takes its DB from the hero run manifest; drop --db"
else
  DB="$(realpath -m -- "${DB:-$ROOT/var/settlewell.db}")"
fi

for tool in uv npm python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH"
done
[[ -d "$ROOT/frontend/node_modules" ]] || die "frontend/node_modules missing; run npm ci in frontend/ first"

# True when nothing is listening on 127.0.0.1:PORT (bind test; never connects to or stops anything).
port_free() {
  python3 -I -c '
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
' "$1"
}
port_free "$API_PORT" || die "API port $API_PORT is already in use; not killing or reusing it"
port_free "$UI_PORT" || die "UI port $UI_PORT is already in use; not killing or reusing it"

# Known credential names only. Names are removed from the child env; values are never read or printed.
SCRUB_VARS=(
  ANTHROPIC_API_KEY
  GITHUB_TOKEN_SETTLEWELL
  AIRWALLEX_BASE_URL
  AIRWALLEX_CLIENT_ID
  AIRWALLEX_API_KEY
  AIRWALLEX_SANDBOX_CLIENT_ID
  AIRWALLEX_SANDBOX_API_KEY
  AIRWALLEX_WEBHOOK_SECRET
)
FE_UNSET=()
for v in "${SCRUB_VARS[@]}"; do FE_UNSET+=(-u "$v"); done

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    kill -TERM -- "-$pid" 2>/dev/null || true
  done
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do
    wait "$pid" 2>/dev/null || true
  done
  exit "$rc"
}
trap cleanup EXIT INT TERM

# Each child gets its own process group, so cleanup signals only what we started.
set -m

wait_for_port() {
  local port=$1 pid=$2 name=$3 i
  for (( i = 0; i < 240; i++ )); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      return 0
    fi
    kill -0 "$pid" 2>/dev/null || die "$name exited before listening on $port (see output above)"
    sleep 0.5
  done
  die "$name did not listen on $port within 120s"
}

cd "$ROOT"

if [[ "$MODE" == mock ]]; then
  API_CMD=(uv run python -m backend.api --mode mock --db "$DB" --port "$API_PORT")
else
  API_CMD=(uv run python -m backend.api --mode sandbox --port "$API_PORT")
fi
if (( UI_PORT != DEFAULT_UI_PORT )); then
  API_CMD+=(--ui-port "$UI_PORT")
fi

echo "starting API ($MODE) on 127.0.0.1:$API_PORT"
"${API_CMD[@]}" &
API_PID=$!
PIDS+=("$API_PID")
wait_for_port "$API_PORT" "$API_PID" "API"

echo "starting UI on 127.0.0.1:$UI_PORT (proxy -> 127.0.0.1:$API_PORT)"
env "${FE_UNSET[@]}" SETTLEWELL_API_PROXY_TARGET="http://127.0.0.1:$API_PORT" \
  npm --prefix "$ROOT/frontend" run dev -- --host 127.0.0.1 --port "$UI_PORT" --strictPort &
UI_PID=$!
PIDS+=("$UI_PID")
wait_for_port "$UI_PORT" "$UI_PID" "UI"

echo "console: http://127.0.0.1:$UI_PORT   api: http://127.0.0.1:$API_PORT   mode: $MODE"
echo "Ctrl-C stops only the API and UI started by this script."
wait -n || true
die "a started process exited; stopping the other one"
