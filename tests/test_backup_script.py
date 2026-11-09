"""Exercise the scheduled backup script with isolated fake server commands."""

import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest

from tests.test_update_script import BASH, shell_path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "scripts" / "backup.sh"
README = ROOT / "README.md"

# Only these commands are faked; mv, rm, chmod, sort and the rest are real and
# operate on the temporary directory.
MOCK = r"""#!/usr/bin/env bash
set -eu
tool=${0##*/}
printf '%s\n' "$tool $*" >> "$FIXTURE/trace"
fail() { [[ " ${SCENARIO:-} " = *" $1 "* ]]; }
case "$tool" in
id)
    case "$*" in
    -u) if fail not_root; then echo 1000; else echo 0; fi ;;
    "-u kcaloriebot") echo "$(command -p id -u)" ;;
    "-g kcaloriebot") echo "$(command -p id -g)" ;;
    kcaloriebot) : ;;
    *) exit 1 ;;
    esac ;;
setpriv)
    [[ $1 = --reuid=* && $2 = --regid=* && $3 = --init-groups && $4 = -- ]] || exit 98
    shift 4; exec "$@" ;;
flock)
    [[ $* = "-n -E 75 9" ]] || exit 97
    if fail locked; then exit 75; fi ;;
date) cat "$FIXTURE/now" ;;
install) mkdir -p "${@: -1}" ;;
systemctl)
    case "$1" in
    cat) ! fail no_unit ;;
    is-active) [[ $(cat "$FIXTURE/services") = running ]] ;;
    show) cat "$FIXTURE/invocation" ;;
    *) exit 96 ;;
    esac ;;
sqlite3)
    [[ ${1:-} != -readonly ]] || shift
    db=$1
    query=$2
    if [[ $query = .backup* ]]; then
        target=${query#*\'}; target=${target%\'}
        if fail backup; then echo partial > "$target"; exit 1; fi
        if fail empty_backup; then : > "$target"; exit 0; fi
        cp "$db" "$target"
        if fail restarted; then echo second-run > "$FIXTURE/invocation"; fi
        if fail stopped; then echo stopped > "$FIXTURE/services"; fi
    elif [[ $query = 'PRAGMA journal_mode=DELETE;' ]]; then
        [[ $db = *.incomplete ]] || exit 94  # never on the live database
        if fail journal; then echo wal; else echo delete; fi
    elif [[ $query = *integrity_check* ]]; then
        if fail corrupt; then printf '%s\n' '*** in database main ***' 'Page 4 is never used'
        else echo ok; fi
    else exit 95; fi ;;
*) exit 99 ;;
esac
"""

FAKE_COMMANDS = ("id", "setpriv", "flock", "date", "install", "systemctl", "sqlite3")
NOW = "20261110T033000Z"
NEW_BACKUP = f"scheduled-{NOW}.db"


