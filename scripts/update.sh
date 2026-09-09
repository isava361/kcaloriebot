#!/usr/bin/env bash
#
# Update KCalorieBot to the latest commit on its tracking branch.
#
# Stops both writers, takes a verified database backup, fast-forwards the
# checkout, reinstalls, runs the tests, and starts the service again. If any
# step after stopping fails, attempt to restore the previous code and database.
# If recovery fails, leave both services stopped for manual recovery.
#
# Run as root:
#   kcaloriebot-update            # update
#   kcaloriebot-update --check    # report what would happen, change nothing
#   kcaloriebot-update --force    # reinstall/restart the current commit
#   kcaloriebot-update --no-rollback   # leave the failure in place to inspect
#
set -Eeuo pipefail
umask 077

APP_DIR=${KCALORIE_APP_DIR:-/opt/kcaloriebot/app}
VENV_PY=${KCALORIE_PYTHON:-/opt/kcaloriebot/.venv/bin/python}
DB_PATH=${KCALORIE_DATABASE:-/var/lib/kcaloriebot/kcaloriebot.db}
BACKUP_DIR=${KCALORIE_BACKUPS:-/var/backups/kcaloriebot}
SERVICE=${KCALORIE_BOT_SERVICE:-kcalculatorbot}
WEB_SERVICE=${KCALORIE_WEB_SERVICE:-kcaloriebot-web}
RUN_AS=${KCALORIE_RUN_AS:-kcaloriebot}
WEB_URL=${KCALORIE_WEB_URL:-http://127.0.0.1:18081/}
LOCK_PATH=${KCALORIE_LOCK:-/run/lock/kcaloriebot-update.lock}

CHECK_ONLY=0
ROLLBACK=1
FORCE=0

# State the failure handler needs. SERVICE_STOPPED gates whether a failure
# should attempt recovery at all.
OLD_COMMIT=""
BACKUP_PATH=""
SERVICE_STOPPED=0
HAS_WEB=0
BACKUP_VALID=0
CODE_CHANGED=0
INSTALL_ATTEMPTED=0
DB_CHANGED=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; return 1; }

as_bot() { sudo -u "$RUN_AS" -H "$@"; }
git_bot() { as_bot git -C "$APP_DIR" "$@"; }

usage() {
    printf '%s\n' 'Usage: kcaloriebot-update [--check] [--force] [--no-rollback]' \
        '--check: fetch and report pending commits; leave services/code/DB unchanged.' \
        '--force: reinstall and restart even if the checkout is already current.' \
        '--no-rollback: stop both services on failure and leave files for inspection.'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --check|-n) CHECK_ONLY=1 ;;
        --force) FORCE=1 ;;
        --no-rollback) ROLLBACK=0 ;;
        --help|-h) usage ;;
        *) die "Unknown option: $arg (try --help)" ;;
    esac
done

# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------

on_error() {
    local exit_code=${2:-$?}
    local line=${1:-?}
    # A failing command substitution must let the parent handle recovery once.
    if [ "$BASH_SUBSHELL" -gt 0 ]; then exit "$exit_code"; fi
    trap - ERR INT TERM
    set +e
    warn "Update failed at line $line (exit $exit_code)."

    if [ "$SERVICE_STOPPED" -eq 0 ]; then
        warn "The service was never stopped; nothing to restore."
        exit "$exit_code"
    fi

    if [ "$ROLLBACK" -eq 0 ]; then
        stop_services || warn "Could not stop all services; inspect systemd."
        warn "--no-rollback was given. The service is STOPPED and the checkout"
        warn "may be on the new commit. Backup: ${BACKUP_PATH:-none}"
        exit "$exit_code"
    fi

    warn "Rolling back to $OLD_COMMIT..."
    if recover; then
        warn "Rollback complete: both configured services passed health checks."
        warn "Backup kept at $BACKUP_PATH"
    else
        stop_services || warn "Could not stop all services; inspect systemd."
        warn "ROLLBACK FAILED — manual recovery needed."
        warn "Database backup: ${BACKUP_PATH:-none}"
        warn "Restore it with:"
        warn "Keep $SERVICE and $WEB_SERVICE stopped until code, dependencies and DB are restored."
        warn "Previous commit: $OLD_COMMIT"
    fi
    exit "$exit_code"
}
trap 'on_error $LINENO' ERR
trap 'on_error "$LINENO" 130' INT
trap 'on_error "$LINENO" 143' TERM

