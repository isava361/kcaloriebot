import hashlib
import hmac
import importlib.util
import json
import logging
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("Install .[miniapp] to test the Mini App")

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kcaloriebot.config import Settings
from kcaloriebot.database import Database
from kcaloriebot.domain import SessionState
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
        return await self.client.request(method, path, headers=self.headers, **kwargs)

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
            json={"favorite_id": favorite["favorite_id"], "amount": 100},
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
        response = await self.request(
            "POST",
            "/api/entries",
            json={"favorite_id": favorite.favorite_id, "amount": 0.5},
        )
        self.assertEqual(response.status, 201)
        nutrition = (await response.json())["nutrition"]
        self.assertEqual(nutrition["calories"], 500)
        self.assertEqual(nutrition["servings"], 0.5)
        self.assertEqual(self.store.get_session(123, 123), session)
