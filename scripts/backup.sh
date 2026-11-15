#!/usr/bin/env bash
#
# Take a verified online backup of the KCalorieBot database and prune old
# scheduled backups. Meant for a daily systemd timer (see README, "Backups and
# Restore"); both services keep running.
#
#   kcaloriebot-backup            # back up, verify, then prune
#
# Exit status:
#   0   backup written, verified and retention applied
#   75  skipped on purpose, nothing written or deleted: an update holds the
#       lock, or the bot is not running (maintenance or manual recovery)
#   1   anything else; when the backup itself failed, nothing was pruned
#
# Only files named scheduled-YYYYMMDDTHHMMSSZ.db, created by this script, are
# ever pruned. The updater's before-*/failed-* snapshots, manual backups and any
# other file are never touched.
#
set -Eeuo pipefail
umask 077

# An explicitly empty value is an error, not "use the default": these paths
# decide what gets deleted, so `${VAR-default}` (no colon) is deliberate.
DB_PATH=${KCALORIE_DATABASE-/var/lib/kcaloriebot/kcaloriebot.db}
BACKUP_DIR=${KCALORIE_BACKUPS-/var/backups/kcaloriebot}
SERVICE=${KCALORIE_BOT_SERVICE-kcalculatorbot}
RUN_AS=${KCALORIE_RUN_AS-kcaloriebot}
LOCK_PATH=${KCALORIE_LOCK-/run/lock/kcaloriebot-update.lock}
KEEP_DAYS=${KCALORIE_BACKUP_KEEP_DAYS-30}

EXIT_SKIPPED=75
PREFIX=scheduled-
# Fixed-width UTC timestamps sort lexically in chronological order.
NAME_RE='^scheduled-[0-9]{8}T[0-9]{6}Z\.db$'
PARTIAL_RE='^\.scheduled-[0-9]{8}T[0-9]{6}Z-[0-9]+\.incomplete(-journal|-wal|-shm)?$'
# Absolute, not "/" alone, no "//" or trailing slash, conservative characters.
PATH_RE='^(/[A-Za-z0-9._@+-]+)+$'
DAYS_RE='^[1-9][0-9]{0,3}$'
UNIT_RE='^[A-Za-z0-9@._:-]+$'
USER_RE='^[a-z_][a-z0-9_-]*$'

TMP_PATH=""

# Under systemd, <N> prefixes set the journal priority of each line.
if [ -n "${JOURNAL_STREAM:-}" ]; then
    P_INFO='<6>' P_WARN='<4>' P_ERR='<3>'
else
    P_INFO='' P_WARN='' P_ERR=''
fi
log()  { printf '%s%s\n' "$P_INFO" "$*"; }
warn() { printf '%sWARNING: %s\n' "$P_WARN" "$*" >&2; }
die()  { printf '%sERROR: %s\n' "$P_ERR" "$*" >&2; exit 1; }
skip() { warn "Backup skipped: $*"; exit "$EXIT_SKIPPED"; }

usage() {
    printf '%s\n' 'Usage: kcaloriebot-backup' \
        'Back up the live database, verify it, and prune old scheduled backups.' \
        'Exit 0 = done, 75 = skipped (update running or bot stopped), other = failure.'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --help|-h) usage ;;
        *) die "Unknown option: $arg (try --help)" ;;
    esac
done

# Unexpected failures must never look like a deliberate skip.
on_error() {
    local code=$? line=${1:-?}
    [ "$code" -ne "$EXIT_SKIPPED" ] || code=1
    # A failing command substitution reports once, from the parent shell.
    if [ "$BASH_SUBSHELL" -gt 0 ]; then exit "$code"; fi
    printf '%sERROR: Backup failed at line %s (exit %s); nothing was pruned.\n' \
        "$P_ERR" "$line" "$code" >&2
    exit "$code"
}
trap 'on_error $LINENO' ERR

# Drop to the service account without PAM or setuid helpers, so this also works
# under NoNewPrivileges=. Every file in the backup directory and every SQLite
# connection to the live database is handled as that account: backups end up
# owned like the updater's, no root-owned -wal/-shm files appear next to the
# live database, and root never writes into a directory the bot can modify.
as_bot() { setpriv --reuid="$RUN_UID" --regid="$RUN_GID" --init-groups -- "$@"; }

cleanup() {
    local suffix
    [ -n "$TMP_PATH" ] || return 0
    for suffix in "" -journal -wal -shm; do
        if [ -e "$TMP_PATH$suffix" ] || [ -L "$TMP_PATH$suffix" ]; then
            as_bot rm -f -- "$TMP_PATH$suffix" || warn "Could not remove $TMP_PATH$suffix"
        fi
    done
}
trap 'trap - ERR; set +e; cleanup' EXIT

