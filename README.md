# KCalorieBot

KCalorieBot is a private-chat Telegram bot for recording food, tracking calories
and macronutrients, and reusing saved favorite foods. It stores data in SQLite
and assigns entries to calendar days using each user's IANA timezone.

The bot uses Telegram long polling. It does not need a public domain, TLS
certificate, Nginx, or an inbound firewall port.

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

`scripts/update.sh` performs the whole procedure below and rolls back
automatically if any step fails. Install it once, outside the repository so
that updating the checkout cannot replace the script while it is running:

```bash
sudo install -m 755 /opt/kcaloriebot/app/scripts/update.sh \
  /usr/local/sbin/kcaloriebot-update
```

Afterwards each update is one command:

```bash
sudo kcaloriebot-update
```

It refuses to run if the checkout has local changes or if the branch has
diverged from origin, stops the service before taking the backup so a rollback
cannot lose entries, verifies the backup with `PRAGMA integrity_check`, runs
the test suite before starting the service, and confirms the service stayed
running afterwards. If anything fails after the service was stopped, it
restores the previous commit and the backup and starts the service again, so a
failed update leaves a working bot. Useful flags:

```bash
sudo kcaloriebot-update --check        # report pending commits, change nothing
sudo kcaloriebot-update --no-rollback  # leave a failure in place to inspect
```

The last ten backups are kept in `/var/backups/kcaloriebot`; older ones are
pruned. Reinstall the script after an update that changes it.

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
SQLite's online backup command instead:

```bash
sudo install -d -m 750 -o kcaloriebot -g kcaloriebot /var/backups/kcaloriebot
export BACKUP_PATH="/var/backups/kcaloriebot/kcaloriebot-$(date +%F-%H%M%S).db"
sudo -u kcaloriebot sqlite3 /var/lib/kcaloriebot/kcaloriebot.db \
  ".backup '$BACKUP_PATH'" && \
test "$(sudo -u kcaloriebot sqlite3 "$BACKUP_PATH" \
  "PRAGMA integrity_check;")" = "ok" && \
sudo chmod 600 "$BACKUP_PATH"
```

Store copies outside the server and define a retention policy. To restore a
backup, stop the bot so it cannot write during the operation:

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
`/start` resumes an unfinished prompt instead of silently discarding it. Drafts
that have been inactive for seven days expire automatically.

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
weight (or serving count), the entry time — including backdating with `HH:MM`
(today, local time) or `YYYY-MM-DD HH:MM` — the name, and calories, protein,
fat, and carbs (per 100g for weighed entries, per serving for serving-based
ones). Future timestamps and dates more than a year old are rejected.

### Weight tracking

The `Weight` button or `/weight 82.5` records a body-weight measurement in
kilograms. Replies show the latest measurement, the floating average over the
last 7 local days, and the difference against the previous 7 days once enough
history exists.

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
