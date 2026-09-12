"""Exercise the actual Bash updater with isolated fake server commands."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

UPDATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update.sh"

BASH = shutil.which("bash")
if not BASH and os.name == "nt":
    candidate = Path("C:/Program Files/Git/bin/bash.exe")
    if candidate.exists():
        BASH = str(candidate)


def shell_path(path):
    value = Path(path).resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


MOCK = r"""#!/usr/bin/env bash
set -eu
tool=${0##*/}
printf '%s\n' "$tool $*" >> "$FIXTURE/trace"
fail() { [[ ${SCENARIO:-} = "$1" ]]; }
new_code() { [[ $(cat "$FIXTURE/head") = new ]]; }
case "$tool" in
id) if [[ ${1:-} = -u ]]; then echo 0; fi ;;
sudo) shift 3; exec "$@" ;;
flock) ! fail locked ;;
sleep|chmod) : ;;
install) mkdir -p "${@: -1}" ;;
systemctl)
    case "$1" in
    cat) : ;;
    show)
        case "$*" in
        *LoadState*) if fail bot_only; then echo not-found; elif fail masked; then echo masked; else echo loaded; fi ;;
        *MainPID*) if [[ $(cat "$FIXTURE/services") = stopped ]]; then echo 0; else echo 123; fi ;;
        *) if [[ $(cat "$FIXTURE/services") = stopped ]]; then echo inactive; else echo active; fi ;;
        esac ;;
    stop) if fail stop; then exit 1; fi; echo stopped > "$FIXTURE/services" ;;
    start) echo running > "$FIXTURE/services"; if new_code && fail start; then exit 1; fi ;;
    is-active) [[ $(cat "$FIXTURE/services") = running ]] ;;
    esac ;;
git)
    shift 2
    case "$1" in
    status) if fail dirty; then echo ' M user-change'; fi ;;
    rev-parse)
        if [[ $* = *abbrev-ref* ]]; then echo main
        elif [[ $2 = HEAD ]]; then cat "$FIXTURE/head"
        elif fail unchanged; then echo old
        else echo new; fi ;;
    fetch) : ;;
    merge-base) ! fail diverged ;;
    log) echo incoming ;;
    merge) echo new > "$FIXTURE/head" ;;
    reset) if fail reset; then exit 1; fi; echo old > "$FIXTURE/head" ;;
    esac ;;
python)
    if [[ $* = *'pip install'* ]]; then
        if new_code && fail install; then exit 1; fi
        if ! new_code && fail rollback_install; then exit 1; fi
    elif [[ $* = *unittest* ]]; then
        if fail tests || fail reset || fail rollback_install; then exit 1; fi
    elif [[ $* = *SCHEMA_VERSION* ]]; then
        if fail schema_import; then exit 1; fi
        echo 7
    elif [[ $* = *initialize* ]]; then
        echo new-database > "$KCALORIE_DATABASE"
        if fail migration; then exit 1; fi
    fi ;;
