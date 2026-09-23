import asyncio
import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path

if importlib.util.find_spec("aiohttp") is None:
    raise unittest.SkipTest("Install .[miniapp] to test the Mini App")

from aiohttp.test_utils import TestClient, TestServer

from kcaloriebot.config import ConfigError, Settings, load_settings
from kcaloriebot.database import Database
from kcaloriebot.food_search import (
    FoodSearch,
    SearchBusy,
    SearchUnavailable,
    product,
    products,
)
from kcaloriebot.web import build_web_app
from tests.test_web import TOKEN, signed


def hit(name="Гречка", brand="Мистраль", **nutriments):
    values = {
        "energy-kcal_100g": 351,
        "proteins_100g": 12,
        "fat_100g": 3.4,
        "carbohydrates_100g": 64.2,
    }
    values.update(nutriments)
    return {
        "code": "4601916007082",
        "product_name": name,
        "brands": [brand] if brand else None,
        "nutriments": {
            key: value for key, value in values.items() if value is not None
        },
    }


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ProductTests(unittest.TestCase):
    def test_normalizes_to_per_100g_with_brand_in_name(self):
        self.assertEqual(
            product(hit()),
            {
                "source": "openfoodfacts",
                "code": "4601916007082",
                "name": "Гречка (Мистраль)",
                "title": "Гречка",
                "brand": "Мистраль",
                "calories": 351.0,
                "protein": 12.0,
                "fat": 3.4,
                "carbs": 64.2,
            },
        )

    def test_prefers_russian_name_and_skips_brand_already_named(self):
        item = product(
            {
                **hit("Milk Prostokvashino", "Простоквашино"),
                "product_name_ru": "Молоко Простоквашино 2,5%",
            }
        )
        self.assertEqual(item["name"], "Молоко Простоквашино 2,5%")

    def test_energy_from_kilojoules_and_missing_macros(self):
        item = product(
            hit(**{"energy-kcal_100g": None, "energy-kj_100g": 418.4, "fat_100g": None})
        )
        self.assertEqual((item["calories"], item["fat"]), (100.0, None))
        item = product(hit(**{"energy-kcal_100g": None, "energy_100g": "836,8"}))
        self.assertEqual(item["calories"], 200.0)

    def test_rejects_unusable_products(self):
        for bad in (
            hit(**{"energy-kcal_100g": None}),
            hit(**{"energy-kcal_100g": -1}),
            hit(**{"energy-kcal_100g": 2000}),
            hit(**{"energy-kcal_100g": True}),
            hit(**{"energy-kcal_100g": float("nan")}),
            hit(proteins_100g=120),
            hit(proteins_100g=60, carbohydrates_100g=60),
            hit(name="  "),
            {**hit(), "nutriments": None},
            "not a hit",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(product(bad))

    def test_long_name_drops_brand_and_fits_diary_limit(self):
        item = product(hit(name="а" * 199, brand="Бренд"))
        self.assertEqual(item["name"], "а" * 199)
        self.assertEqual(len(product(hit(name="б" * 250))["title"]), 200)

    def test_duplicates_are_merged_and_bad_payload_is_unavailable(self):
        items = products({"hits": [hit(), hit(), hit(**{"energy-kcal_100g": 340})]})
        self.assertEqual([item["calories"] for item in items], [351.0, 340.0])
        with self.assertRaises(SearchUnavailable):
            products({"error": "nope"})


class FoodSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_limits_and_failures(self):
        clock, calls = Clock(), []

        async def fetch(query, page):
            calls.append((query, page))
            if query == "сломано":
                raise asyncio.TimeoutError
            return {"hits": [hit()], "page_count": 3}

        search = FoodSearch(fetch, clock=clock, server_per_minute=3, user_per_minute=2)
        result = await search.search(1, "Гречка", 1)
        self.assertEqual((result["page"], result["has_next"]), (1, True))
        # Cached regardless of case, and a cache hit costs no quota.
        await search.search(1, "гречка", 1)
        self.assertEqual(calls, [("Гречка", 1)])
        with self.assertRaises(SearchUnavailable):
            await search.search(1, "сломано", 1)
        with self.assertRaises(SearchBusy):
            await search.search(1, "творог", 1)
        await search.search(2, "творог", 1)
        with self.assertRaises(SearchBusy):
            await search.search(3, "кефир", 1)
        clock.now += 61
        await search.search(1, "кефир", 3)
        self.assertFalse((await search.search(1, "кефир", 3))["has_next"])
        clock.now += 24 * 3600
        await search.search(2, "гречка", 1)
        self.assertEqual(calls[-1], ("гречка", 1))


class FoodSearchWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Database(Path(self.directory.name) / "test.db")
        self.calls = []

        async def fetch(query, page):
            self.calls.append((query, page))
            if query == "сломано":
                raise SearchUnavailable("HTTP 502")
            return {
                "hits": [hit(), hit(name="Мусор", **{"energy-kcal_100g": None})],
                "page_count": 1,
            }

        self.search = FoodSearch(fetch, server_per_minute=100, user_per_minute=100)
        self.client = await self.start(Settings(TOKEN, self.store.path, logging.INFO))
        self.headers = {"Authorization": "tma " + signed()}

    async def start(self, settings):
        client = TestClient(
            TestServer(build_web_app(settings, self.store, self.search))
        )
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_search_returns_normalized_products(self):
        response = await self.client.get(
            "/api/food-search",
            params={"q": "  гречка  ", "page": "1"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(
            [item["name"] for item in body["items"]], ["Гречка (Мистраль)"]
        )
        self.assertFalse(body["has_next"])
        self.assertEqual(self.calls, [("гречка", 1)])

    async def test_search_validation_auth_and_upstream_failure(self):
        for params in (
            {"q": "г"},
            {"q": ""},
            {"q": "гречка", "page": "11"},
            {"q": "а" * 201},
        ):
            response = await self.client.get(
                "/api/food-search", params=params, headers=self.headers
            )
            self.assertEqual(response.status, 400, params)
        response = await self.client.get("/api/food-search", params={"q": "гречка"})
        self.assertEqual(response.status, 401)
        with self.assertLogs("kcaloriebot.web", logging.WARNING):
            response = await self.client.get(
                "/api/food-search", params={"q": "сломано"}, headers=self.headers
            )
        self.assertEqual(response.status, 503)
        self.assertIn("не отвечает", (await response.json())["error"])

    async def test_busy_search_is_429(self):
        self.search.server_limit.limit = 0
        response = await self.client.get(
            "/api/food-search", params={"q": "кефир"}, headers=self.headers
        )
        self.assertEqual(response.status, 429)

    async def test_disabled_search_is_hidden(self):
        client = await self.start(
            Settings(TOKEN, self.store.path, logging.INFO, food_search=False)
        )
        response = await client.get(
            "/api/food-search", params={"q": "гречка"}, headers=self.headers
        )
        self.assertEqual(response.status, 404)
        self.store.set_timezone(123, "Europe/Moscow")
        diary = await (await client.get("/api/diary", headers=self.headers)).json()
        self.assertFalse(diary["food_search"])
        diary = await (await self.client.get("/api/diary", headers=self.headers)).json()
        self.assertTrue(diary["food_search"])

    async def test_create_favorite_from_search_result_updates_same_name(self):
        data = {
            "name": "Гречка (Мистраль)",
            "unit": "100g",
            "calories": 351,
            "protein": 12,
            "fat": 3.4,
            "carbs": None,
        }
        response = await self.client.post(
            "/api/favorites", json=data, headers=self.headers
        )
        self.assertEqual(response.status, 201)
        created = await response.json()
        self.assertEqual(
            (created["calories_per_100g"], created["carbs_per_100g"]), (351, None)
        )
        response = await self.client.post(
            "/api/favorites",
            json={**data, "name": "гречка (мистраль)", "calories": 340},
            headers=self.headers,
        )
        self.assertEqual(response.status, 200)
        updated = await response.json()
        self.assertEqual(
            (updated["favorite_id"], updated["calories_per_100g"]),
            (created["favorite_id"], 340),
        )
        self.assertEqual(len(self.store.search_favorites(123, "гречка")), 1)
        for bad in (
            {**data, "protein": 80, "fat": 30},
            {**data, "calories": "351"},
            {**data, "unit": "kg"},
            {**data, "name": " "},
            {**data, "extra": 1},
            {key: value for key, value in data.items() if key != "name"},
        ):
            response = await self.client.post(
                "/api/favorites", json=bad, headers=self.headers
            )
            self.assertEqual(response.status, 400, bad)


class SettingsTests(unittest.TestCase):
    def test_food_search_setting(self):
        base = {"BOT_TOKEN": "x"}
        self.assertTrue(load_settings(base).food_search)
        self.assertTrue(load_settings({**base, "FOOD_SEARCH": " "}).food_search)
        self.assertFalse(load_settings({**base, "FOOD_SEARCH": "OFF"}).food_search)
        with self.assertRaises(ConfigError):
            load_settings({**base, "FOOD_SEARCH": "fatsecret"})


if __name__ == "__main__":
    unittest.main()
