#!/bin/bash
# Stellt die Playbook-Registry-DB aus einem Backup wieder her.
#
# Aufruf vom Host:
#   docker compose stop playbook-registry
#   docker run --rm -v hermes-playbook-registry_playbook-data:/data \
#     -v $(pwd)/scripts:/scripts hermes-playbook-registry-playbook-registry \
#     /scripts/restore.sh /data/backups/playbooks-20260509-120000Z.db
#   docker compose start playbook-registry
#
# Wichtig: Service vorher stoppen (sonst hängt SQLite mit Lock).

set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <backup-file>" >&2
    exit 1
fi

SRC="$1"
DB_PATH=${PLAYBOOK_DB_PATH:-/data/playbooks.db}

if [ ! -f "$SRC" ]; then
    echo "ERROR: backup file not found: $SRC" >&2
    exit 1
fi

# Integrität des Backups vor Restore prüfen.
if ! sqlite3 "$SRC" "PRAGMA integrity_check" | grep -q "^ok$"; then
    echo "ERROR: backup integrity check failed for $SRC, refusing to restore" >&2
    exit 1
fi

# Aktuelle DB als safety-copy beiseite legen.
if [ -f "$DB_PATH" ]; then
    SAFETY="$DB_PATH.pre-restore.$(date -u +%Y%m%d-%H%M%SZ)"
    cp "$DB_PATH" "$SAFETY"
    cp -f "$DB_PATH-wal" "$SAFETY-wal" 2>/dev/null || true
    cp -f "$DB_PATH-shm" "$SAFETY-shm" 2>/dev/null || true
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] safety copy: $SAFETY"
fi

# Restore: SQLite kann ein Backup direkt einkopieren (atomar).
sqlite3 "$DB_PATH" ".restore '$SRC'"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] restored $SRC -> $DB_PATH"