# The Mini App needs the aiohttp extra; a bot-only server must not pull it in.
install_app() {
    if [ "$HAS_WEB" -eq 1 ]; then
        as_bot "$VENV_PY" -m pip install -q -e "$APP_DIR[miniapp]"
    else
        as_bot "$VENV_PY" -m pip install -q -e "$APP_DIR"
    fi
}

# PartOf= propagates stop and restart to the web unit but never start, so the
# Mini App has to be started explicitly or Nginx keeps answering 502.
start_services() {
    systemctl start "$SERVICE" || return 1
    [ "$HAS_WEB" -eq 0 ] || systemctl start "$WEB_SERVICE" || return 1
}

stop_services() {
    local unit state
    local units=("$SERVICE")
    [ "$HAS_WEB" -eq 0 ] || units+=("$WEB_SERVICE")
    systemctl stop "${units[@]}" || return 1
    for unit in "${units[@]}"; do
        state=$(systemctl show "$unit" -p ActiveState --value) || return 1
        case "$state" in inactive|failed) ;; *) return 1 ;; esac
        [ "$(systemctl show "$unit" -p MainPID --value)" = 0 ] || return 1
    done
}

check_services() {
    local attempt good status stable=0
    for ((attempt=0; attempt<20; attempt++)); do
        good=1
        systemctl is-active --quiet "$SERVICE" || good=0
        if [ "$HAS_WEB" -eq 1 ]; then
            systemctl is-active --quiet "$WEB_SERVICE" || good=0
            status=$(curl --silent --max-time 2 --output /dev/null --write-out '%{http_code}' "$WEB_URL") || status=000
            [ "$status" = 200 ] || good=0
        fi
        if [ "$good" -eq 1 ]; then stable=$((stable + 1)); else stable=0; fi
        [ "$stable" -lt 3 ] || return 0
        sleep 1
    done
    return 1
}

recover() {
    # Errexit is disabled here. Never start a partially restored installation.
    stop_services || return 1
    if [ "$CODE_CHANGED" -eq 1 ]; then
        git_bot reset --keep "$OLD_COMMIT" || return 1
    fi
    if [ "$INSTALL_ATTEMPTED" -eq 1 ]; then
        install_app || return 1
    fi
    if [ "$DB_CHANGED" -eq 1 ]; then
        [ "$BACKUP_VALID" -eq 1 ] || return 1
        # Preserve writes that could have arrived during the attempted startup.
        local failed_path="$BACKUP_DIR/failed-${BACKUP_PATH##*/}"
        as_bot sqlite3 "$DB_PATH" ".backup '$failed_path'" || return 1
        [ "$(as_bot sqlite3 -readonly "$failed_path" 'PRAGMA integrity_check;')" = ok ] || return 1
        [ "$(as_bot sqlite3 -readonly "$BACKUP_PATH" 'PRAGMA integrity_check;')" = ok ] || return 1
        as_bot sqlite3 "$DB_PATH" ".restore '$BACKUP_PATH'" || return 1
        [ "$(as_bot sqlite3 -readonly "$DB_PATH" 'PRAGMA integrity_check;')" = ok ] || return 1
        warn "Post-update database retained at $failed_path; live data restored to before the update."
    fi
    start_services || return 1
    check_services || return 1
}

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo $0)."

for cmd in git sqlite3 systemctl sudo flock; do
    command -v "$cmd" >/dev/null || die "Required command not found: $cmd"
done
exec 9>"$LOCK_PATH"
flock -n 9 || die "Another update is already running."
[ -d "$APP_DIR/.git" ]  || die "Not a git checkout: $APP_DIR"
[ -x "$VENV_PY" ]       || die "Virtualenv python not found: $VENV_PY"
[ -f "$DB_PATH" ]       || die "Database not found: $DB_PATH"
id "$RUN_AS" >/dev/null 2>&1 || die "Service account missing: $RUN_AS"
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "No such service: $SERVICE"
case "$DB_PATH$BACKUP_DIR" in *"'"*|*$'\n'*) die "Unsupported quote/newline in database or backup path." ;; esac
cd "$APP_DIR"

WEB_STATE=$(systemctl show "$WEB_SERVICE" -p LoadState --value) || {
    [ "$WEB_STATE" = not-found ] || die "Cannot inspect $WEB_SERVICE."
}
if [ "$WEB_STATE" = loaded ]; then
    HAS_WEB=1
    command -v curl >/dev/null || die "Required command not found: curl"
    log "Mini App unit found: $WEB_SERVICE will be reinstalled, restarted and checked."