# --------------------------------------------------------------------------
# Configuration checks: refuse anything odd before touching the filesystem
# --------------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Run as root (the update lock is root-only)."

check_path() {
    local name=$1 value=$2 part rest
    [ -n "$value" ] || die "$name is empty."
    [[ $value =~ $PATH_RE ]] \
        || die "$name must be an absolute path of [A-Za-z0-9._@+-] components: '$value'"
    rest=${value#/}
    while [ -n "$rest" ]; do
        part=${rest%%/*}
        case "$part" in .|..) die "$name must not contain '.' or '..' components: '$value'" ;; esac
        [ "$part" != "$rest" ] || break
        rest=${rest#*/}
    done
}
check_path KCALORIE_BACKUPS "$BACKUP_DIR"
check_path KCALORIE_DATABASE "$DB_PATH"
check_path KCALORIE_LOCK "$LOCK_PATH"
[[ $KEEP_DAYS =~ $DAYS_RE ]] \
    || die "KCALORIE_BACKUP_KEEP_DAYS must be a whole number from 1 to 9999: '$KEEP_DAYS'"
[[ $SERVICE =~ $UNIT_RE ]] || die "Odd KCALORIE_BOT_SERVICE: '$SERVICE'"
[[ $RUN_AS =~ $USER_RE ]] || die "Odd KCALORIE_RUN_AS: '$RUN_AS'"

for cmd in sqlite3 systemctl setpriv flock; do
    command -v "$cmd" >/dev/null || die "Required command not found: $cmd"
done

# --------------------------------------------------------------------------
# Coordination with kcaloriebot-update
# --------------------------------------------------------------------------

# Share the updater's lock for the whole run. While an update or its rollback
# holds it, the database may be mid-migration or mid-restore: skip instead of
# waiting, and never block the updater for longer than one backup.
exec 9>"$LOCK_PATH"
lock_status=0
flock -n -E "$EXIT_SKIPPED" 9 || lock_status=$?
case "$lock_status" in
    0) ;;
    "$EXIT_SKIPPED") skip "an update or rollback holds $LOCK_PATH." ;;
    *) die "Cannot lock $LOCK_PATH (flock exit $lock_status)." ;;
esac

[ -f "$DB_PATH" ] || die "Database not found: $DB_PATH"
id "$RUN_AS" >/dev/null 2>&1 || die "Service account missing: $RUN_AS"
RUN_UID=$(id -u "$RUN_AS")
RUN_GID=$(id -g "$RUN_AS")
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "No such service: $SERVICE"

# A stopped bot means maintenance: a failed or --no-rollback update, or a manual
# restore. Its database is not a "good" daily state, and nothing new is being
# written that would need backing up.
systemctl is-active --quiet "$SERVICE" \
    || skip "$SERVICE is not running; the database may be under maintenance."
INVOCATION=$(systemctl show "$SERVICE" -p InvocationID --value)

if [ -L "$BACKUP_DIR" ]; then
    die "Backup directory is a symlink; refusing: $BACKUP_DIR"
elif [ ! -e "$BACKUP_DIR" ]; then
    install -d -m 750 -o "$RUN_UID" -g "$RUN_GID" "$BACKUP_DIR"
elif [ ! -d "$BACKUP_DIR" ]; then
    die "Backup path exists but is not a directory: $BACKUP_DIR"
fi

# --------------------------------------------------------------------------
# Backup and verification
# --------------------------------------------------------------------------

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL_NAME="$PREFIX$STAMP.db"
FINAL_PATH="$BACKUP_DIR/$FINAL_NAME"
[[ $FINAL_NAME =~ $NAME_RE ]] || die "Unexpected timestamp from date: '$STAMP'"
if [ -e "$FINAL_PATH" ] || [ -L "$FINAL_PATH" ]; then
    die "Backup already exists: $FINAL_PATH"
fi

# Hidden and without a .db suffix: never mistaken for a usable backup.
TMP_PATH="$BACKUP_DIR/.$PREFIX$STAMP-$$.incomplete"
if [ -e "$TMP_PATH" ] || [ -L "$TMP_PATH" ]; then
    TMP_PATH=""
    die "Temporary path already exists; refusing to reuse it."
fi

log "Backing up $DB_PATH..."
# SQLite's online backup includes committed WAL content and never blocks writers
# for long; a plain file copy of a WAL database can be inconsistent.
as_bot sqlite3 "$DB_PATH" ".backup '$TMP_PATH'" || die "sqlite3 .backup failed."
[ -f "$TMP_PATH" ] && [ ! -L "$TMP_PATH" ] && [ -s "$TMP_PATH" ] \
    || die "sqlite3 .backup produced no usable file."

