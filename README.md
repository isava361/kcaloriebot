# KCalorieBot

KCalorieBot is a private-chat Telegram bot for recording food, tracking calories
and macronutrients, and reusing saved favorite foods. It stores data in SQLite
and assigns entries to calendar days using each user's IANA timezone.

The bot uses Telegram long polling. It does not need a public domain, TLS
certificate, Nginx, or an inbound firewall port.

An optional Telegram Mini App adds a Russian mobile interface for the diary,
food entry and editing, recent foods, favorite search (including servings),
recipes (a dish summed from raw ingredients, logged by portion weight), daily
goals, week/month statistics, and weight tracking. It shares the bot's
SQLite database. The Mini App needs a public HTTPS address; see
[Mini App setup](docs/miniapp.md).

Apple Health export is available through an iPhone Shortcut, installed from a
published iCloud link and holding the user's own export key. Open
**Settings → Apple Health** in a private chat (or send `/health connect`) to get
a personal export key and setup instructions. It transfers nutrition and weight
through the Mini App HTTPS server, tracks confirmed samples, and pauses
interrupted or changed records for review. The export window starts at a given
day, or covers a closed range of days so history can be transferred in chunks.
See [Apple Health setup and recovery](docs/apple-health.md).

## Requirements

- Ubuntu Server 22.04 LTS or newer
- Python 3.10 or newer
- A Telegram bot token from [BotFather](https://t.me/BotFather)
- Outbound HTTPS access to the Telegram Bot API

The application uses Python's standard-library SQLite driver, so it does not
require a C compiler or a separate database server.

## Deploy on Ubuntu

The commands below install the bot under `/opt/kcaloriebot`, keep mutable data
under `/var/lib/kcaloriebot`, and run it as an unprivileged system user.

### 1. Install system packages

```bash
sudo apt update
sudo apt install -y ca-certificates git python3 python3-venv sqlite3 tzdata
```

Verify that the installed Python version is supported:

```bash
python3 --version
```

### 2. Create the service account and install the application

Create a locked service account, clone the repository, and build its virtual
environment:

```bash
sudo useradd --system --create-home \
  --home-dir /opt/kcaloriebot \
  --shell /usr/sbin/nologin \
  kcaloriebot

sudo -u kcaloriebot -H git clone \
  https://github.com/isava361/kcaloriebot.git /opt/kcaloriebot/app
sudo -u kcaloriebot -H python3 -m venv /opt/kcaloriebot/.venv
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m pip install --upgrade pip
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m pip install -e /opt/kcaloriebot/app
```

If the `kcaloriebot` account already exists, skip the `useradd` command.

### 3. Configure secrets and runtime settings

Create a root-owned environment file:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/kcaloriebot.env
sudoedit /etc/kcaloriebot.env
```

Add the following values:

```dotenv
BOT_TOKEN=replace-with-the-token-from-botfather
DATABASE_PATH=/var/lib/kcaloriebot/kcaloriebot.db
LOG_LEVEL=INFO
```

Do not commit the real token to Git. If a token is exposed, revoke it in
BotFather and generate a replacement.

`DATABASE_PATH` and `LOG_LEVEL` are optional when running manually. The service
uses an explicit database path so application updates and user data remain
separate.

`LOG_LEVEL` applies only to the application's own logger. HTTP client and
Telegram library loggers are capped at WARNING because their request logs
include the Bot API URL, which contains the token; a redaction filter
additionally replaces the token in anything that is still logged. Versions
before this protection logged such URLs at INFO: if an older version ran in
production, treat existing journals as sensitive and rotate the token in
BotFather.

### 4. Create the systemd service

Create `/etc/systemd/system/kcalculatorbot.service`:

```bash
sudoedit /etc/systemd/system/kcalculatorbot.service
```

Use this unit definition:

```ini
[Unit]
Description=KCalorieBot Telegram bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=kcaloriebot
Group=kcaloriebot
WorkingDirectory=/opt/kcaloriebot/app
EnvironmentFile=/etc/kcaloriebot.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/kcaloriebot/.venv/bin/python -m kcaloriebot
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077

StateDirectory=kcaloriebot
StateDirectoryMode=0750
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/kcaloriebot

[Install]
WantedBy=multi-user.target
```

Note the naming: only the systemd unit is called `kcalculatorbot.service`, so
`systemctl` and `journalctl` commands use `kcalculatorbot`. The service
account, the install paths, the environment file, and the Python package that
`ExecStart` runs are all `kcaloriebot` and must not be renamed to match the
unit.

Load the unit and start the bot:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kcalculatorbot
sudo systemctl status kcalculatorbot --no-pager
```

The database directory and schema are created automatically. SQLite foreign
keys, WAL mode, busy timeouts, numeric constraints, and deterministic indexes
are enabled at startup. Databases from earlier versions are migrated to the
current schema automatically on the first start; because the migration
rebuilds tables, take the pre-update backup described below before upgrading.
Each migration step re-checks the schema version under SQLite's write lock, so
the bot and the Mini App server may start at the same time.

### 5. Verify the deployment

Follow the service logs:

```bash
sudo journalctl -u kcalculatorbot -f
```

Then open a private chat with the bot and send `/start`. Stop following logs
with `Ctrl+C`; this does not stop the service.

Useful service commands:

```bash
sudo systemctl is-active kcalculatorbot
sudo systemctl restart kcalculatorbot
sudo systemctl stop kcalculatorbot
sudo journalctl -u kcalculatorbot -n 100 --no-pager
```

## Updating the Server

### Using the update script

`scripts/update.sh` performs the procedure below and attempts rollback on failure.
Install it outside the repository so
that updating the checkout cannot replace the script while it is running:

```bash
sudo install -m 755 /opt/kcaloriebot/app/scripts/update.sh \
  /usr/local/sbin/kcaloriebot-update
```

Afterwards each update is one command:

```bash
sudo kcaloriebot-update
```

It locks against concurrent updates and refuses dirty or diverged checkouts.
When the web unit is installed, it explicitly stops **both** services before
backing up SQLite, installs `.[miniapp]`, runs tests and migrates the database
before starting either writer. It checks the schema against the installed
release, verifies database integrity, and waits for three consecutive healthy
service checks (including HTTP 200 from `127.0.0.1:18081` for the Mini App).

Failure handling stops both services before rollback. Unverified backups are
never restored. If code, dependencies or database cannot be restored, services
remain stopped for manual recovery. A database snapshot taken after a failed
migration/startup is retained as `failed-before-*.db`; writes received during
that startup are preserved there, not in the restored live database. Useful flags:

```bash
sudo kcaloriebot-update --check        # fetch/report; do not deploy or stop services
sudo kcaloriebot-update --force        # reinstall/restart even if HEAD is current
sudo kcaloriebot-update --no-rollback  # leave a failure in place to inspect
```

The updater's snapshots (`before-*.db`, `failed-before-*.db`) are kept in
`/var/backups/kcaloriebot` and are never pruned automatically, neither by the
updater nor by the [scheduled backup](#scheduled-backups); delete old ones by
hand once you no longer need them. Reinstall the script after an update that
changes it. Defaults match the server
documented in [miniapp.md](docs/miniapp.md#deploy-food-server); no preceding shell
variable setup is needed. For other installations, override `KCALORIE_APP_DIR`,
`KCALORIE_PYTHON`, `KCALORIE_DATABASE`, `KCALORIE_BACKUPS`, `KCALORIE_BOT_SERVICE`,
`KCALORIE_WEB_SERVICE`, `KCALORIE_RUN_AS`, `KCALORIE_WEB_URL` or `KCALORIE_LOCK`
with `sudo env NAME=value kcaloriebot-update`. Use the same service account for
the checkout, virtualenv and DB, as in the installation instructions.

Run isolated updater failure tests with `python -m unittest tests.test_update_script`.
They execute Bash against fake server commands and temporary files, never the
live services or database.

### Updating by hand

Back up the database before an update, then stop the service, pull only
fast-forward changes, install any changed dependencies, run the tests, and
start the service again:

```bash
sudo install -d -m 750 -o kcaloriebot -g kcaloriebot /var/backups/kcaloriebot
export BACKUP_PATH="/var/backups/kcaloriebot/before-update-$(date +%F-%H%M%S).db"
sudo -u kcaloriebot sqlite3 /var/lib/kcaloriebot/kcaloriebot.db \
  ".backup '$BACKUP_PATH'" && \
test "$(sudo -u kcaloriebot sqlite3 "$BACKUP_PATH" \
  "PRAGMA integrity_check;")" = "ok" && \
sudo chmod 600 "$BACKUP_PATH" && \
sudo systemctl stop kcalculatorbot && \
sudo -u kcaloriebot -H git -C /opt/kcaloriebot/app pull --ff-only && \
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m pip install \
  -e /opt/kcaloriebot/app && \
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m unittest \
  discover -s /opt/kcaloriebot/app/tests -v && \
sudo systemctl start kcalculatorbot && \
sudo systemctl status kcalculatorbot --no-pager
```

The `&&` chain stops immediately if backup creation, integrity verification,
pulling, installing, or testing fails. A failure after `systemctl stop` leaves
the service stopped intentionally. Fix the error or restore the pre-update
backup before starting the bot. A new application version can migrate the
database schema at startup, so rolling the code back may also require restoring
that backup.

Do not run two instances with the same bot token. Telegram permits only one
long-polling consumer, and a second process will produce polling conflict errors.

## Backups and Restore

SQLite WAL mode can keep recent transactions outside the main `.db` file.
Therefore, do not copy only `kcaloriebot.db` while the service is running. Use
SQLite's online backup command instead, as both scripts below do.

### Scheduled backups

`scripts/backup.sh` takes a daily online backup while the bot and Mini App keep
running. It writes the copy under a hidden temporary name, converts it to a
single self-contained file (no `-wal`/`-shm` needed to open it), checks it with
`PRAGMA integrity_check`, makes it mode `600`, and only then renames it to
`scheduled-YYYYMMDDTHHMMSSZ.db` (UTC). Only after that does it prune.

Install it outside the checkout, like the updater, and create the backup
directory before the first run:

```bash
sudo install -m 755 /opt/kcaloriebot/app/scripts/backup.sh \
  /usr/local/sbin/kcaloriebot-backup
sudo install -d -m 750 -o kcaloriebot -g kcaloriebot /var/backups/kcaloriebot
sudo kcaloriebot-backup   # one manual run; expect "Backup verified: ..."
```

Create `/etc/systemd/system/kcaloriebot-backup.service`:

```ini
[Unit]
Description=KCalorieBot scheduled database backup
After=kcalculatorbot.service kcaloriebot-web.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kcaloriebot-backup
# 75 = skipped on purpose (update running or bot stopped); not a failure.
SuccessExitStatus=75
UMask=0077

NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/kcaloriebot /var/backups/kcaloriebot /run/lock
```

and `/etc/systemd/system/kcaloriebot-backup.timer`:

```ini
[Unit]
Description=Daily KCalorieBot database backup

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kcaloriebot-backup.timer
systemctl list-timers kcaloriebot-backup.timer
```

The service has no `User=`: it must share the updater's lock
`/run/lock/kcaloriebot-update.lock`, which belongs to root. The script itself
drops to the `kcaloriebot` account (with `setpriv`, which also works under
`NoNewPrivileges=`) for every SQLite connection and every file it creates,
renames or deletes, so backups are owned like the updater's and no root-owned
`-wal`/`-shm` files appear next to the live database. `ReadWritePaths=` must
name directories that exist, hence the `install -d` above. `Persistent=true`
runs a backup missed while the server was off soon after the next boot.
Like the bot unit, these unit files live only in this README.

Exit status 0 means a verified backup was written and retention applied. 75
means the run was skipped on purpose and nothing was written or deleted; the
reason is logged as a warning (`journalctl -u kcaloriebot-backup -p warning`):

- An update or its rollback holds the updater lock. The backup does not wait,
  because the database may be mid-migration or mid-restore. The next daily run
  backs up the result.
- `kcalculatorbot` is not running, or stopped/restarted during the backup.
  A stopped bot means maintenance — a failed or `--no-rollback` update, or a
  manual restore — so that state is not saved as a "good" daily backup.

Any other status is a failure and marks the unit failed. If the new backup
could not be written or failed verification, nothing is pruned. The script
refuses empty, relative or unusual paths (anything other than
`/`-separated `[A-Za-z0-9._@+-]` components, `.`/`..` included) and a
backup directory that is a symlink.

The backup holds the updater lock for its few seconds. An update started in
exactly that window stops at its preflight with "Another update is already
running" before touching anything; run it again.

Retention keeps the newest scheduled backup of each of the 30 most recent UTC
days that have one, and deletes the other `scheduled-*.db` files. Days without
a backup do not count, so downtime or skipped runs never empty the directory.
The backup just written is never deleted. If a newer-looking scheduled backup
exists (clock moved backwards), nothing is pruned and the run fails. Pruning
matches only regular files named exactly `scheduled-YYYYMMDDTHHMMSSZ.db`. It
never touches the updater's `before-*`/`failed-before-*` snapshots, manual
`kcaloriebot-*.db` backups, `deploy-*` directories, symlinks or anything else.

Settings use the updater's variables where they mean the same thing:
`KCALORIE_DATABASE`, `KCALORIE_BACKUPS`, `KCALORIE_RUN_AS`,
`KCALORIE_BOT_SERVICE` and `KCALORIE_LOCK`, plus `KCALORIE_BACKUP_KEEP_DAYS`
(default `30`). For the timer, set them in a drop-in
(`sudo systemctl edit kcaloriebot-backup.service`):

```ini
[Service]
Environment=KCALORIE_BACKUP_KEEP_DAYS=60
```

If you change `KCALORIE_DATABASE` or `KCALORIE_BACKUPS`, change
`ReadWritePaths=` to match. Reinstall `kcaloriebot-backup` after an update that
changes `scripts/backup.sh`. Isolated tests:
`python -m unittest tests.test_backup_script`.

Scheduled backups only protect against bad writes and mistakes, not against
losing the server. Upload copies elsewhere with a drop-in on the same unit,
for example `ExecStartPost=/usr/local/sbin/kcaloriebot-offsite`, where that
command copies the whole `/var/backups/kcaloriebot` directory (for example
`rclone copy`, not `sync`). `ExecStartPost=` also runs after a skipped run, so
make it idempotent, and remember the unit's `ProtectSystem=`/`ReadWritePaths=`
restrictions apply to it. Prefer a destination the server cannot delete from,
so a compromised or broken server cannot erase the off-site history.

### Manual backup

```bash
sudo install -d -m 750 -o kcaloriebot -g kcaloriebot /var/backups/kcaloriebot
export BACKUP_PATH="/var/backups/kcaloriebot/kcaloriebot-$(date +%F-%H%M%S).db"
sudo -u kcaloriebot sqlite3 /var/lib/kcaloriebot/kcaloriebot.db \
  ".backup '$BACKUP_PATH'" && \
test "$(sudo -u kcaloriebot sqlite3 "$BACKUP_PATH" \
  "PRAGMA integrity_check;")" = "ok" && \
sudo chmod 600 "$BACKUP_PATH"
```

Manual `kcaloriebot-*.db` backups are never pruned automatically; delete them
yourself when no longer needed.

### Restore

The newest scheduled backup is
`ls -1 /var/backups/kcaloriebot/scheduled-*.db | tail -n 1`. To restore a
backup, stop the bot so it cannot write during the operation (stopping it also
makes a scheduled backup that fires meanwhile skip):

```bash
export RESTORE_PATH="/var/backups/kcaloriebot/backup-file.db"
test "$(sudo -u kcaloriebot sqlite3 "$RESTORE_PATH" \
  "PRAGMA integrity_check;")" = "ok" && \
sudo systemctl stop kcalculatorbot && \
sudo -u kcaloriebot sqlite3 /var/lib/kcaloriebot/kcaloriebot.db \
  ".restore '$RESTORE_PATH'" && \
test "$(sudo -u kcaloriebot sqlite3 \
  /var/lib/kcaloriebot/kcaloriebot.db "PRAGMA integrity_check;")" = "ok" && \
sudo systemctl start kcalculatorbot && \
sudo systemctl status kcalculatorbot --no-pager
```

If validation or restore fails, the command chain does not start the service.
Resolve the error and check the database before starting it manually.

## Troubleshooting

- `Configuration error: BOT_TOKEN is required`: check `/etc/kcaloriebot.env`
  and its `EnvironmentFile` path in the unit.
- `Permission denied` for the database: check that `/var/lib/kcaloriebot` is
  owned by `kcaloriebot:kcaloriebot`.
- Polling conflict errors: stop any other process or server using the same bot
  token.
- The service repeatedly restarts: inspect
  `sudo journalctl -u kcalculatorbot -n 100 --no-pager`.
- Changes are not active after `git pull`: reinstall the project in the virtual
  environment and restart the service as shown in the update procedure.

Telegram updates queued while the service is down are processed after it starts
again; the application does not discard pending updates on startup.

## Usage

Start a private chat with the bot and send `/start`. The required first prompt
records an IANA timezone such as `Europe/Moscow`; an unambiguous city such as
`New York` also works. Use the `Update Timezone` menu option or
`/updatetimezone` to change it and `/cancel` to leave an active workflow.
`/start` resumes an unfinished prompt instead of silently discarding it, also
after a restart. A draft left untouched for three hours expires automatically:
the next message is then handled as a normal menu choice or quick add instead of
as an answer to the old prompt. Diary, statistics, and favorites navigation
buttons keep working while a prompt is open.

### Logging food

The fastest way to log is a single message: a name, calories per 100g, and the
serving weight in grams, for example `oatmeal 370 60` or
`буханка 250 ккал 150 г`. Units (`kcal`/`g` and their Russian forms) are
optional and may fix the value order; without units the first number is
calories and the second is grams. Optional macro tokens add protein, fat, and
carbs per 100g: `bread 250 150 p8 f3 c47` (or `б`/`ж`/`у`). The `/add` command
accepts the same format.

Every saved entry replies with a receipt: the food, amount, calories, macros,
the entry time, and today's progress, together with inline `Undo` (deletes the
entry, valid for 15 minutes), `Edit` (opens the full entry editor), and — for
named foods — `Save as favorite` buttons. Saving a favorite whose name already
exists updates that favorite's values.

The `Add Food` menu starts the step-by-step wizard instead. At the calories
step, `Per Serving` switches the draft to serving mode: calories and macros
are then entered per one serving (a whole pizza, a sandwich) without the
per-100g limits, and the amount is a serving count such as `0.5`, `1`, or `2`.
If the entered food name matches a saved favorite, the bot offers to reuse its
nutrition values and asks only for the amount; choose `Enter Manually` to type
new values. `Recent Foods` lists the latest distinct foods so a repeated meal
is two taps: pick the food, then send the amount or choose `Same as last time`.

### Logging food from a photo

When `ANTHROPIC_API_KEY` is set in `/etc/kcaloriebot.env` (plus
`ANTHROPIC_WORKSPACE_ID` if the key is not scoped to a workspace), photos sent
by the bot owner (`ADMIN_USER_ID` in `kcaloriebot/bot.py`) are analyzed by Claude
(`claude-haiku-5-5` at effort `high` by default; override with `PHOTO_MODEL`
and `PHOTO_EFFORT` — `low`, `medium`, `high`, `xhigh` or `max`). Every
recognized food is logged with an estimated weight and per-100g nutrition,
each with its own receipt and `Undo`/`Edit` buttons; a body-weight scale
photo records a weight measurement. A caption such as
`200 г` or `творог 5%` refines the estimate. For branded products or chain
dishes without a visible label, Claude may run up to three web searches per
photo ($10 per 1,000 searches on top of token costs). Photos from anyone else, or with
no key configured, are not sent anywhere.

### Favorites and servings

Favorites store nutrition per 100g by default. `To Serving` in the favorite
view converts a per-100g favorite into a per-serving one: enter the weight of
one serving and the stored values are converted. Serving favorites are logged
by serving count (including fractions like `0.5`), and `Amend` edits their
values per serving.

### Goals, editing, and statistics

`Daily Goal` stores a calorie target; every logged entry replies with today's
total and the remaining budget, and `Food Today` shows today's totals with
progress against the goal above the entry list. Choose `Remove` in the goal
prompt to clear it.

The diary is browsable by day: `Food Today` has previous/next-day arrows and a
`Today` shortcut, entry buttons show the local time of each entry, and every
day listed in week or month statistics is a button that opens that day's
diary. Backdated entries are therefore reachable for editing on their own day.

Entries can be fully corrected after logging from the entry view: the serving
weight (or serving count), the entry time — `HH:MM` (local time on the entry's
own day) or `YYYY-MM-DD HH:MM` to move it to another day — the name, and calories, protein,
fat, and carbs (per 100g for weighed entries, per serving for serving-based
ones). Future timestamps and dates more than a year old are rejected.

### Weight tracking

The `Weight` button or `/weight 82.5` records a body-weight measurement in
kilograms. Replies show the latest measurement, the floating average over the
last 7 local days, and the difference against the previous 7 days once enough
history exists.

### Exporting data

The diary page of a day and the week and month statistics each have an
`Export CSV` button that sends the food entries of exactly that day, week or
month as a file. **Settings → Export data** sends the whole history instead:
all food entries plus a second file with the weight measurements.

Food files have one row per entry (`date`, `time`, `name`, `grams`,
`servings`, `calories`, `protein`, `fat`, `carbs` — totals for that entry);
the weight file has `date`, `time`, `weight_kg`. Dates and times are local to
the profile timezone, unknown macronutrients are empty cells rather than zeros,
and the files are comma-separated UTF-8 with a byte order mark. An Excel
installation whose list separator is a semicolon (the Russian locale, for
example) needs **Data → From Text/CSV** instead of a double click.

The bot intentionally declines group-chat use because food history and inline
button contents are personal data.

Week and month statistics are a per-day breakdown of days on which at least one
entry was recorded, newest day first, paginated seven days per page with inline
`Previous`/`Next` buttons. Month statistics open on the current month and offer
inline buttons to switch to adjacent months. Each day shows its calories,
protein, fat, and carbs. Unknown macronutrients remain unknown; the bot marks a
day's nutrient as partial when only some of its entries contain it. Entries use
the timestamp of the user's Telegram message,
not a delayed processing time. Today, yesterday, week, and month boundaries are
calculated as local calendar boundaries and converted to UTC, including DST
transitions. Changing the profile timezone can therefore regroup historical
entries near midnight.

## Development and Tests

For local development on Linux or macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
```

Tests cover validation, nutrient scaling, owner-scoped CRUD, durable sessions,
transactions, paging, foreign keys, callback parsing, complete handler-level
user workflows, retry behavior, and DST boundaries.

## Existing Go Database

Keep a backup of `mydb.db`, then import it into a new Python database before
starting the service for the first time. The source file and every parent
directory must be readable by the `kcaloriebot` account:

```bash
sudo install -d -m 750 -o kcaloriebot -g kcaloriebot /var/lib/kcaloriebot
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m kcaloriebot.migrate \
  /path/to/mydb.db /var/lib/kcaloriebot/kcaloriebot.db
```

The importer preserves user IDs, entry IDs, favorite IDs, valid nutrition data,
timestamps, and recognized timezones. It reports and skips rows that violate the
new constraints. Active Go workflow states are intentionally reset because the
old database did not contain their required draft context.
