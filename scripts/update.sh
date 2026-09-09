#!/usr/bin/env bash
#
# Update KCalorieBot to the latest commit on its tracking branch.
#
# Takes a verified database backup, stops the service, fast-forwards the
# checkout, reinstalls, runs the tests, and starts the service again. If any
# step after the service was stopped fails, the previous commit and the backup
# are restored automatically and the service is started, so a failed update
# leaves a working bot rather than a broken one.
#
# Run as root:
#   kcaloriebot-update            # update
#   kcaloriebot-update --check    # report what would happen, change nothing
#   kcaloriebot-update --no-rollback   # leave the failure in place to inspect
#
set -Eeuo pipefail

APP_DIR=/opt/kcaloriebot/app
VENV_PY=/opt/kcaloriebot/.venv/bin/python
DB_PATH=/var/lib/kcaloriebot/kcaloriebot.db
BACKUP_DIR=/var/backups/kcaloriebot
SERVICE=kcalculatorbot
WEB_SERVICE=kcaloriebot-web
RUN_AS=kcaloriebot
KEEP_BACKUPS=10
EXPECTED_SCHEMA=6

CHECK_ONLY=0
ROLLBACK=1

# State the failure handler needs. SERVICE_STOPPED gates whether a failure
# should attempt recovery at all.
OLD_COMMIT=""
BACKUP_PATH=""
SERVICE_STOPPED=0
HAS_WEB=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

as_bot() { sudo -u "$RUN_AS" -H "$@"; }
git_bot() { as_bot git -C "$APP_DIR" "$@"; }

usage() {
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --check|-n) CHECK_ONLY=1 ;;
        --no-rollback) ROLLBACK=0 ;;
        --help|-h) usage ;;
        *) die "Unknown option: $arg (try --help)" ;;
    esac
done

# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------

on_error() {
    local exit_code=$?
    local line=${1:-?}
    warn "Update failed at line $line (exit $exit_code)."

    if [ "$SERVICE_STOPPED" -eq 0 ]; then
        warn "The service was never stopped; nothing to restore."
        exit "$exit_code"
    fi

    if [ "$ROLLBACK" -eq 0 ]; then
        warn "--no-rollback was given. The service is STOPPED and the checkout"
        warn "may be on the new commit. Backup: ${BACKUP_PATH:-none}"
        exit "$exit_code"
    fi

    warn "Rolling back to $OLD_COMMIT and restoring the database..."
    local rollback_ok=1

    if [ -n "$OLD_COMMIT" ]; then
        git_bot reset --hard "$OLD_COMMIT" || rollback_ok=0
        install_app || rollback_ok=0
    fi

    if [ -n "$BACKUP_PATH" ] && [ -f "$BACKUP_PATH" ]; then
        as_bot sqlite3 "$DB_PATH" ".restore '$BACKUP_PATH'" || rollback_ok=0
    fi

    start_services || rollback_ok=0

    if [ "$rollback_ok" -eq 1 ] && systemctl is-active --quiet "$SERVICE"; then
        warn "Rollback complete: the bot is running on the previous version."
        warn "Backup kept at $BACKUP_PATH"
    else
        warn "ROLLBACK FAILED — manual recovery needed."
        warn "Database backup: ${BACKUP_PATH:-none}"
        warn "Restore it with:"
        warn "  systemctl stop $SERVICE"
        warn "  sudo -u $RUN_AS sqlite3 $DB_PATH \".restore '$BACKUP_PATH'\""
        warn "  systemctl start $SERVICE"
    fi
    exit "$exit_code"
}
trap 'on_error $LINENO' ERR

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

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo $0)."

for cmd in git sqlite3 systemctl sudo; do
    command -v "$cmd" >/dev/null || die "Required command not found: $cmd"
done
[ -d "$APP_DIR/.git" ]  || die "Not a git checkout: $APP_DIR"
[ -x "$VENV_PY" ]       || die "Virtualenv python not found: $VENV_PY"
[ -f "$DB_PATH" ]       || die "Database not found: $DB_PATH"
id "$RUN_AS" >/dev/null 2>&1 || die "Service account missing: $RUN_AS"
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "No such service: $SERVICE"

if systemctl cat "$WEB_SERVICE" >/dev/null 2>&1; then
    HAS_WEB=1
    log "Mini App unit found: $WEB_SERVICE will be reinstalled, restarted and checked."
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
NEW_COMMIT=$(git_bot rev-parse "origin/$BRANCH")

if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
    log "Already up to date ($(git_bot log --oneline -1))."
    exit 0
fi

# Only fast-forward. A diverged history means the server has commits that
# origin does not, which this script must not throw away.
git_bot merge-base --is-ancestor HEAD "origin/$BRANCH" \
    || die "Local branch has diverged from origin/$BRANCH; resolve by hand."

log "Incoming commits:"
git_bot log --oneline --no-decorate "HEAD..origin/$BRANCH" | sed 's/^/    /'

if [ "$CHECK_ONLY" -eq 1 ]; then
    log "--check given: nothing was changed."
    exit 0
fi

# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------

# The service is stopped before the backup so that no entry can be written
# between the two — otherwise a rollback would silently lose those entries.
log "Stopping $SERVICE..."
systemctl stop "$SERVICE"
SERVICE_STOPPED=1

log "Backing up the database..."
install -d -m 750 -o "$RUN_AS" -g "$RUN_AS" "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/before-$(date +%F-%H%M%S)-${OLD_COMMIT:0:7}.db"
as_bot sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH'"
[ "$(as_bot sqlite3 "$BACKUP_PATH" 'PRAGMA integrity_check;')" = "ok" ] \
    || die "Backup failed its integrity check: $BACKUP_PATH"
chmod 600 "$BACKUP_PATH"
log "Backup: $BACKUP_PATH"

log "Updating the checkout..."
git_bot pull --quiet --ff-only origin "$BRANCH"

log "Installing dependencies..."
install_app

log "Running tests..."
as_bot "$VENV_PY" -m unittest discover -s "$APP_DIR/tests" -t "$APP_DIR"

log "Starting services..."
start_services

# The units restart on failure, so a crash loop can still look "activating"
# for a moment. Give them a few seconds before believing they started.
sleep 5
systemctl is-active --quiet "$SERVICE" || die "$SERVICE did not stay running."

if [ "$HAS_WEB" -eq 1 ] && ! systemctl is-active --quiet "$WEB_SERVICE"; then
    journalctl -u "$WEB_SERVICE" -n 30 --no-pager >&2 || true
    die "$WEB_SERVICE did not stay running; the Mini App would answer 502."
fi

SCHEMA=$(as_bot sqlite3 "$DB_PATH" 'PRAGMA user_version;')
[ "$SCHEMA" = "$EXPECTED_SCHEMA" ] \
    || warn "Schema version is $SCHEMA, expected $EXPECTED_SCHEMA."

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------

trap - ERR

if [ "$KEEP_BACKUPS" -gt 0 ]; then
    # shellcheck disable=SC2012  # names are ours: date + short commit, no spaces
    ls -1t "$BACKUP_DIR"/before-*.db 2>/dev/null \
        | tail -n "+$((KEEP_BACKUPS + 1))" | xargs -r rm -f
fi

RUNNING="$SERVICE"
[ "$HAS_WEB" -eq 0 ] || RUNNING="$SERVICE and $WEB_SERVICE"
log "Updated $(echo "$OLD_COMMIT" | cut -c1-7) -> $(echo "$NEW_COMMIT" | cut -c1-7), schema v$SCHEMA, $RUNNING running."
log "Backup kept at $BACKUP_PATH"
