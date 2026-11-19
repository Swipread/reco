#!/usr/bin/env bash
# End-to-end tests: the real backend (Swipread/api) + this reco + PostgreSQL + Redis.
#
#   SWIPREAD_API_DIR=/path/to/swipread-api scripts/e2e.sh [--keep] [pytest args...]
#   scripts/e2e.sh down
#
# Starts the backend's compose services (its compose.yaml, compose project $E2E_COMPOSE_PROJECT),
# the backend without profile (real HTTP recommendation client, dev sample data), seeds test data,
# starts the reco api and worker as a login role member of swipread_reco, runs `pytest -m e2e`,
# then stops everything. --keep leaves it all running; `scripts/e2e.sh down` stops it.
# Honors UV_PROJECT_ENVIRONMENT (virtualenv used by uv).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${E2E_COMPOSE_PROJECT:-swipread-e2e}"
STATE_FILE="${TMPDIR:-/tmp}/${PROJECT}.state"
RECO_ROLE="reco_e2e"
JWT_SECRET="e2e-only-secret-e2e-only-secret-0123456789"

log() { printf '[e2e] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

compose() { docker compose -p "$PROJECT" -f "$API_DIR/compose.yaml" "$@"; }

# Stop a process from its pid file: TERM, then KILL after 15 s.
stop_pid() {
  local file="$1" pid
  [[ -f "$file" ]] || return 0
  pid="$(cat "$file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
}

# Stop what the run recorded in $STATE_FILE started.
down() {
  [[ -f "$STATE_FILE" ]] || { log "nothing to stop ($STATE_FILE absent)"; return 0; }
  # shellcheck disable=SC1090
  source "$STATE_FILE"
  log "stopping run $RUN_DIR"
  stop_pid "$RUN_DIR/reco-api.pid"
  stop_pid "$RUN_DIR/worker.pid"
  stop_pid "$RUN_DIR/backend.pid"
  docker compose -p "$PROJECT" -f "$API_DIR/compose.yaml" down -v --remove-orphans >/dev/null 2>&1 \
    || log "docker compose down failed"
  rm -f "$STATE_FILE"
  log "stopped; logs kept in $RUN_DIR"
}

free_port() {
  "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

# wait_until <seconds> <description> <command...>
wait_until() {
  local timeout="$1" what="$2"; shift 2
  local deadline=$((SECONDS + timeout))
  until "$@" >/dev/null 2>&1; do
    ((SECONDS < deadline)) || die "timed out after ${timeout}s waiting for $what (logs: $RUN_DIR)"
    sleep 1
  done
}

alive() { kill -0 "$(cat "$1")" 2>/dev/null; }

if [[ "${1:-}" == "down" ]]; then
  down
  exit 0
fi

KEEP=0
if [[ "${1:-}" == "--keep" ]]; then KEEP=1; shift; fi

API_DIR="${SWIPREAD_API_DIR:-}"
[[ -n "$API_DIR" && -f "$API_DIR/compose.yaml" ]] \
  || die "SWIPREAD_API_DIR must point at a checkout of Swipread/api (compose.yaml not found)"
API_DIR="$(cd "$API_DIR" && pwd)"
command -v docker >/dev/null || die "docker not found"
command -v java >/dev/null || die "java (JDK 25) not found"
command -v uv >/dev/null || die "uv not found"

# A previous --keep run, or an interrupted one: start from a clean state.
if [[ -f "$STATE_FILE" ]]; then log "previous run found, stopping it first"; down; fi

RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swipread-e2e.XXXXXX")"
printf "RUN_DIR='%s'\nAPI_DIR='%s'\n" "$RUN_DIR" "$API_DIR" > "$STATE_FILE"
log "logs: $RUN_DIR"

cleanup() {
  local status=$?
  if ((KEEP == 0)); then down; else
    log "left running (--keep); stop with: scripts/e2e.sh down"
  fi
  log "logs: $RUN_DIR"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

cd "$REPO"
PY="$(uv run python -c 'import sys; print(sys.executable)')"   # syncs the environment first

log "starting PostgreSQL and Redis (compose project $PROJECT)"
compose up -d >"$RUN_DIR/compose.log" 2>&1 || die "docker compose up failed, see $RUN_DIR/compose.log"
PG_PORT="$(compose port postgres 5432 | sed 's/.*://')"
REDIS_PORT="$(compose port redis 6379 | sed 's/.*://')"
wait_until 60 "PostgreSQL" compose exec -T postgres pg_isready -U swipread -d swipread -h 127.0.0.1
wait_until 30 "Redis" compose exec -T redis redis-cli ping

# The jar has a fixed reproducible-build date: compare the sources with a stamp written after the build.
JAR="$(ls "$API_DIR"/target/api-*.jar 2>/dev/null | head -1 || true)"
STAMP="$API_DIR/target/.swipread-e2e-build"
if [[ -z "$JAR" || ! -f "$STAMP" \
      || -n "$(find "$API_DIR/src/main" "$API_DIR/pom.xml" -newer "$STAMP" -print -quit)" ]]; then
  log "building the backend (first run downloads its dependencies)"
  (cd "$API_DIR" && ./mvnw -q -DskipTests package) >"$RUN_DIR/backend-build.log" 2>&1 \
    || die "backend build failed, see $RUN_DIR/backend-build.log"
  touch "$STAMP"
  JAR="$(ls "$API_DIR"/target/api-*.jar | head -1)"
fi

BACKEND_PORT="$(free_port)"
RECO_PORT="$(free_port)"
while [[ "$RECO_PORT" == "$BACKEND_PORT" ]]; do RECO_PORT="$(free_port)"; done

# No profile: `dev` and `test` include `mock-reco`, which replaces the HTTP client. The dev sample
# data comes from the same Flyway locations as `dev`, on a fresh database.
log "starting the backend on port $BACKEND_PORT (reco expected on $RECO_PORT)"
(
  cd "$API_DIR"
  export SERVER_PORT="$BACKEND_PORT" \
    SPRING_DATASOURCE_URL="jdbc:postgresql://127.0.0.1:$PG_PORT/swipread" \
    SPRING_DATASOURCE_USERNAME=swipread SPRING_DATASOURCE_PASSWORD=swipread \
    SPRING_DATA_REDIS_HOST=127.0.0.1 SPRING_DATA_REDIS_PORT="$REDIS_PORT" \
    SPRING_FLYWAY_LOCATIONS="classpath:db/migration,classpath:db/seed,classpath:db/dev-data" \
    SWIPREAD_JWT_SECRET="$JWT_SECRET" SWIPREAD_JWT_TTL=2h \
    SWIPREAD_AUTH_REFRESH_COOKIE_SECURE=false \
    SWIPREAD_RECO_URL="http://127.0.0.1:$RECO_PORT"
  exec java -jar "$JAR"
) >"$RUN_DIR/backend.log" 2>&1 &
echo $! >"$RUN_DIR/backend.pid"
backend_up() { alive "$RUN_DIR/backend.pid" || die "backend exited, see $RUN_DIR/backend.log"
  curl -sf "http://127.0.0.1:$BACKEND_PORT/actuator/health" | grep -q '"UP"'; }
wait_until 120 "the backend" backend_up

export E2E_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
export E2E_RECO_URL="http://127.0.0.1:$RECO_PORT"
export E2E_ADMIN_DSN="postgresql://swipread:swipread@127.0.0.1:$PG_PORT/swipread"
export E2E_RECO_DSN="postgresql://$RECO_ROLE:$RECO_ROLE@127.0.0.1:$PG_PORT/swipread"
export E2E_REDIS_URL="redis://127.0.0.1:$REDIS_PORT/0"
export E2E_RUN_DIR="$RUN_DIR"
export E2E_API_LOG="$RUN_DIR/backend.log"

log "seeding test data"
PYTHONPATH="$REPO/src:$REPO" "$PY" -m tests.e2e.seed >"$RUN_DIR/seed.log" 2>&1 \
  || die "seed failed, see $RUN_DIR/seed.log"

export RECO_DATABASE_URL="$E2E_RECO_DSN" RECO_REDIS_URL="$E2E_REDIS_URL" RECO_PORT \
  HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
log "starting the reco worker and api (role $RECO_ROLE)"
PYTHONPATH="$REPO/src" "$PY" -m swipread_reco worker >"$RUN_DIR/worker.log" 2>&1 &
echo $! >"$RUN_DIR/worker.pid"
PYTHONPATH="$REPO/src" "$PY" -m swipread_reco api >"$RUN_DIR/reco-api.log" 2>&1 &
echo $! >"$RUN_DIR/reco-api.pid"

reco_up() { alive "$RUN_DIR/reco-api.pid" || die "reco api exited, see $RUN_DIR/reco-api.log"
  curl -sf "$E2E_RECO_URL/healthz" | grep -q '"catalogSize"'; }
wait_until 60 "the reco api" reco_up
fallback_written() { alive "$RUN_DIR/worker.pid" || die "worker exited, see $RUN_DIR/worker.log"
  grep -q "fallback lists written" "$RUN_DIR/worker.log"; }
wait_until 120 "the worker's fallback lists" fallback_written
log "ready: backend $E2E_BACKEND_URL, reco $E2E_RECO_URL"

set +e
"$PY" -m pytest -m e2e tests/e2e -p no:cacheprovider "$@"
status=$?
set -e
if [[ -f "$RUN_DIR/measures.txt" ]]; then log "measures:"; cat "$RUN_DIR/measures.txt" >&2; fi
exit "$status"