@unittest.skipUnless(BASH, "Bash is required for backup script tests")
class BackupScriptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        commands = self.root / "bin"
        commands.mkdir()
        for name in FAKE_COMMANDS:
            file = commands / name
            file.write_text(MOCK, encoding="utf-8", newline="\n")
            file.chmod(0o755)
        (self.root / "now").write_text(NOW)
        (self.root / "services").write_text("running")
        (self.root / "invocation").write_text("first-run")
        self.database = self.root / "state" / "live.db"
        self.database.parent.mkdir()
        self.database.write_text("live-database")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.script = self.root / "backup.sh"
        self.script.write_text(
            BACKUP_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )

    def run_backup(self, scenario="", *args, **overrides):
        env = {
            **os.environ,
            "FIXTURE": shell_path(self.root),
            "SCENARIO": scenario,
            "KCALORIE_DATABASE": shell_path(self.database),
            "KCALORIE_BACKUPS": shell_path(self.backups),
            "KCALORIE_LOCK": shell_path(self.root / "update.lock"),
            "KCALORIE_BACKUP_KEEP_DAYS": "3",
            "MSYS_NO_PATHCONV": "1",
        }
        env.pop("JOURNAL_STREAM", None)
        env.update(overrides)
        result = subprocess.run(
            [
                BASH,
                "-c",
                'export PATH="$FIXTURE/bin:$PATH"; bash "$FIXTURE/backup.sh" "$@"',
                "test",
                *args,
            ],
            env=env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        trace = self.root / "trace"
        output = result.stdout + result.stderr
        return result.returncode, output, trace.read_text() if trace.exists() else ""

    def listing(self, directory=None):
        return sorted(path.name for path in (directory or self.backups).iterdir())

    def seed(self, *names):
        for name in names:
            (self.backups / name).write_text(name)

    def test_success_writes_verified_private_backup(self):
        code, output, trace = self.run_backup()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.listing(), [NEW_BACKUP])
        backup = self.backups / NEW_BACKUP
        self.assertEqual(backup.read_text(), "live-database")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        # Lock first, online backup to a temporary name, verified, then renamed.
        self.assertLess(trace.index("flock -n"), trace.index(".backup"))
        self.assertIn(f"sqlite3 {shell_path(self.database)} .backup '", trace)
        self.assertRegex(
            trace, r"\.backup '[^']*/\.scheduled-\d{8}T\d{6}Z-\d+\.incomplete'"
        )
        self.assertLess(trace.index(".backup"), trace.index("journal_mode=DELETE"))
        self.assertLess(
            trace.index("journal_mode=DELETE"), trace.index("integrity_check")
        )
        self.assertIn(".incomplete PRAGMA integrity_check", trace)
        self.assertLess(trace.index("integrity_check"), trace.index(" mv -- "))
        self.assertRegex(
            trace, r"setpriv --reuid=\d+ --regid=\d+ --init-groups -- sqlite3"
        )
        self.assertNotIn("cp ", trace)
        self.assertIn("Backup verified", output)

    def test_missing_backup_directory_is_created(self):
        self.backups = self.root / "new" / "backups"
        code, output, trace = self.run_backup()
        self.assertEqual(code, 0, output)
        self.assertIn("install -d -m 750", trace)
        self.assertEqual(self.listing(), [NEW_BACKUP])

    def test_failed_backup_or_verification_keeps_everything(self):
        old = (
            "scheduled-20200101T000000Z.db",
            "scheduled-20200102T000000Z.db",
            "scheduled-20200103T000000Z.db",
            "scheduled-20200104T000000Z.db",
        )
        for scenario in ("backup", "empty_backup", "journal", "corrupt"):
            with self.subTest(scenario=scenario):
                for path in self.backups.iterdir():
                    path.unlink()
                self.seed(*old)
                code, output, _ = self.run_backup(scenario)
                self.assertNotIn(code, (0, 75), output)
                # Nothing under a good-looking name, no leftovers, nothing pruned.
                self.assertEqual(self.listing(), sorted(old), output)
                self.assertNotIn("Backup verified", output)
        self.assertIn("integrity check", output)

    def test_retention_keeps_newest_per_day_and_only_touches_own_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        target = outside / "scheduled-20000101T000000Z.db"
        target.write_text("outside")
        (outside / "keep.txt").write_text("outside")
        foreign = (
            "before-2026-01-01-120000-abc1234-42.db",
            "failed-before-2026-01-01-120000-abc1234-42.db",
            "before-update-2026-01-01-120000.db",
            "kcaloriebot-2026-01-01-120000.db",
            "scheduled-2026-01-01.db",
            "scheduled-20200101T000000Z.db.bak",
            "scheduled-20200101T000000Z.dbx",
            "Scheduled-20200101T000000Z.db",
            "notes.txt",
        )
        self.seed(
            *foreign,
            "scheduled-20261109T120000Z.db",  # newest of day -1: kept
            "scheduled-20261109T030000Z.db",  # older same-day copy: pruned
            "scheduled-20261107T030000Z.db",  # day -3 with a backup: kept
            "scheduled-20261101T030000Z.db",  # beyond 3 days: pruned
            "scheduled-20251020T030000Z.db",  # pruned
            ".scheduled-20261001T000000Z-123.incomplete",  # stale partial: pruned
            ".scheduled-20261001T000000Z-123.incomplete-wal",
        )
        (self.backups / "scheduled-20000102T000000Z.db").mkdir()
        (self.backups / "scheduled-20000103T000000Z.db").symlink_to(target)
        (self.backups / "scheduled-20000104T000000Z.db").symlink_to(outside)

        code, output, _ = self.run_backup()
        self.assertEqual(code, 0, output)
        self.assertEqual(
            self.listing(),
            sorted(
                (
                    *foreign,
                    NEW_BACKUP,
                    "scheduled-20261109T120000Z.db",
                    "scheduled-20261107T030000Z.db",
                    "scheduled-20000102T000000Z.db",
                    "scheduled-20000103T000000Z.db",
                    "scheduled-20000104T000000Z.db",
                )
            ),
            output,
        )
        self.assertEqual(target.read_text(), "outside")
        self.assertEqual(self.listing(outside), ["keep.txt", target.name])
        self.assertIn("deleted 3", output)

    def test_keep_one_day_keeps_only_the_new_backup(self):
        self.seed("scheduled-20261110T010000Z.db", "scheduled-20261109T010000Z.db")
        code, output, _ = self.run_backup(KCALORIE_BACKUP_KEEP_DAYS="1")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.listing(), [NEW_BACKUP])

    def test_clock_skew_never_prunes(self):
        future = "scheduled-20991231T000000Z.db"
        self.seed(future, "scheduled-20000101T000000Z.db")
        code, output, _ = self.run_backup(KCALORIE_BACKUP_KEEP_DAYS="1")
        self.assertNotIn(code, (0, 75), output)
        self.assertEqual(
            self.listing(),
            sorted((future, "scheduled-20000101T000000Z.db", NEW_BACKUP)),
        )
        self.assertIn("clock", output)

    def test_skips_are_clean_and_distinct(self):
        old = ("scheduled-20200101T000000Z.db", "scheduled-20200102T000000Z.db")
        for scenario in ("locked", "stopped_before", "restarted", "stopped"):
            with self.subTest(scenario=scenario):
                for path in self.backups.iterdir():
                    path.unlink()
                self.seed(*old)
                (self.root / "trace").unlink(missing_ok=True)
                (self.root / "invocation").write_text("first-run")
                (self.root / "services").write_text(
                    "stopped" if scenario == "stopped_before" else "running"
                )
                code, output, trace = self.run_backup(scenario)
                self.assertEqual(code, 75, output)
                self.assertIn("Backup skipped", output)
                self.assertEqual(self.listing(), sorted(old))
                self.assertNotRegex(trace, r"rm -f -- \S*/scheduled-")
                if scenario in ("locked", "stopped_before"):
                    self.assertNotIn("sqlite3", trace)

    def test_lock_skip_happens_before_any_database_access(self):
        code, output, trace = self.run_backup("locked")
        self.assertEqual(code, 75, output)
        self.assertIn("holds", output)
        self.assertEqual(trace.splitlines()[-1], "flock -n -E 75 9")

    def test_journal_priority_prefixes(self):
        code, output, _ = self.run_backup("locked", JOURNAL_STREAM="8:1234")
        self.assertEqual(code, 75)
        self.assertIn("<4>WARNING: Backup skipped", output)

    def test_odd_configuration_is_refused_before_anything_runs(self):
        cases = [
            {"KCALORIE_BACKUPS": ""},
            {"KCALORIE_BACKUPS": "/"},
            {"KCALORIE_BACKUPS": "//"},
            {"KCALORIE_BACKUPS": "backups"},
            {"KCALORIE_BACKUPS": shell_path(self.backups) + "/"},
            {"KCALORIE_BACKUPS": shell_path(self.backups) + "/../backups"},
            {"KCALORIE_BACKUPS": shell_path(self.backups) + "/."},
            {"KCALORIE_BACKUPS": shell_path(self.root) + "//backups"},
            {"KCALORIE_BACKUPS": shell_path(self.root) + "/back ups"},
            {"KCALORIE_BACKUPS": shell_path(self.root) + "/back'ups"},
            {"KCALORIE_BACKUPS": shell_path(self.root) + "/back*"},
            {"KCALORIE_BACKUPS": shell_path(self.root) + "/b\nackups"},
            {"KCALORIE_DATABASE": ""},
            {"KCALORIE_LOCK": ""},
            {"KCALORIE_RUN_AS": ""},
            {"KCALORIE_RUN_AS": "-u root"},
            {"KCALORIE_BOT_SERVICE": "bot; rm"},
            {"KCALORIE_BACKUP_KEEP_DAYS": ""},
            {"KCALORIE_BACKUP_KEEP_DAYS": "0"},
            {"KCALORIE_BACKUP_KEEP_DAYS": "-1"},
            {"KCALORIE_BACKUP_KEEP_DAYS": "07"},
            {"KCALORIE_BACKUP_KEEP_DAYS": "1e3"},
            {"KCALORIE_BACKUP_KEEP_DAYS": "10000"},
            {"KCALORIE_BACKUP_KEEP_DAYS": "3 days"},
        ]
        self.seed("scheduled-20000101T000000Z.db", "scheduled-20000102T000000Z.db")
        for overrides in cases:
            with self.subTest(overrides=overrides):
                (self.root / "trace").unlink(missing_ok=True)
                code, output, trace = self.run_backup(**overrides)
                self.assertNotIn(code, (0, 75), output)
                self.assertIn("ERROR", output)
                self.assertNotIn("flock", trace)
                self.assertNotIn("sqlite3", trace)
                self.assertEqual(
                    self.listing(),
                    ["scheduled-20000101T000000Z.db", "scheduled-20000102T000000Z.db"],
                )

    def test_symlinked_or_non_directory_backup_path_is_refused(self):
        real = self.root / "real-backups"
        real.mkdir()
        (real / "scheduled-20000101T000000Z.db").write_text("x")
        link = self.root / "linked-backups"
        link.symlink_to(real)
        plain_file = self.root / "plain-file"
        plain_file.write_text("x")
        for path in (link, plain_file):
            with self.subTest(path=path.name):
                (self.root / "trace").unlink(missing_ok=True)
                # shell_path() resolves symlinks; keep the link itself.
                value = shell_path(path.parent) + "/" + path.name
                code, output, trace = self.run_backup(KCALORIE_BACKUPS=value)
                self.assertNotIn(code, (0, 75), output)
                self.assertNotIn("sqlite3", trace)
        self.assertEqual(self.listing(real), ["scheduled-20000101T000000Z.db"])
        self.assertEqual(plain_file.read_text(), "x")

    def test_environment_and_usage_errors(self):
        for scenario, args in (("not_root", ()), ("no_unit", ()), ("", ("--bogus",))):
            with self.subTest(scenario=scenario, args=args):
                code, output, trace = self.run_backup(scenario, *args)
                self.assertNotIn(code, (0, 75), output)
                self.assertNotIn("sqlite3", trace)
        code, output, _ = self.run_backup("", "--help")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.listing(), [])

    def test_readme_units_match_the_script(self):
        readme = README.read_text(encoding="utf-8")
        service = re.search(
            r"```ini\n(\[Unit\]\nDescription=KCalorieBot scheduled database backup\n.*?)```",
            readme,
            re.S,
        )
        timer = re.search(
            r"```ini\n(\[Unit\]\nDescription=Daily KCalorieBot database backup\n.*?)```",
            readme,
            re.S,
        )
        self.assertIsNotNone(service, "backup .service block missing from README")
        self.assertIsNotNone(timer, "backup .timer block missing from README")
        service, timer = service.group(1), timer.group(1)
        script = BACKUP_SCRIPT.read_text(encoding="utf-8")
        skipped = re.search(r"^EXIT_SKIPPED=(\d+)$", script, re.M).group(1)
        self.assertIn(f"SuccessExitStatus={skipped}\n", service)
        self.assertIn("Type=oneshot\n", service)
        self.assertIn("ExecStart=/usr/local/sbin/kcaloriebot-backup\n", service)
        self.assertNotIn("User=", service)  # needs root for the updater's lock
        for path in ("/var/lib/kcaloriebot", "/var/backups/kcaloriebot", "/run/lock"):
            self.assertIn(
                path, re.search(r"^ReadWritePaths=(.*)$", service, re.M).group(1)
            )
        self.assertIn("Persistent=true\n", timer)
        self.assertIn("OnCalendar=", timer)
        self.assertIn("WantedBy=timers.target", timer)
