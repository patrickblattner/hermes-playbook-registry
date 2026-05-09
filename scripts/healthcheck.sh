#!/bin/bash
# Health-Probe für externe Monitoring-Tools (Uptime Kuma, Cron-Alert, etc.).
#
# Exit-Code:
#   0  /health antwortet 200 OK mit status='ok'
#   1  /health antwortet 200 aber status != 'ok' (z.B. degraded — DB down)
#   2  /health unerreichbar oder !200
#
# Beispiel als Host-Cron (5-Min-Intervall, alarmiert bei Fehler via mail):
#   */5 * * * * docker exec playbook-registry /app/scripts/healthcheck.sh \
#     || echo "playbook-registry health failed at $(date)" | mail -s ALERT ops@example.com

set -euo pipefail

URL=${HEALTH_URL:-http://localhost:8000/health}
TIMEOUT=${TIMEOUT:-5}

response=$(curl --silent --show-error --max-time "$TIMEOUT" --write-out "\n%{http_code}" "$URL" 2>&1) || {
    echo "ERROR: $URL unreachable: $response" >&2
    exit 2
}

http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" != "200" ]; then
    echo "ERROR: HTTP $http_code from $URL" >&2
    echo "$body" >&2
    exit 2
fi

# Mit python parsen — sqlite3 CLI hat kein JSON.
status=$(python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('status','?'))" <<< "$body")

if [ "$status" = "ok" ]; then
    echo "OK: $body"
    exit 0
else
    echo "DEGRADED: $body" >&2
    exit 1
fi
