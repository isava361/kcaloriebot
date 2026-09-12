import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType

from kcaloriebot.bot import health_callback, health_command, handle_text
from kcaloriebot.database import Database, SCHEMA_VERSION
from kcaloriebot.domain import StateConflict, ValidationError, day_bounds, local_date
from kcaloriebot.health import HealthStore, HealthUnauthorized


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = HealthStore(Path(self.directory.name) / "test.db")
        self.store.initialize()
        self.store.set_timezone(1, "Europe/Moscow")
        self.store.set_timezone(2, "UTC")
        self.token = self.store.connect(1, "2020-01-01")
        self.other_token = self.store.connect(2, "2020-01-01")
        self.epoch = 1_780_000_000

    def next(self, token=None):
        return self.store.request(token or self.token, "next", {})

    def ack(self, result):
        return self.store.request(self.token, "ack", {"receipt": result["receipt"]})

    def food(self, **kwargs):
        return self.store.add_entry(1, self.epoch, "Овсянка", 200, 50, **kwargs)

    def test_v6_migration_preserves_diary_and_is_idempotent(self):
        food = self.food()
        with sqlite3.connect(self.store.path) as conn:
            conn.executescript(
                "DROP TABLE health_samples; DROP TABLE health_connections; PRAGMA user_version=6;"
            )
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.get_entry(1, food.entry_id), food)
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertIn(
                "end_utc",
                {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(health_connections)")
                },
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_export_totals_units_dates_unknown_zero_and_owner_isolation(self):
        self.food(protein_per_100g=10, fat_per_100g=0)
        self.store.add_weight(1, self.epoch, 82.5)
        self.store.add_entry(2, self.epoch, "Private", 100, 100)
        samples = []
        while (result := self.next())["status"] == "sample":
            samples.append(result["sample"])
            self.ack(result)
        self.assertEqual(result["status"], "done")
        self.assertEqual(
            {s["type"]: (s["value"], s["unit"]) for s in samples},
            {
                "calories": (100, "kcal"),
                "protein": (5, "g"),
                "fat": (0, "g"),
                "weight": (82.5, "kg"),
            },
        )
        for sample in samples:
            self.assertEqual(
                datetime.fromisoformat(sample["date"]).timestamp(), self.epoch
            )
        self.assertEqual(self.next(self.other_token)["status"], "sample")
        self.assertEqual(self.next()["status"], "done")

    def test_default_start_is_local_midnight_and_future_is_not_exported(self):
        today = local_date(self.store.now_epoch(), "Europe/Moscow")
        midnight = day_bounds(today, "Europe/Moscow").start_utc
        self.token = self.store.connect(1, today.isoformat())
        self.store.add_entry(1, midnight - 1, "Old", 100, 1)
        current = self.store.add_entry(1, midnight, "Today", 100, 2)
        self.store.add_entry(1, self.store.now_epoch() + 3600, "Future", 100, 3)
        result = self.next()
        self.assertEqual(result["sample"]["id"], f"food:{current.entry_id}:calories")
        self.ack(result)
        self.assertEqual(self.next()["status"], "done")
        self.assertEqual(self.store.status(1)["start_utc"], midnight)
        self.store.set_timezone(3, "UTC")
        token = self.store.connect(3)
        self.assertEqual(
            self.store.status(3)["start_utc"],
            day_bounds(local_date(self.store.now_epoch(), "UTC"), "UTC").start_utc,
        )
        self.assertEqual(self.next(token)["status"], "done")

    def test_rotation_revocation_keep_window_pending_and_receipts(self):
        self.food()
        result = self.next()
        original_start = self.store.status(1)["start_utc"]
        old = self.token
        self.token = self.store.connect(1)
        with self.assertRaises(HealthUnauthorized):
            self.next(old)
        self.assertEqual(self.store.status(1)["start_utc"], original_start)
        self.assertEqual(self.next()["receipt"], result["receipt"])
        self.store.disconnect(1)
        with self.assertRaises(HealthUnauthorized):
            self.ack(result)
        self.token = self.store.connect(1)
        self.ack(result)
        self.assertEqual(self.next()["status"], "done")
        with sqlite3.connect(self.store.path) as conn:
            stored = conn.execute(
                "SELECT token_hash FROM health_connections WHERE user_id=1"
            ).fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(self.token.encode()).hexdigest())
        self.assertNotEqual(stored, self.token)

    def test_pending_survives_restart_ack_is_idempotent_and_other_owner_cannot_ack(
        self,
    ):
        self.food()
        first = self.next()
        self.store = HealthStore(self.store.path)
        self.store.initialize()
        self.assertEqual(self.next()["status"], "review")
        with self.assertRaises(StateConflict):
            self.store.request(self.other_token, "ack", {"receipt": first["receipt"]})
        self.assertEqual(self.ack(first), {"status": "acknowledged"})
        self.ack(first)
        self.assertEqual(self.next()["status"], "done")

    def test_concurrent_next_issues_only_one_write_permission(self):
        self.food()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.next(), range(2)))
        self.assertEqual(sorted(r["status"] for r in results), ["review", "sample"])
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])

    def test_retry_rejects_stale_receipt_and_reads_latest_values(self):
        food = self.food()
        first = self.next()
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE food_entries SET calories=90 WHERE entry_id=?", (food.entry_id,)
            )
        with self.assertRaises(StateConflict):
            self.store.recover(2, "retry", first["receipt"])
        self.store.recover(1, "retry", first["receipt"])
        result = self.next()
        self.assertNotEqual(first["receipt"], result["receipt"])
        self.assertEqual(result["sample"]["value"], 90)
        with self.assertRaises(StateConflict):
            self.ack(first)
        self.store.recover(1, "saved", result["receipt"])
        self.assertEqual(self.next()["status"], "done")

    def test_edits_deletions_and_stale_manual_correction(self):
        food = self.food()
        first = self.next()
        # Editing after issuance does not silently change the issued snapshot.
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE food_entries SET calories=90 WHERE entry_id=?", (food.entry_id,)
            )
        self.ack(first)
        issue = self.next()["issues"][0]
        self.assertEqual(self.next()["status"], "changed")
        self.assertEqual(issue["previous"]["value"], 100)
        self.assertEqual(issue["current"]["value"], 90)
        with self.assertRaises(StateConflict):
            self.store.corrected(2, issue["id"], issue["revision"])
        with self.store._connect() as conn:
            conn.execute("DELETE FROM food_entries WHERE entry_id=?", (food.entry_id,))
        with self.assertRaises(StateConflict):
            self.store.corrected(1, issue["id"], issue["revision"])
        issue = self.next()["issues"][0]
        self.assertIsNone(issue["current"])
        self.store.corrected(1, issue["id"], issue["revision"])
        self.assertEqual(self.next()["status"], "done")
        self.ack(first)  # An old ack cannot resurrect a deleted/corrected payload.
        self.assertEqual(self.next()["status"], "done")

    def test_newly_known_macro_and_rename_do_not_duplicate_calories(self):
        food = self.food()
        self.ack(self.next())
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE food_entries SET name='New', protein=7 WHERE entry_id=?",
                (food.entry_id,),
            )
        result = self.next()
        self.assertEqual(result["sample"]["type"], "protein")
        self.ack(result)
        self.assertEqual(self.next()["status"], "done")

    def test_status_is_read_only_and_invalid_requests_do_not_reserve(self):
        self.food()
        self.assertEqual(
            self.store.request(self.token, "status", {})["status"], "ready"
        )
        for token in ("", "x" * 43, "a" * 10000, "with spaces", None):
            with self.assertRaises(HealthUnauthorized):
                self.store.request(token, "next", {})
        for action, data in (
            ("next", {"user_id": 2}),
            ("delete", {}),
            ("ack", {"receipt": []}),
        ):
            with self.assertRaises(ValidationError):
                self.store.request(self.token, action, data)
        self.assertEqual(self.next()["status"], "sample")

    def test_invalid_connect_does_not_rotate_key(self):
        for start in ("bad", "1999-01-01", "2999-01-01"):
            with self.assertRaises(ValidationError):
                self.store.connect(1, start)
        for start, end in (
            (None, "2020-01-01"),
            ("2020-02-01", "2020-01-31"),
            ("2020-01-01", "2999-01-01"),
            ("2020-01-01", "bad"),
        ):
            with self.assertRaises(ValidationError):
                self.store.connect(1, start, end)
        with self.assertRaises(ValidationError):
            self.store.connect(99)
        self.assertEqual(self.next()["status"], "done")

    def test_closed_window_exports_only_its_own_local_days(self):
        zone = "Europe/Moscow"
        day = local_date(self.epoch, zone)
        inside = day_bounds(day, zone)
        before = self.store.add_entry(1, inside.start_utc - 1, "Раньше", 100, 1)
        first = self.store.add_entry(1, inside.start_utc, "Начало дня", 100, 2)
        last = self.store.add_entry(1, inside.end_utc - 1, "Конец дня", 100, 3)
        after = self.store.add_entry(1, inside.end_utc, "Позже", 100, 4)
        self.token = self.store.connect(1, day.isoformat(), day.isoformat())
        status = self.store.status(1)
        self.assertEqual(status["start_utc"], inside.start_utc)
        self.assertEqual(status["end_utc"], inside.end_utc)
        self.assertEqual(status["remaining"], 2)
        exported = set()
        while (result := self.next())["status"] == "sample":
            exported.add(result["sample"]["id"])
            self.ack(result)
        self.assertEqual(
            exported,
            {f"food:{first.entry_id}:calories", f"food:{last.entry_id}:calories"},
        )
        # A closed window stays closed: neighbouring days are never offered.
        self.assertEqual(self.next()["status"], "done")
        # Rotating the key keeps both bounds; a lone start date reopens the export.
        self.token = self.store.connect(1)
        self.assertEqual(self.store.status(1)["end_utc"], inside.end_utc)
        self.assertEqual(self.next()["status"], "done")
        self.token = self.store.connect(1, "2020-01-01")
        self.assertIsNone(self.store.status(1)["end_utc"])
        reopened = set()
        while (result := self.next())["status"] == "sample":
            reopened.add(result["sample"]["id"])
            self.ack(result)
        self.assertEqual(
            reopened,
            {f"food:{before.entry_id}:calories", f"food:{after.entry_id}:calories"},
        )

    def test_window_can_walk_the_history_in_chunks(self):
        zone = "Europe/Moscow"
        day = local_date(self.epoch, zone)
        earlier = day - timedelta(days=1)
        first = self.store.add_entry(
            1, day_bounds(earlier, zone).start_utc, "Вчера", 100, 1
        )
        second = self.store.add_entry(
            1, day_bounds(day, zone).start_utc, "Сегодня", 100, 2
        )
        self.token = self.store.connect(1, earlier.isoformat(), earlier.isoformat())
        result = self.next()
        self.assertEqual(result["sample"]["id"], f"food:{first.entry_id}:calories")
        self.ack(result)
        self.assertEqual(self.next()["status"], "done")
        self.token = self.store.connect(1, day.isoformat(), day.isoformat())
        result = self.next()
        self.assertEqual(result["sample"]["id"], f"food:{second.entry_id}:calories")
        self.ack(result)
        # Moving the window never re-offers what an earlier chunk confirmed.
        self.token = self.store.connect(1, earlier.isoformat(), day.isoformat())
        self.assertEqual(self.store.status(1)["confirmed"], 2)
        self.assertEqual(self.next()["status"], "done")


class HealthBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_setup_buttons_and_old_connect_preserves_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HealthStore(Path(directory) / "bot.db")
            store.initialize()
            store.set_timezone(1, "UTC")
            context = SimpleNamespace(
                application=SimpleNamespace(
                    bot_data={
                        "database": store,
                        "miniapp_url": "https://example.com:8443/",
                    }
                )
            )
            message = SimpleNamespace(
                text="Settings", reply_text=AsyncMock(), is_accessible=True
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1, type=ChatType.PRIVATE),
                effective_message=message,
                callback_query=None,
            )
            await handle_text(update, context)
            settings = message.reply_text.call_args.kwargs["reply_markup"]
            self.assertIn(
                "Apple Health",
                [button.text for row in settings.keyboard for button in row],
            )
            message.text = "Apple Health"
            await handle_text(update, context)
            panel = message.reply_text.call_args.kwargs["reply_markup"]
            buttons = [button for row in panel.inline_keyboard for button in row]
            self.assertIn(
                "health:connect", [button.callback_data for button in buttons]
            )
            self.assertIn(
                "https://example.com:8443/static/apple-health.html",
                [button.url for button in buttons],
            )
            self.assertFalse(store.status(1)["connected"])
            query = SimpleNamespace(
                data="health:connect", message=message, answer=AsyncMock()
            )
            update.callback_query = query
            await health_callback(update, context)
            self.assertTrue(store.status(1)["connected"])
            self.assertIn("Персональный ключ", message.reply_text.call_args.args[0])
            token = store.connect(1)
            await health_callback(update, context)
            self.assertNotIn("Персональный ключ", message.reply_text.call_args.args[0])
            self.assertEqual(store.request(token, "status", {})["status"], "done")
            panel = message.reply_text.call_args.kwargs["reply_markup"]
            self.assertNotIn(
                "health:connect",
                [
                    button.callback_data
                    for row in panel.inline_keyboard
                    for button in row
                ],
            )
            # A callback in a group cannot issue or rotate a private export key.
            update.effective_chat.type = ChatType.GROUP
            await health_callback(update, context)
            self.assertEqual(store.request(token, "status", {})["status"], "done")

    async def test_setup_private_recovery_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Database(Path(directory) / "bot.db")
            store.initialize()
            store.set_timezone(1, "UTC")
            context = SimpleNamespace(
                args=["connect"],
                application=SimpleNamespace(
                    bot_data={
                        "database": store,
                        "miniapp_url": "https://example.com:8443/?startapp=1",
                    }
                ),
            )
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1, type=ChatType.GROUP),
                effective_message=message,
                callback_query=None,
            )
            await health_command(update, context)
            health = HealthStore(store.path)
            self.assertFalse(health.status(1)["connected"])
            update.effective_chat.type = ChatType.PRIVATE
            await health_command(update, context)
            call = message.reply_text.call_args
            self.assertTrue(call.kwargs["disable_web_page_preview"])
            self.assertIn("https://example.com:8443/health/v1/", call.args[0])
            self.assertIn(
                "https://example.com:8443/static/apple-health.html", call.args[0]
            )
            self.assertTrue(health.status(1)["connected"])
            token = health.connect(1, "2020-01-01")
            store.add_entry(1, 1780000000, "Food", 100, 100)
            result = health.request(token, "next", {})
            context.args = []
            await health_command(update, context)
            self.assertIn(
                f"/health saved {result['receipt']}",
                message.reply_text.call_args.args[0],
            )
            context.args = ["saved", result["receipt"]]
            await health_command(update, context)
            self.assertEqual(health.request(token, "next", {})["status"], "done")
            context.args = ["disconnect"]
            await health_command(update, context)
            self.assertFalse(health.status(1)["connected"])

    async def test_missing_https_does_not_issue_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HealthStore(Path(directory) / "bot.db")
            store.initialize()
            store.set_timezone(1, "UTC")
            context = SimpleNamespace(
                args=["connect"],
                application=SimpleNamespace(bot_data={"database": store}),
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1, type=ChatType.PRIVATE),
                effective_message=SimpleNamespace(reply_text=AsyncMock()),
            )
            await health_command(update, context)
            self.assertFalse(store.status(1)["connected"])
            self.assertIn(
                "HTTPS", update.effective_message.reply_text.call_args.args[0]
            )
