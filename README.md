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

### 4. Create the systemd service

Create `/etc/systemd/system/kcaloriebot.service`:

```bash
sudoedit /etc/systemd/system/kcaloriebot.service
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

Load the unit and start the bot:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kcaloriebot
sudo systemctl status kcaloriebot --no-pager
```

The database directory and schema are created automatically. SQLite foreign
keys, WAL mode, busy timeouts, numeric constraints, and deterministic indexes
are enabled at startup.

### 5. Verify the deployment

Follow the service logs:

```bash
sudo journalctl -u kcaloriebot -f
```

Then open a private chat with the bot and send `/start`. Stop following logs
with `Ctrl+C`; this does not stop the service.

Useful service commands:

```bash
sudo systemctl is-active kcaloriebot
sudo systemctl restart kcaloriebot
sudo systemctl stop kcaloriebot
sudo journalctl -u kcaloriebot -n 100 --no-pager
```

## Updating the Server

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
sudo systemctl stop kcaloriebot && \
sudo -u kcaloriebot -H git -C /opt/kcaloriebot/app pull --ff-only && \
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m pip install \
  -e /opt/kcaloriebot/app && \
sudo -u kcaloriebot -H /opt/kcaloriebot/.venv/bin/python -m unittest \
  discover -s /opt/kcaloriebot/app/tests -v && \
sudo systemctl start kcaloriebot && \
sudo systemctl status kcaloriebot --no-pager
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
sudo systemctl stop kcaloriebot && \
sudo -u kcaloriebot sqlite3 /var/lib/kcaloriebot/kcaloriebot.db \
  ".restore '$RESTORE_PATH'" && \
test "$(sudo -u kcaloriebot sqlite3 \
  /var/lib/kcaloriebot/kcaloriebot.db "PRAGMA integrity_check;")" = "ok" && \
sudo systemctl start kcaloriebot && \
sudo systemctl status kcaloriebot --no-pager
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
  `sudo journalctl -u kcaloriebot -n 100 --no-pager`.
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

The bot intentionally declines group-chat use because food history and inline
button contents are personal data.

Week and month statistics are averages across days on which at least one entry
was recorded. Unknown macronutrients remain unknown and are excluded from their
individual averages; the bot marks aggregates as partial when only some entries
contain a nutrient. Entries use the timestamp of the user's Telegram message,
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
