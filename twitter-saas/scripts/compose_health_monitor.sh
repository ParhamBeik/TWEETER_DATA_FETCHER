#!/usr/bin/env bash
# Periodic health check for twitter-saas Compose stack.
# Interval: 12 minutes. Logs to diagnostics/reports/saas_compose_health.log
set -u
ROOT="/Users/parham/Downloads/GITHUB_PROJECTS/TWEETER_DATA_FETCHER/twitter-saas"
LOG="/Users/parham/Downloads/GITHUB_PROJECTS/TWEETER_DATA_FETCHER/twitter_fetcher/diagnostics/reports/saas_compose_health.log"
INTERVAL_SEC=720  # 12 minutes
PIDFILE="/tmp/twitter_saas_health_monitor.pid"

echo $$ > "$PIDFILE"
mkdir -p "$(dirname "$LOG")"

check_once() {
  local ts ok=1
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  {
    echo "===== HEALTH $ts ====="
    cd "$ROOT" || { echo "FAIL: cannot cd $ROOT"; return 1; }

    docker compose ps -a
    local running
    running="$(docker compose ps --status running --format '{{.Service}}' 2>/dev/null | wc -l | tr -d ' ')"
    echo "running_services=$running (expect 6)"
    if [ "$running" -lt 6 ]; then ok=0; fi

    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8002/admin/login/ || echo 000)"
    echo "web_admin=$code"; [ "$code" = "200" ] || ok=0
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8080/ || echo 000)"
    echo "frontend=$code"; [ "$code" = "200" ] || ok=0
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8080/api/ || echo 000)"
    echo "frontend_api_proxy=$code"; [ "$code" = "401" ] || [ "$code" = "403" ] || ok=0

    if docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
      echo "redis=PONG"
    else
      echo "redis=FAIL"; ok=0
    fi
    if docker compose exec -T postgres pg_isready -U postgres 2>/dev/null | grep -q accepting; then
      echo "postgres=accepting"
    else
      echo "postgres=FAIL"; ok=0
    fi

    echo "--- recent errors (web/worker/beat, last 80 lines) ---"
    docker compose logs --tail=80 web worker beat 2>/dev/null \
      | grep -E 'ERROR|CRITICAL|Traceback|FATAL|WorkerLost|Connection refused' \
      | grep -v 'Unauthorized' \
      | tail -20 || true

    if [ "$ok" -eq 1 ]; then
      echo "RESULT=OK"
    else
      echo "RESULT=DEGRADED"
    fi
    echo
  } >> "$LOG" 2>&1
}

while true; do
  check_once
  sleep "$INTERVAL_SEC"
done
