#!/usr/bin/env bash
# clean.sh — remove garbage from results/
#
# Garbage patterns (all at results/ root, none inside a target/run dir):
#   results/run-N/             — top-level run dir with no target-key wrapper
#   results/smoke-*            — direct smoke.py invocations
#   results/verify*            — verify harness ad-hoc runs (verify-, verify2-, verify3-, verify-ds*, verify-tb*)
#   results/.orphan-cleanup-*  — old backups created by previous runs of this script
#
# Safety:
#   - Backs up to results/.orphan-cleanup-<TS>/ before any deletion by default.
#   - Deletes ONLY explicitly matched patterns; never globs over results/*.
#   - Idempotent: re-running is safe.
#
# Usage:
#   ./clean.sh                       # back up + delete (dry-run if --dry-run)
#   ./clean.sh --dry-run             # print actions without doing them
#   ./clean.sh --no-backup           # skip the backup step
#   ./clean.sh --keep-backup-days N  # also drop .orphan-cleanup-* dirs older than N days (default 14)
#
# Exit codes:
#   0 — success (or dry-run with no errors)
#   2 — misuse (bad flag)
#   non-zero from inner commands on real failures

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

die(){ echo "ERROR: $*" >&2; exit 2; }
log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

DRY_RUN=0
DO_BACKUP=1
KEEP_BACKUP_DAYS=14

while (($#)); do
  case "$1" in
    --dry-run)            DRY_RUN=1; shift;;
    --no-backup)          DO_BACKUP=0; shift;;
    --keep-backup-days)   (($#>1)) || die 'missing --keep-backup-days value'; KEEP_BACKUP_DAYS="$2"; shift 2;;
    --keep-backup-days=*) KEEP_BACKUP_DAYS="${1#*=}"; shift;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0;;
    *) die "unknown argument: $1";;
  esac
done

run(){
  if (( DRY_RUN )); then
    printf '[dry-run] %s\n' "$*"
  else
    eval "$@"
  fi
}

# Identify orphan dirs (explicit patterns, never a wildcard over results/).
ORPHANS=()
[[ -d results/run-1 ]] && ORPHANS+=(results/run-1)
for d in results/smoke-*;    do [[ -d "$d" ]] && ORPHANS+=("$d"); done
for d in results/verify*;    do [[ -d "$d" ]] && ORPHANS+=("$d"); done

# Old backups (always candidate; --keep-backup-days controls cutoff).
OLD_BACKUPS=()
if (( KEEP_BACKUP_DAYS > 0 )); then
  while IFS= read -r d; do
    [[ -d "$d" ]] && OLD_BACKUPS+=("$d")
  done < <(find results -maxdepth 1 -type d -name '.orphan-cleanup-*' -mtime +"$KEEP_BACKUP_DAYS" 2>/dev/null)
fi

if (( ${#ORPHANS[@]} == 0 && ${#OLD_BACKUPS[@]} == 0 )); then
  log "nothing to clean"
  exit 0
fi

log "found ${#ORPHANS[@]} orphan dir(s): ${ORPHANS[*]:-<none>}"
log "found ${#OLD_BACKUPS[@]} old backup(s) (>$KEEP_BACKUP_DAYS days): ${OLD_BACKUPS[*]:-<none>}"

# Step 1: back up the orphans (skip with --no-backup).
if (( DO_BACKUP && ${#ORPHANS[@]} > 0 )); then
  TS=$(date -u +%Y%m%d-%H%M%S)
  BACKUP="results/.orphan-cleanup-$TS"
  log "backing up to $BACKUP/"
  run "mkdir -p '$BACKUP'"
  for d in "${ORPHANS[@]}"; do
    base="$(basename "$d")"
    run "cp -a '$d' '$BACKUP/$base'"
  done
else
  log "skipping backup (--no-backup or no orphans)"
fi

# Step 2: delete orphans.
for d in "${ORPHANS[@]}"; do
  run "rm -rf '$d'"
  log "deleted: $d"
done

# Step 3: expire old backups.
for d in "${OLD_BACKUPS[@]}"; do
  run "rm -rf '$d'"
  log "expired: $d"
done

log "done"
