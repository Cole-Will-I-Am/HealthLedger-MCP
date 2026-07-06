#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/srv/health-mcp/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

DB="${HEALTH_MCP_DB:-/srv/health-mcp/health.db}"
BACKUP_DIR="${HEALTH_MCP_BACKUP_DIR:-/srv/health-mcp/backups}"
RETENTION_DAYS="${HEALTH_MCP_BACKUP_RETENTION_DAYS:-14}"

if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
  echo "HEALTH_MCP_BACKUP_RETENTION_DAYS must be a positive integer" >&2
  exit 2
fi

if [[ ! -f "$DB" ]]; then
  echo "database not found: $DB" >&2
  exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp="$BACKUP_DIR/health-$timestamp.db.tmp"
out="$BACKUP_DIR/health-$timestamp.db"

sqlite3 "$DB" ".backup '$tmp'"
integrity="$(sqlite3 "$tmp" "PRAGMA integrity_check;")"
if [[ "$integrity" != "ok" ]]; then
  rm -f "$tmp"
  echo "backup integrity check failed: $integrity" >&2
  exit 1
fi

chmod 600 "$tmp"
mv "$tmp" "$out"
find "$BACKUP_DIR" -type f -name 'health-*.db' -mtime +"$RETENTION_DAYS" -delete
echo "$out"
