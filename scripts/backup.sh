#!/bin/bash
# Online-Backup der Playbook-Registry-DB.
#
# Nutzt SQLite's .backup-Befehl (atomar, läuft während aktiver Schreibzugriffe
# weiter — WAL-mode macht's möglich). Älteste Backups werden auf RETAIN
# Stück rotiert.
#
# Aufruf vom Host:
#   docker exec playbook-registry /app/scripts/backup.sh
#
# Empfohlen als Cron-Job (z.B. stündlich):
#   0 * * * * docker exec playbook-registry /app/scripts/backup.sh >> /var/log/playbook-backup.log 2>&1

set -euo pipefail

DB_PATH=${PLAYBOOK_DB_PATH:-/data/playbooks.db}
BACKUP_DIR=${BACKUP_DIR:-/data/backups}
RETAIN=${RETAIN:-24}   # Wieviele Backups behalten (24 = 1 Tag bei stündlichem Cron)

mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date -u +%Y%m%d-%H%M%SZ)
DEST="$BACKUP_DIR/playbooks-$TIMESTAMP.db"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup $DB_PATH -> $DEST"
sqlite3 "$DB_PATH" ".backup '$DEST'"

# Integrität verifizieren — kostet eine Sekunde, gibt aber Sicherheit dass das
# Backup wirklich lesbar ist (corruptes Backup ist schlimmer als kein Backup).
if ! sqlite3 "$DEST" "PRAGMA integrity_check" | grep -q "^ok$"; then
    echo "ERROR: backup integrity check failed for $DEST" >&2
    rm -f "$DEST"
    exit 1
fi

# Rotation: älteste löschen, neueste $RETAIN behalten.
ls -1t "$BACKUP_DIR"/playbooks-*.db 2>/dev/null \
  | tail -n +$((RETAIN + 1)) \
  | xargs -r rm -f

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] backup ok ($(du -h "$DEST" | cut -f1))"