sqlite3)
    [[ ${1:-} != -readonly ]] || shift
    db=$1
    query=$2
    if [[ $query = .backup* ]]; then
        [[ $(cat "$FIXTURE/services") = stopped ]] || exit 90
        if fail backup; then exit 1; fi
        target=${query#*\'}; target=${target%\'}
        cp "$db" "$target"
    elif [[ $query = .restore* ]]; then
        [[ $(cat "$FIXTURE/services") = stopped ]] || exit 91
        if fail restore; then exit 1; fi
        source=${query#*\'}; source=${source%\'}
        cp "$source" "$db"
    elif [[ $query = *integrity_check* ]]; then
        if fail invalid_backup && [[ $db = *before-* ]]; then echo damaged; else echo ok; fi
    elif [[ $query = *user_version* ]]; then
        if fail schema; then echo 6; else echo 7; fi
    fi ;;
curl)
    if new_code && { fail http || fail restore; }; then echo 503
    elif fail slow && [[ ! -f "$FIXTURE/slow-once" ]]; then touch "$FIXTURE/slow-once"; exit 7
    else echo 200; fi ;;
*) exit 99 ;;
esac
"""


@unittest.skipUnless(BASH, "Bash is required for updater tests")
class UpdateScriptTests(unittest.TestCase):
    def run_update(self, scenario="", *args):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            (app / ".git").mkdir(parents=True)
            commands = root / "bin"
            commands.mkdir()
            for name in (
                "id",
                "sudo",
                "flock",
                "sleep",
                "chmod",
                "install",
                "systemctl",
                "git",
                "python",
                "sqlite3",
                "curl",
            ):
                file = commands / name
                file.write_text(MOCK, encoding="utf-8", newline="\n")
                file.chmod(0o755)
            (root / "head").write_text("old")
            (root / "services").write_text("running")
            database = root / "live.db"
            database.write_text("old-database")
            # A copy also makes the test immune to Windows checkout line endings.
            script = root / "update.sh"
            script.write_text(
                UPDATE_SCRIPT.read_text(encoding="utf-8"),
                encoding="utf-8",
                newline="\n",
            )
            env = {
                **os.environ,
                "FIXTURE": shell_path(root),
                "SCENARIO": scenario,
                "KCALORIE_APP_DIR": shell_path(app),
                "KCALORIE_PYTHON": shell_path(commands / "python"),
                "KCALORIE_DATABASE": shell_path(database),
                "KCALORIE_BACKUPS": shell_path(root / "backups"),
                "KCALORIE_LOCK": shell_path(root / "update.lock"),
                "MSYS_NO_PATHCONV": "1",
            }
            # Bash prepends POSIX paths; do not put a POSIX PATH into Windows env.
            result = subprocess.run(
                [
                    BASH,
                    "-c",
                    'export PATH="$FIXTURE/bin:$PATH"; bash "$FIXTURE/update.sh" "$@"',
                    "test",
                    *args,
                ],
                env=env,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "code": result.returncode,
                "output": result.stdout + result.stderr,
                "trace": (root / "trace").read_text()
                if (root / "trace").exists()
                else "",
                "head": (root / "head").read_text().strip(),
                "services": (root / "services").read_text().strip(),
                "db": database.read_text().strip(),
            }

    def test_fixture_works_outside_checkout(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                result = self.run_update("", "--check")
            finally:
                os.chdir(previous)
        self.assertEqual(result["code"], 0, result["output"])
        self.assertNotIn("systemctl stop", result["trace"])

    def test_success_stops_both_and_migrates_before_start(self):
        result = self.run_update("slow")
        self.assertEqual(result["code"], 0, result["output"])
        trace = result["trace"]
        self.assertIn("systemctl stop kcalculatorbot kcaloriebot-web", trace)
        self.assertLess(trace.index("systemctl stop"), trace.index(".backup"))
        self.assertLess(trace.index("initialize()"), trace.index("systemctl start"))
        self.assertIn("[miniapp]", trace)
        self.assertEqual(result["head"], "new")
        self.assertGreaterEqual(trace.count("curl "), 4)

    def test_failure_paths_recover_old_code_and_database(self):
        for scenario in (
            "backup",
            "invalid_backup",
            "install",
            "tests",
            "migration",
            "schema",
            "schema_import",
            "start",
            "http",
        ):
            with self.subTest(scenario=scenario):
                result = self.run_update(scenario)
                self.assertNotEqual(result["code"], 0)
                self.assertIn("Rollback complete", result["output"], result["output"])
                self.assertEqual(result["head"], "old")
                self.assertEqual(result["db"], "old-database")
                self.assertEqual(result["services"], "running")
                self.assertNotIn("reset --hard", result["trace"])
                self.assertLessEqual(
                    result["trace"].count(".restore"), 2
                )  # sudo + executable

    def test_failed_recovery_does_not_start_partial_installation(self):
        for scenario in ("reset", "rollback_install", "restore"):
            with self.subTest(scenario=scenario):
                result = self.run_update(scenario)
                self.assertNotEqual(result["code"], 0)
                self.assertEqual(result["services"], "stopped", result["output"])
                self.assertIn("ROLLBACK FAILED", result["output"])

    def test_preflight_and_check_never_stop_services(self):
        for scenario, args in (
            ("dirty", ()),
            ("diverged", ()),
            ("locked", ()),
            ("masked", ()),
            ("", ("--check",)),
            ("unchanged", ()),
        ):
            with self.subTest(scenario=scenario, args=args):
                result = self.run_update(scenario, *args)
                self.assertNotIn("systemctl stop", result["trace"])
                self.assertEqual(result["db"], "old-database")

    def test_no_rollback_stops_services_and_retains_new_database(self):
        result = self.run_update("http", "--no-rollback")
        self.assertNotEqual(result["code"], 0)
        self.assertEqual(result["services"], "stopped")
        self.assertEqual(result["db"], "new-database")
        self.assertNotIn(".restore", result["trace"])

    def test_stop_failure_cannot_modify_code_or_database(self):
        result = self.run_update("stop")
        self.assertNotEqual(result["code"], 0)
        self.assertEqual(result["db"], "old-database")
        self.assertEqual(result["head"], "old")
        self.assertNotIn(".backup", result["trace"])
        self.assertNotIn(".restore", result["trace"])
        self.assertNotIn("systemctl start", result["trace"])

    def test_force_and_bot_only(self):
        for scenario, args in (("unchanged", ("--force",)), ("bot_only", ())):
            result = self.run_update(scenario, *args)
            self.assertEqual(result["code"], 0, result["output"])
            self.assertIn("systemctl stop", result["trace"])
            if scenario == "bot_only":
                self.assertNotIn("[miniapp]", result["trace"])
                self.assertNotIn("curl ", result["trace"])
