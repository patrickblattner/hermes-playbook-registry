#!/bin/bash
# Hermes Playbook Registry — One-shot Deploy.
#
# Lädt die Compose-Definition (inline) ins INSTALL_DIR, zieht die Images von
# ghcr.io, startet den Stack, wartet auf den Healthcheck. Idempotent — kann
# mehrfach laufen, fungiert dann als Update (pull + up -d recreated nur
# Container deren Image sich verändert hat).
#
# Quick-Start (legt ein Unterverzeichnis im aktuellen Pfad an):
#   curl -fsSL https://raw.githubusercontent.com/patrickblattner/hermes-playbook-registry/main/deploy.sh | bash
#
# Oder explizit:
#   INSTALL_DIR=/opt/hermes-playbook-registry REGISTRY_TAG=latest bash deploy.sh
#
# Variablen:
#   INSTALL_DIR     Wo die compose.yml und Daten landen.
#                   Default: <PWD>/hermes-playbook-registry — also ein
#                   Unterverzeichnis im aktuellen Pfad. Wenn du z.B. in
#                   ~/Docker/ stehst, wird ~/Docker/hermes-playbook-registry/
#                   angelegt.
#   REGISTRY_TAG    Image-Tag (Default: latest)
#   GHCR_USER       GitHub-Username für ghcr.io login (nur falls Image privat)
#   GHCR_TOKEN      GitHub-PAT mit read:packages (nur falls Image privat)

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$PWD/hermes-playbook-registry}"
REGISTRY_TAG="${REGISTRY_TAG:-latest}"
GHCR_USER="${GHCR_USER:-}"
GHCR_TOKEN="${GHCR_TOKEN:-}"

log() { printf "\033[1;34m[deploy]\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m[ ok ]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[fail]\033[0m %s\n" "$*" >&2; }

# --- 1. Voraussetzungen prüfen ----------------------------------------------

if ! command -v docker >/dev/null; then
    err "docker fehlt. Installiere Docker Engine: https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    err "docker compose plugin fehlt. Update Docker Engine, oder installiere docker-compose-plugin."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    err "Docker daemon läuft nicht. systemctl start docker, oder Docker Desktop öffnen."
    exit 1
fi

ok "docker $(docker --version | awk '{print $3}' | tr -d ',') verfügbar"

# --- 1b. Shared Network anlegen ---------------------------------------------
# Externe Hermes-Agenten-Stacks hängen sich auch in dieses Network. Wir nutzen
# einen festen Namen (hermes-net) statt das compose-Prefix, damit andere Stacks
# es per external: true einfach referenzieren können.

if ! docker network inspect hermes-net >/dev/null 2>&1; then
    log "Lege Docker-Netzwerk 'hermes-net' an ..."
    docker network create hermes-net >/dev/null
    ok "hermes-net erstellt"
else
    ok "hermes-net existiert bereits"
fi

# --- 2. Install-Verzeichnis anlegen -----------------------------------------

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
log "Install-Dir: $INSTALL_DIR"

# --- 3. compose.yml schreiben (inline, kein git clone nötig) -----------------

cat > docker-compose.yml <<'YAML'
services:
  playbook-registry:
    image: ghcr.io/patrickblattner/hermes-playbook-registry:${REGISTRY_TAG:-latest}
    container_name: playbook-registry
    restart: unless-stopped
    volumes:
      - playbook-data:/data
    networks:
      - hermes-net

  playbook-registry-mcp:
    image: ghcr.io/patrickblattner/hermes-playbook-registry-mcp:${REGISTRY_TAG:-latest}
    container_name: playbook-registry-mcp
    restart: unless-stopped
    depends_on:
      - playbook-registry
    networks:
      - hermes-net
    environment:
      - PLAYBOOK_REGISTRY_URL=http://playbook-registry:8000
      - MCP_TRANSPORT=http
      - MCP_PORT=8001
      # Optional: DEFAULT_AGENT_ID für Single-Agent-Setups setzen.

volumes:
  playbook-data:
    driver: local

networks:
  hermes-net:
    external: true
    name: hermes-net
YAML

cat > .env <<EOF
REGISTRY_TAG=$REGISTRY_TAG
EOF

ok "docker-compose.yml + .env geschrieben"

# --- 4. Optional: GHCR-Login (nur falls private Images) ---------------------

if [ -n "$GHCR_USER" ] && [ -n "$GHCR_TOKEN" ]; then
    log "GHCR-Login als $GHCR_USER ..."
    echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null
    ok "ghcr.io eingeloggt"
fi

# --- 5. Pull + Up ------------------------------------------------------------

log "Pulling Images (Tag=$REGISTRY_TAG) ..."
docker compose pull

log "Starting stack ..."
docker compose up -d

# --- 6. Health-Wait ----------------------------------------------------------

log "Warte auf Health-Status ..."
for i in $(seq 1 30); do
    status=$(docker inspect --format='{{.State.Health.Status}}' playbook-registry 2>/dev/null || echo "starting")
    if [ "$status" = "healthy" ]; then
        ok "playbook-registry: healthy (nach ${i}s)"
        break
    fi
    sleep 1
done

if [ "$status" != "healthy" ]; then
    err "Health-Check innerhalb von 30s nicht grün. Logs:"
    docker compose logs --tail=20 playbook-registry
    exit 1
fi

# --- 7. Backup-Cron-Hinweis -------------------------------------------------

cat <<EOF

------------------------------------------------------------------------------
Hermes Playbook Registry läuft.

  REST (intern, im hermes-net):  http://playbook-registry:8000
  MCP   (intern, im hermes-net):  http://playbook-registry-mcp:8001/mcp

  Daten-Volume:    hermes-playbook-registry_playbook-data (Docker-managed)
  Healthcheck:     docker exec playbook-registry /app/scripts/healthcheck.sh
  Online-Backup:   docker exec playbook-registry /app/scripts/backup.sh

  Logs:            docker compose -f $INSTALL_DIR/docker-compose.yml logs -f
  Update:          INSTALL_DIR=$INSTALL_DIR bash deploy.sh
  Stop:            docker compose -f $INSTALL_DIR/docker-compose.yml down
  Stop + Daten:    docker compose -f $INSTALL_DIR/docker-compose.yml down -v

Empfehlung: stündlich Backup via Host-Cron einrichten:

  0 * * * * docker exec playbook-registry /app/scripts/backup.sh \\
              >> $INSTALL_DIR/backup.log 2>&1

------------------------------------------------------------------------------
EOF