# The copy inherits WAL mode, so every later open would need writable -wal/-shm
# side files (a -readonly open can fail outright). Make it one self-contained
# file before verifying, so what is verified is exactly what is kept. Restoring
# into the WAL-mode live database keeps that database in WAL mode.
JOURNAL=$(as_bot sqlite3 "$TMP_PATH" 'PRAGMA journal_mode=DELETE;') \
    || die "Could not switch the new backup out of WAL mode."
[ "$JOURNAL" = delete ] || die "New backup is in journal mode '$JOURNAL', expected delete."

INTEGRITY=$(as_bot sqlite3 -readonly "$TMP_PATH" 'PRAGMA integrity_check;') \
    || die "Integrity check could not run on the new backup."
[ "$INTEGRITY" = ok ] || die "New backup failed its integrity check: ${INTEGRITY%%$'\n'*}"
as_bot chmod 600 "$TMP_PATH"

# A restart or stop during the backup means someone was working on the service;
# do not label that moment as a good daily state.
systemctl is-active --quiet "$SERVICE" || skip "$SERVICE stopped during the backup; discarded it."
[ "$(systemctl show "$SERVICE" -p InvocationID --value)" = "$INVOCATION" ] \
    || skip "$SERVICE restarted during the backup; discarded it."

if [ -e "$FINAL_PATH" ] || [ -L "$FINAL_PATH" ]; then
    die "Backup appeared while writing: $FINAL_PATH"
fi
as_bot mv -- "$TMP_PATH" "$FINAL_PATH"
TMP_PATH=""
log "Backup verified: $FINAL_PATH"

# --------------------------------------------------------------------------
# Retention: only reached with a fresh verified backup in place
# --------------------------------------------------------------------------

# Keep the newest scheduled backup of each of the KEEP_DAYS most recent UTC days
# that have one; delete the rest. Counting days that *have* a backup (not days
# since now) means missed runs or a downtime never empty the directory.
prune() {
    local path name day last_day="" days=0 kept=0 deleted=0 failed=0 sorted
    local names=()

    shopt -s nullglob
    for path in "$BACKUP_DIR"/"$PREFIX"*.db; do
        name=${path##*/}
        [[ $name =~ $NAME_RE ]] || continue
        # Never follow symlinks or touch directories, even with a matching name.
        [ -f "$path" ] && [ ! -L "$path" ] || continue
        names+=("$name")
    done
    shopt -u nullglob

    sorted=$(printf '%s\n' ${names[@]+"${names[@]}"} | LC_ALL=C sort -r)
    if [ "${sorted%%$'\n'*}" != "$FINAL_NAME" ]; then
        warn "A scheduled backup newer than $FINAL_NAME exists (clock skew?)."
        warn "Nothing was pruned; check the system clock and $BACKUP_DIR."
        return 1
    fi

    while IFS= read -r name; do
        [ -n "$name" ] || continue
        day=${name:${#PREFIX}:8}
        if [ "$day" != "$last_day" ]; then
            last_day=$day
            days=$((days + 1))
            if [ "$days" -le "$KEEP_DAYS" ]; then
                kept=$((kept + 1))
                continue
            fi
        fi
        [ "$name" != "$FINAL_NAME" ] || continue
        path="$BACKUP_DIR/$name"
        [ -f "$path" ] && [ ! -L "$path" ] || continue
        if as_bot rm -f -- "$path"; then
            deleted=$((deleted + 1))
            log "Pruned $name"
        else
            warn "Could not delete $path"
            failed=1
        fi
    done <<< "$sorted"

    # Leftovers of runs that were killed mid-backup (they never counted as backups).
    shopt -s nullglob
    for path in "$BACKUP_DIR"/."$PREFIX"*.incomplete*; do
        name=${path##*/}
        [[ $name =~ $PARTIAL_RE ]] || continue
        [ -f "$path" ] && [ ! -L "$path" ] || continue
        as_bot rm -f -- "$path" || { warn "Could not delete $path"; failed=1; }
    done
    shopt -u nullglob

    log "Retention: kept $kept scheduled backup(s) over $KEEP_DAYS day(s), deleted $deleted."
    return "$failed"
}

prune || die "Retention incomplete; the new backup $FINAL_PATH is kept."

# Off-server copies: add an ExecStartPost= drop-in to the systemd unit (see
# README) rather than code here, so a failed upload never affects retention.
