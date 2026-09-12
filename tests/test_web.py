import hashlib
import asyncio
import hmac
import importlib.util
import json
import logging
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from pathlib import Path
from urllib.parse import urlencode

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("Install .[miniapp] to test the Mini App")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kcaloriebot.config import Settings
from kcaloriebot.database import Database
from kcaloriebot.health import HealthStore
from kcaloriebot.domain import SessionState
from kcaloriebot.domain import local_datetime
from kcaloriebot.web_store import entry_data
from kcaloriebot.web import authenticate, build_web_app

TOKEN = "test-only-token"


def signed(user_id=123, auth_date=None, **extra):
    data = {
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        **extra,
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class AuthTests(unittest.TestCase):
    def test_valid_signature_including_optional_fields(self):
        self.assertEqual(
            authenticate(signed(auth_date=100, signature="extra"), TOKEN, 100), 123
        )

    def test_tampered_expired_future_duplicate_and_invalid_ids(self):
        for raw in (
            "",
            signed(auth_date=100).replace("123", "124"),
            signed(auth_date=100) + "&auth_date=100",
            signed(auth_date=-4000),
            signed(auth_date=131),
            signed(user_id=True, auth_date=100),
            signed(user_id="123", auth_date=100),
            signed(user_id=2**64, auth_date=100),
        ):
            with self.subTest(raw=raw), self.assertRaises(web.HTTPUnauthorized):
                authenticate(raw, TOKEN, 100)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_authentication_is_separate_and_revocable(self):
        self.store.set_timezone(123, "UTC")
        health = HealthStore(self.store.path)
        token = health.connect(123)
        headers = {"Authorization": "Bearer " + token}
        for method, path in (
            ("GET", "/health/v1/status"),
            ("POST", "/health/v1/next"),
            ("POST", "/health/v1/ack"),
        ):
            for invalid in ({}, self.headers, {"Authorization": "Bearer invalid"}):
                response = await self.client.request(
                    method, path, headers=invalid, json={}
                )
                self.assertEqual(response.status, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.client.get("/api/diary", headers=headers)
        self.assertEqual(response.status, 401)
        response = await self.client.get("/health/v1/status", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await response.json())["status"], "done")
        health.disconnect(123)
        response = await self.client.post("/health/v1/next", headers=headers, json={})
        self.assertEqual(response.status, 401)
        self.assertNotIn(token, await response.text())

    async def test_health_protocol_partial_import_and_bad_payloads(self):
        self.store.set_timezone(123, "UTC")
        health = HealthStore(self.store.path)
        token = health.connect(123, "2020-01-01")
        headers = {"Authorization": "Bearer " + token}
        entry = self.store.add_entry(
            123, 1780000000, "Food", 100, 100, protein_per_100g=5
        )
        for kwargs in ({"data": "{"}, {"json": []}, {"json": {"user_id": 999}}):
            response = await self.client.post(
                "/health/v1/next", headers=headers, **kwargs
            )
            self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/health/v1/next?user_id=999", headers=headers, json={}
        )
        self.assertEqual(response.status, 400)
        # Apple Shortcuts omits the body for a JSON request body with no
        # fields, which must mean the same as an explicit empty object.
        empty = {"data": "", "headers": {**headers, "Content-Type": "application/json"}}
        response = await self.client.post("/health/v1/ack", **empty)
        self.assertEqual(response.status, 400)
        first = await (await self.client.post("/health/v1/next", **empty)).json()
        self.assertEqual(first["status"], "sample")
        self.assertEqual(first["sample"]["id"], f"food:{entry.entry_id}:calories")
        pending = await (
            await self.client.post("/health/v1/next", headers=headers, json={})
        ).json()
        self.assertEqual(pending["status"], "review")
        response = await self.client.post(
            "/health/v1/ack", headers=headers, json={"receipt": "wrong"}
        )
        self.assertEqual(response.status, 409)
        for _ in range(2):
            response = await self.client.post(
                "/health/v1/ack", headers=headers, json={"receipt": first["receipt"]}
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"status": "acknowledged"})
        second = await (
            await self.client.post("/health/v1/next", headers=headers, json={})
        ).json()
        self.assertEqual(second["sample"]["type"], "protein")
        # The API offers no bearer-authorized mutation of food or setup keys.
        response = await self.client.post(
            "/health/v1/connect", headers=headers, json={}
        )
        self.assertEqual(response.status, 404)
        response = await self.client.get("/static/apple-health.html")
        self.assertEqual(response.status, 200)
        guide = await response.text()
        # The guide installs the published shortcut and keeps the build as backup.
        self.assertIn("icloud.com/shortcuts/", guide)
        self.assertIn("apple-health-manual.html", guide)
        response = await self.client.get("/static/apple-health-manual.html")
        self.assertEqual(response.status, 200)
        self.assertIn("Log Health Sample", await response.text())

    async def test_static_revalidation_and_private_api(self):
        for path in ("/", "/static/app.js", "/static/app.css"):
            response = await self.client.get(path)
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
            etag = response.headers["ETag"]
            self.assertTrue(await response.read())
            response = await self.client.get(
                path, headers={"If-None-Match": "W/" + etag}
            )
            self.assertEqual(response.status, 304)
            self.assertEqual(await response.read(), b"")
        response = await self.request("GET", "/api/diary")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Database(Path(self.directory.name) / "test.db")
        settings = Settings(TOKEN, self.store.path, logging.INFO)
        self.client = TestClient(TestServer(build_web_app(settings, self.store)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers = {"Authorization": "tma " + signed()}

    async def request(self, method, path, **kwargs):
        headers = {
            **self.headers,
            "Idempotency-Key": str(uuid4()),
            **kwargs.pop("headers", {}),
        }
        if (
            method == "POST"
            and path == "/api/entries"
            and isinstance(kwargs.get("json"), dict)
        ):
            zone = self.store.get_timezone(123) or "UTC"
            kwargs["json"] = {
                "eaten_at": local_datetime(int(time.time()), zone).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                **kwargs["json"],
            }
        if (
            method == "DELETE"
            and path.startswith("/api/entries/")
            and "json" not in kwargs
        ):
            entry = self.store.get_entry(123, int(path.rsplit("/", 1)[1]))
            kwargs["json"] = {"version": entry_data(entry)["version"]} if entry else {}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def test_static_public_but_all_api_routes_require_auth(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("Дневник питания", await response.text())
        for method, path in (
            ("GET", "/api/diary"),
            ("GET", "/api/favorites"),
            ("POST", "/api/entries"),
            ("PUT", "/api/profile"),
            ("DELETE", "/api/entries/1"),
            ("POST", "/api/entries/1/favorite"),
        ):
            response = await self.client.request(method, path)
            self.assertEqual(response.status, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.client.get("/static/config.py")
        self.assertEqual(response.status, 404)

    async def test_onboarding_add_goal_favorite_and_delete(self):
        response = await self.request("GET", "/api/diary")
        self.assertTrue((await response.json())["needs_timezone"])
        response = await self.request(
            "PUT", "/api/profile", json={"timezone": "Europe/Moscow"}
        )
        self.assertEqual(response.status, 200)
        response = await self.request("PUT", "/api/profile", json={"goal": 2100})
        self.assertEqual(response.status, 200)
        response = await self.request(
            "POST",
            "/api/entries",
            json={"name": "Овсянка", "calories": 370, "grams": 60, "protein": 10},
        )
        self.assertEqual(response.status, 201)
        entry = await response.json()
        self.assertEqual(entry["nutrition"]["calories"], 222)
        response = await self.request(
            "POST", f"/api/entries/{entry['entry_id']}/favorite", json={}
        )
        self.assertEqual(response.status, 201)
        favorite = await response.json()
        response = await self.request(
            "POST",
            "/api/entries",
            json={
                "favorite_id": favorite["favorite_id"],
                "favorite_version": favorite["version"],
                "amount": 100,
            },
        )
        self.assertEqual(response.status, 201)
        response = await self.request("GET", "/api/diary")
        diary = await response.json()
        self.assertEqual(diary["goal"], 2100)
        self.assertEqual(diary["stats"]["calories"], 592)
        self.assertIsNone(diary["stats"]["fat"])
        response = await self.request("DELETE", f"/api/entries/{entry['entry_id']}")
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.store.get_entry(123, entry["entry_id"]))

    async def test_other_users_entries_and_favorites_are_inaccessible(self):
        self.store.set_timezone(999, "UTC")
        self.store.set_timezone(123, "UTC")
        entry = self.store.add_entry(999, int(time.time()), "Private", 100, 100)
        favorite, _ = self.store.add_favorite_from_entry(999, entry.entry_id)
        for method, path, body in (
            ("DELETE", f"/api/entries/{entry.entry_id}", {}),
            ("POST", f"/api/entries/{entry.entry_id}/favorite", {}),
            (
                "POST",
                "/api/entries",
                {"favorite_id": favorite.favorite_id, "amount": 100},
            ),
        ):
            response = await self.request(method, path, json=body)
            self.assertEqual(response.status, 404)
        response = await self.request("GET", "/api/diary?user_id=999")
        self.assertEqual((await response.json())["stats"]["entry_count"], 0)
        response = await self.request("GET", "/api/favorites?user_id=999")
        self.assertEqual((await response.json())["items"], [])
        self.assertIsNotNone(self.store.get_entry(999, entry.entry_id))

    async def test_invalid_input_never_writes_an_entry(self):
        self.store.set_timezone(123, "UTC")
        valid = {"name": "Food", "calories": 100, "grams": 100}
        for override in (
            {"grams": 0},
            {"grams": True},
            {"grams": 100001},
            {"calories": float("nan")},
            {"calories": 10001},
            {"protein": 80, "fat": 50},
            {"protein": "bad"},
            {"name": []},
        ):
            response = await self.request(
                "POST", "/api/entries", json={**valid, **override}
            )
            self.assertEqual(response.status, 400)
        response = await self.request("POST", "/api/entries", data="{")
        self.assertEqual(response.status, 400)
        response = await self.request("GET", "/api/diary")
        self.assertEqual((await response.json())["stats"]["entry_count"], 0)

    async def test_day_boundaries_paging_and_partial_macros(self):
        self.store.set_timezone(123, "America/New_York")
        # The spring DST day is 23 hours: 05:00 UTC to 04:00 UTC the next day.
        start = int(datetime(2026, 3, 8, 5, tzinfo=timezone.utc).timestamp())
        for index in range(31):
            self.store.add_entry(
                123, start + index, "Food", 100, 100, 5 if index == 0 else None
            )
        self.store.add_entry(123, start + 23 * 3600, "Next day", 900, 100)
        response = await self.request("GET", "/api/diary?day=2026-03-08")
        diary = await response.json()
        self.assertEqual(diary["stats"]["calories"], 3100)
        self.assertEqual(diary["stats"]["protein_coverage"], 1)
        self.assertTrue(diary["entries"]["has_next"])
        response = await self.request("GET", "/api/diary?day=2026-03-08&offset=30")
        self.assertEqual(len((await response.json())["entries"]["items"]), 1)

    async def test_serving_favorite_preserves_chat_draft(self):
        self.store.set_timezone(123, "UTC")
        favorite = self.store.add_favorite(123, "Pizza", 250, 10, 10, 30)
        self.store.start_session(
            123,
            123,
            SessionState.WAIT_FAVORITE_TO_SERVING,
            selected_favorite_id=favorite.favorite_id,
        )
        favorite = self.store.convert_favorite_to_serving(123, 123, 400)
        session = self.store.start_session(123, 123, SessionState.WAIT_FOOD_NAME)
        response = await self.request("GET", "/api/favorites")
        version = (await response.json())["items"][0]["version"]
        response = await self.request(
            "POST",
            "/api/entries",
            json={
                "favorite_id": favorite.favorite_id,
                "favorite_version": version,
                "amount": 0.5,
            },
        )
        self.assertEqual(response.status, 201)
        nutrition = (await response.json())["nutrition"]
        self.assertEqual(nutrition["calories"], 500)
        self.assertEqual(nutrition["servings"], 0.5)
        self.assertEqual(self.store.get_session(123, 123), session)

    async def test_favorite_changes_conflict_but_committed_retries_replay(self):
        self.store.set_timezone(123, "UTC")
        favorite = self.store.add_favorite(123, "Food", 200, None, None, None)
        response = await self.request("GET", "/api/favorites")
        selected = (await response.json())["items"][0]
        response = await self.request("GET", "/api/favorites?q=Food")
        self.assertEqual((await response.json())["items"][0], selected)
        data = {
            "favorite_id": favorite.favorite_id,
            "favorite_version": selected["version"],
            "amount": 100,
            "eaten_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
        }
        # Existing clients/drafts without a version must reselect the favorite.
        response = await self.request(
            "POST",
            "/api/entries",
            json={
                key: value for key, value in data.items() if key != "favorite_version"
            },
        )
        self.assertEqual(response.status, 409)
        self.store.start_session(
            123,
            123,
            SessionState.WAIT_FAVORITE_TO_SERVING,
            selected_favorite_id=favorite.favorite_id,
        )
        self.store.convert_favorite_to_serving(123, 123, 50)
        headers = {"Idempotency-Key": str(uuid4())}
        response = await self.request(
            "POST", "/api/entries", json=data, headers=headers
        )
        self.assertEqual(response.status, 409)
        response = await self.request("GET", "/api/diary")
        self.assertEqual((await response.json())["stats"]["entry_count"], 0)
        response = await self.request("GET", "/api/favorites")
        data.update(
            favorite_version=(await response.json())["items"][0]["version"], amount=1
        )
        response = await self.request(
            "POST", "/api/entries", json=data, headers=headers
        )
        self.assertEqual(response.status, 201)
        committed = await response.json()
        self.assertEqual(committed["nutrition"]["calories"], 100)
        self.assertEqual(committed["nutrition"]["grams"], 50)
        # Saving a changed entry to this favorite also changes its version.
        replacement = self.store.add_entry(123, int(time.time()), "Food", 300, 100)
        self.store.add_favorite_from_entry(123, replacement.entry_id)
        response = await self.request(
            "POST", "/api/entries", json=data, headers=headers
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(await response.json(), committed)
        response = await self.request("POST", "/api/entries", json=data)
        self.assertEqual(response.status, 409)
        response = await self.request("GET", "/api/diary")
        self.assertEqual((await response.json())["stats"]["entry_count"], 2)

    async def test_concurrent_retries_share_one_committed_entry_and_survive_restart(
        self,
    ):
        self.store.set_timezone(123, "UTC")
        key = str(uuid4())
        data = {
            "name": "Retry",
            "calories": 100,
            "grams": 50,
            "eaten_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
        }
        responses = await asyncio.gather(
            *[
                self.request(
                    "POST", "/api/entries", json=data, headers={"Idempotency-Key": key}
                )
                for _ in range(5)
            ]
        )
        results = [await response.json() for response in responses]
        self.assertTrue(all(response.status == 201 for response in responses))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.store.stats(123, 0, int(time.time()) + 10).entry_count, 1)
        from kcaloriebot.web_store import WebStore

        self.assertEqual(
            WebStore(self.store.path).mutate(123, key, "entry.create", data), results[0]
        )
        changed = await self.request(
            "POST",
            "/api/entries",
            json={**data, "grams": 200},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(changed.status, 409)

    async def test_backdating_edit_conflict_and_undo_keep_chat_session(self):
        self.store.set_timezone(123, "Europe/Moscow")
        session = self.store.start_session(123, 123, SessionState.WAIT_FOOD_NAME)
        yesterday = (
            local_datetime(int(time.time()), "Europe/Moscow") - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        data = {
            "name": "Dinner",
            "calories": 200,
            "grams": 150,
            "eaten_at": yesterday + "T12:00",
        }
        response = await self.request("POST", "/api/entries", json=data)
        entry = await response.json()
        response = await self.request("GET", "/api/diary?day=" + yesterday)
        self.assertEqual((await response.json())["stats"]["calories"], 300)
        path = f"/api/entries/{entry['entry_id']}"
        update = {**data, "name": "Lunch", "grams": 100, "version": entry["version"]}
        response = await self.request("PATCH", path, json=update)
        self.assertEqual(response.status, 200)
        edited = await response.json()
        self.assertEqual(edited["nutrition"]["calories"], 200)
        response = await self.request("PATCH", path, json={**update, "grams": 300})
        self.assertEqual(response.status, 409)
        response = await self.request(
            "DELETE", path, json={"version": edited["version"]}
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone(self.store.get_entry(123, entry["entry_id"]))
        response = await self.request("POST", path + "/restore", json={})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["nutrition"]["calories"], 200)
        self.assertEqual(self.store.get_session(123, 123), session)

    async def test_time_validation_and_missing_key(self):
        self.store.set_timezone(123, "America/New_York")
        for timestamp in (
            None,
            "bad",
            "2026-03-08T02:30",
            "2999-01-01T12:00",
            "2020-01-01T12:00",
        ):
            response = await self.request(
                "POST",
                "/api/entries",
                json={
                    "name": "Food",
                    "calories": 100,
                    "grams": 100,
                    "eaten_at": timestamp,
                },
            )
            self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/api/entries", headers=self.headers, json={"name": "Food"}
        )
        self.assertEqual(response.status, 400)

    async def test_edit_unnamed_and_serving_entries(self):
        self.store.set_timezone(123, "UTC")
        now = int(time.time())
        timestamp = local_datetime(now, "UTC").strftime("%Y-%m-%dT%H:%M")
        unnamed = self.store.add_entry(123, now, None, 100, 100)
        response = await self.request(
            "PATCH",
            f"/api/entries/{unnamed.entry_id}",
            json={
                "name": "",
                "eaten_at": timestamp,
                "calories": 100,
                "grams": 50,
                "version": entry_data(self.store.get_entry(123, unnamed.entry_id))[
                    "version"
                ],
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIsNone((await response.json())["name"])
        response = await self.request(
            "POST",
            "/api/entries",
            json={
                "name": "Soup",
                "unit": "serving",
                "amount": 0.5,
                "serving_grams": 400,
                "calories": 200,
                "protein": 10,
            },
        )
        serving = await response.json()
        response = await self.request(
            "PATCH",
            f"/api/entries/{serving['entry_id']}",
            json={
                "name": "Soup",
                "eaten_at": timestamp,
                "amount": 2,
                "calories": 200,
                "protein": 10,
                "version": serving["version"],
            },
        )
        self.assertEqual(response.status, 200)
        totals = (await response.json())["nutrition"]
        self.assertEqual(totals["grams"], 800)
        self.assertEqual(totals["calories"], 400)
        self.assertEqual(totals["protein"], 20)
        self.assertIsNone(totals["fat"])

    async def test_other_user_cannot_edit_or_restore(self):
        self.store.set_timezone(123, "UTC")
        self.store.set_timezone(999, "UTC")
        entry = self.store.add_entry(999, int(time.time()), "Private", 100, 100)
        path = f"/api/entries/{entry.entry_id}"
        for method, suffix in (("GET", ""), ("PATCH", ""), ("POST", "/restore")):
            response = await self.request(method, path + suffix, json={})
            self.assertEqual(response.status, 404)

    async def test_quick_add_recent_and_literal_favorite_search(self):
        self.store.set_timezone(123, "UTC")
        self.store.set_timezone(999, "UTC")
        self.store.add_entry(999, int(time.time()), "Private", 100, 100)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        response = await self.request(
            "POST",
            "/api/entries",
            json={
                "quick_add": "овсянка 370 60 б12 ж6 у62",
                "eaten_at": yesterday + "T12:00",
            },
        )
        self.assertEqual(response.status, 201, await response.text())
        self.assertEqual((await response.json())["nutrition"]["calories"], 222)
        response = await self.request("GET", "/api/recent")
        items = (await response.json())["items"]
        self.assertEqual([item["name"] for item in items], ["овсянка"])
        self.assertEqual(items[0]["values"]["protein"], 12)
        response = await self.request("POST", "/api/entries", json={"quick_add": "???"})
        self.assertEqual(response.status, 400)
        for index in range(32):
            self.store.add_favorite(123, f"Йогурт 5%_{index}", 100, None, None, None)
        self.store.add_favorite(123, "Йогурт 50", 100, None, None, None)
        self.store.add_favorite(999, "Йогурт 5%_private", 100, None, None, None)
        response = await self.request(
            "GET", "/api/favorites", params={"q": "йОГУРТ 5%_"}
        )
        page = await response.json()
        self.assertEqual(len(page["items"]), 30)
        self.assertTrue(page["has_next"])
        response = await self.request(
            "GET", "/api/favorites", params={"q": "йогурт 5%_", "offset": 30}
        )
        page = await response.json()
        self.assertEqual(len(page["items"]), 2)
        self.assertFalse(page["has_next"])

    async def test_statistics_dst_missing_days_and_owner_scope(self):
        self.store.set_timezone(123, "America/New_York")
        self.store.set_timezone(999, "UTC")
        start = int(datetime(2026, 3, 8, 5, tzinfo=timezone.utc).timestamp())
        self.store.add_entry(123, start, "Breakfast", 200, 100, 10)
        self.store.add_entry(123, start + 23 * 3600 - 1, "Dinner", 300, 100)
        self.store.add_entry(123, start + 23 * 3600, "Next day", 900, 100)
        self.store.add_entry(999, start, "Private", 9999, 100)
        response = await self.request(
            "GET", "/api/statistics?day=2026-03-08&period=week"
        )
        data = await response.json()
        self.assertEqual(data["totals"]["calories"], 500)
        self.assertEqual(data["average_logged_day"], 500)
        self.assertEqual(data["logged_days"], 1)
        self.assertEqual(len(data["days"]), 7)
        self.assertIsNone(data["days"][0]["calories"])
        self.assertEqual(data["days"][-1]["protein_coverage"], 1)
        response = await self.request(
            "GET", "/api/statistics?day=2026-03-08&period=month"
        )
        data = await response.json()
        self.assertEqual(len(data["days"]), 31)
        self.assertEqual(data["totals"]["calories"], 1400)
        response = await self.request("GET", "/api/statistics?period=forever")
        self.assertEqual(response.status, 400)

    async def test_weights_retries_edit_conflict_paging_and_isolation(self):
        self.store.set_timezone(123, "Europe/Moscow")
        self.store.set_timezone(999, "UTC")
        now = int(time.time())
        foreign = self.store.add_weight(999, now, 150)
        session = self.store.start_session(123, 123, SessionState.WAIT_FOOD_NAME)
        data = {
            "measured_at": local_datetime(now, "Europe/Moscow").strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "weight_kg": 80,
        }
        key = str(uuid4())
        response = await self.request(
            "POST", "/api/weights", json=data, headers={"Idempotency-Key": key}
        )
        self.assertEqual(response.status, 201)
        weight = await response.json()
        response = await self.request(
            "POST", "/api/weights", json=data, headers={"Idempotency-Key": key}
        )
        self.assertEqual(await response.json(), weight)
        path = f"/api/weights/{weight['weight_id']}"
        response = await self.request(
            "PATCH", path, json={"weight_kg": 79, "version": weight["version"]}
        )
        updated = await response.json()
        self.assertEqual(updated["weight_kg"], 79)
        response = await self.request(
            "DELETE", path, json={"version": weight["version"]}
        )
        self.assertEqual(response.status, 409)
        for method in ("PATCH", "DELETE"):
            response = await self.request(
                method, f"/api/weights/{foreign.weight_id}", json={"weight_kg": 10}
            )
            self.assertEqual(response.status, 404)
        for value in (None, True, 0, 501):
            response = await self.request(
                "POST", "/api/weights", json={**data, "weight_kg": value}
            )
            self.assertEqual(response.status, 400)
        for i in range(30):
            self.store.add_weight(123, now - 60 - i, 81)
        response = await self.request("GET", "/api/weights")
        result = await response.json()
        self.assertEqual(len(result["items"]), 30)
        self.assertTrue(result["has_next"])
        self.assertAlmostEqual(result["average"], (79 + 30 * 81) / 31)
        response = await self.request("GET", "/api/weights?offset=30")
        self.assertEqual(len((await response.json())["items"]), 1)
        response = await self.request(
            "DELETE", path, json={"version": updated["version"]}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.get_session(123, 123), session)