elif [ "$WEB_STATE" != not-found ]; then
    die "$WEB_SERVICE is $WEB_STATE; fix its configuration before updating."
fi

# A dirty checkout means someone edited code on the server. Fast-forwarding
# over it would silently discard their work, so refuse instead.
if [ -n "$(git_bot status --porcelain)" ]; then
    git_bot status --short
    die "The checkout has local changes. Commit, stash, or discard them first."
fi

BRANCH=$(git_bot rev-parse --abbrev-ref HEAD)
[ "$BRANCH" != "HEAD" ] || die "Detached HEAD; check out a branch first."

log "Fetching origin/$BRANCH..."
git_bot fetch --quiet origin "$BRANCH"

OLD_COMMIT=$(git_bot rev-parse HEAD)
NEW_COMMIT=$(git_bot rev-parse FETCH_HEAD)

if [ "$OLD_COMMIT" = "$NEW_COMMIT" ] && [ "$FORCE" -eq 0 ]; then
    log "Already up to date ($(git_bot log --oneline -1))."
    exit 0
fi

# Only fast-forward. A diverged history means the server has commits that
# origin does not, which this script must not throw away.
git_bot merge-base --is-ancestor HEAD "$NEW_COMMIT" \
    || die "Local branch has diverged from origin/$BRANCH; resolve by hand."

log "Incoming commits:"
git_bot log --oneline --no-decorate "HEAD..$NEW_COMMIT" | sed 's/^/    /'

if [ "$CHECK_ONLY" -eq 1 ]; then
    log "--check given: nothing was changed."
    exit 0
fi

# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------

# The service is stopped before the backup so that no entry can be written
# between the two — otherwise a rollback would silently lose those entries.
log "Stopping all configured writers..."
SERVICE_STOPPED=1
stop_services

log "Backing up the database..."
install -d -m 750 -o "$RUN_AS" -g "$RUN_AS" "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/before-$(date +%F-%H%M%S)-${OLD_COMMIT:0:7}-$$.db"
as_bot sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH'"
[ "$(as_bot sqlite3 -readonly "$BACKUP_PATH" 'PRAGMA integrity_check;')" = "ok" ] \
    || die "Backup failed its integrity check: $BACKUP_PATH"
chmod 600 "$BACKUP_PATH"
BACKUP_VALID=1
log "Backup: $BACKUP_PATH"

log "Updating the checkout..."
CODE_CHANGED=1
git_bot merge --ff-only "$NEW_COMMIT"

log "Installing dependencies..."
INSTALL_ATTEMPTED=1
install_app

log "Running tests..."
as_bot "$VENV_PY" -m unittest discover -s "$APP_DIR/tests" -t "$APP_DIR"

log "Migrating the database before either service starts..."
DB_CHANGED=1
as_bot "$VENV_PY" -c 'import sys; from kcaloriebot.database import Database; Database(sys.argv[1]).initialize()' "$DB_PATH"
EXPECTED_SCHEMA=$(as_bot "$VENV_PY" -c 'from kcaloriebot.database import SCHEMA_VERSION; print(SCHEMA_VERSION)')
SCHEMA=$(as_bot sqlite3 -readonly "$DB_PATH" 'PRAGMA user_version;')
[ "$SCHEMA" = "$EXPECTED_SCHEMA" ] || die "Schema version is $SCHEMA, expected $EXPECTED_SCHEMA."
[ "$(as_bot sqlite3 -readonly "$DB_PATH" 'PRAGMA integrity_check;')" = ok ] || die "Database integrity check failed."

log "Starting services..."
start_services

# The units restart on failure, so a crash loop can still look "activating"
# for a moment. Give them a few seconds before believing they started.
check_services || die "Services did not become healthy; inspect their journals."

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------

trap - ERR INT TERM
# Keep recovery copies; cleanup must not remove evidence of a failed deployment.

RUNNING="$SERVICE"
[ "$HAS_WEB" -eq 0 ] || RUNNING="$SERVICE and $WEB_SERVICE"
log "Updated $(echo "$OLD_COMMIT" | cut -c1-7) -> $(echo "$NEW_COMMIT" | cut -c1-7), schema v$SCHEMA, $RUNNING running."
log "Backup kept at $BACKUP_PATH"
