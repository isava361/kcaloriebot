# KCalorieBot

KCalorieBot is a private-chat Telegram bot for recording food, tracking calories
and macronutrients, and reusing saved favorite foods. It stores data in SQLite
and assigns entries to calendar days using each user's IANA timezone.

## Requirements

- Python 3.10 or newer
- A Telegram bot token from BotFather

The Python implementation uses the standard-library SQLite driver, so it does
not require CGO or a C compiler.

## Install

Create and activate a virtual environment, then install the project:

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Set runtime configuration in the process environment. Do not put a real token
in the repository.

```powershell
$env:BOT_TOKEN = "your-telegram-bot-token"
$env:DATABASE_PATH = "$PWD\data\kcaloriebot.db" # optional
$env:LOG_LEVEL = "INFO"                          # optional
```

Run the bot:

```powershell
python -m kcaloriebot
```

`DATABASE_PATH` defaults to `data/kcaloriebot.db` under the project directory.
The directory and schema are created automatically. SQLite foreign keys, WAL,
busy timeouts, numeric constraints, and deterministic indexes are enabled at
startup.

## Usage

Start a private chat with the bot and send `/start`. The first prompt records an
IANA timezone such as `Europe/Moscow`; an unambiguous city such as `New York`
also works. Use `/updatetimezone` to change it and `/cancel` to leave any active
workflow.

The bot intentionally declines group-chat use because food history and inline
button contents are personal data.

Week and month statistics are averages across days on which at least one entry
was recorded. Unknown macronutrients remain unknown and are excluded from their
individual averages. Today, yesterday, week, and month boundaries are calculated
as local calendar boundaries and converted to UTC, including DST transitions.

## Tests

The test suite uses the standard library:

```powershell
python -m unittest discover -s tests -v
```

Install the development tools with `python -m pip install -e ".[dev]"`, then run
`python -m ruff check .` and `python -m ruff format --check .` for the same
static checks used by CI.

Tests cover validation, nutrient scaling, owner-scoped CRUD, durable sessions,
transactions, paging, foreign keys, callback parsing, and DST boundaries.

## Existing Go Database

Keep a backup of `mydb.db`, then import it into a new Python database:

```powershell
python -m kcaloriebot.migrate .\mydb.db .\data\kcaloriebot.db
```

The importer preserves user IDs, entry IDs, favorite IDs, valid nutrition data,
timestamps, and recognized timezones. It reports and skips rows that violate the
new constraints. Active Go workflow states are intentionally reset because the
old database did not contain their required draft context.
