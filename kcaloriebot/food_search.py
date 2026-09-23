"""Product search in Open Food Facts for the Mini App.

Open Food Facts allows 10 search requests per minute per IP, and every user's
search leaves from this server's address. Results are cached and upstream calls
are rate limited here, well below that, so one busy user cannot get the server
banned for everyone.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from typing import Awaitable, Callable

SEARCH_URL = "https://search.openfoodfacts.org/search"
USER_AGENT = "kcaloriebot/1.0 (+https://github.com/isava361/kcaloriebot)"
PAGE_SIZE = 30
MAX_PAGE = 10
FIELDS = "code,product_name,product_name_ru,brands,nutriments"
# Pure fat is about 900 kcal per 100 g; anything above is a data entry error.
MAX_KCAL_PER_100G = 950.0
KJ_PER_KCAL = 4.184

Fetch = Callable[[str, int], Awaitable[dict]]


class SearchBusy(Exception):
    """The local limit on upstream requests is exhausted; retry later."""


class SearchUnavailable(Exception):
    """Open Food Facts failed, timed out or returned something unusable."""


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _text(value) -> str:
    if isinstance(value, list):
        value = next(
            (item for item in value if isinstance(item, str) and item.strip()), ""
        )
    return " ".join(value.split()) if isinstance(value, str) else ""


def product(hit: dict) -> dict | None:
    """Normalize one hit to per-100 g values, or None when it is unusable."""
    if not isinstance(hit, dict):
        return None
    name = _text(hit.get("product_name_ru")) or _text(hit.get("product_name"))
    nutriments = hit.get("nutriments")
    if not name or not isinstance(nutriments, dict):
        return None
    calories = _number(nutriments.get("energy-kcal_100g"))
    if calories is None:
        kj = _number(nutriments.get("energy-kj_100g"))
        if kj is None:
            kj = _number(nutriments.get("energy_100g"))
        calories = None if kj is None else kj / KJ_PER_KCAL
    if calories is None or not 0 <= calories <= MAX_KCAL_PER_100G:
        return None
    macros = {}
    for key, source in (
        ("protein", "proteins_100g"),
        ("fat", "fat_100g"),
        ("carbs", "carbohydrates_100g"),
    ):
        value = _number(nutriments.get(source))
        if value is not None and not 0 <= value <= 100:
            return None
        macros[key] = None if value is None else round(value, 1)
    if sum(value or 0.0 for value in macros.values()) > 100.000001:
        return None
    brand = _text(hit.get("brands"))
    full_name = name
    if brand and brand.casefold() not in name.casefold():
        full_name = f"{name} ({brand})"
    if len(full_name) > 200:
        full_name = name[:200].rstrip()
    code = hit.get("code")
    return {
        "source": "openfoodfacts",
        "code": code if isinstance(code, str) else None,
        "name": full_name,
        "title": name[:200],
        "brand": brand[:200] or None,
        "calories": round(calories, 1),
        **macros,
    }


def products(payload: dict) -> list[dict]:
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        raise SearchUnavailable("Unexpected search response")
    result, seen = [], set()
    for hit in hits:
        item = product(hit)
        if item is None:
            continue
        # The same product is often entered several times with one barcode or
        # with none; identical name and energy read as duplicates to a user.
        key = (item["name"].casefold(), item["calories"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


class RateLimit:
    def __init__(self, limit: int, period: float, clock: Callable[[], float]):
        self.limit, self.period, self.clock = limit, period, clock
        self.calls: deque[float] = deque()

    def available(self) -> bool:
        now = self.clock()
        while self.calls and self.calls[0] <= now - self.period:
            self.calls.popleft()
        return len(self.calls) < self.limit

    def take(self) -> None:
        self.calls.append(self.clock())


class FoodSearch:
    def __init__(
        self,
        fetch: Fetch,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = 24 * 3600,
        cache_size: int = 500,
        server_per_minute: int = 8,
        user_per_minute: int = 4,
    ):
        self.fetch, self.clock = fetch, clock
        self.cache_seconds, self.cache_size = cache_seconds, cache_size
        self.cache: OrderedDict[tuple[str, int], tuple[float, dict]] = OrderedDict()
        self.server_limit = RateLimit(server_per_minute, 60.0, clock)
        self.user_per_minute = user_per_minute
        self.user_limits: dict[int, RateLimit] = {}

    def _cached(self, key: tuple[str, int]) -> dict | None:
        found = self.cache.get(key)
        if found is None:
            return None
        if found[0] <= self.clock():
            del self.cache[key]
            return None
        self.cache.move_to_end(key)
        return found[1]

    def _user_limit(self, user_id: int) -> RateLimit:
        if len(self.user_limits) > 1000:
            # available() drops expired calls, so idle users end up empty.
            self.user_limits = {
                key: limit
                for key, limit in self.user_limits.items()
                if not limit.available() or limit.calls
            }
        return self.user_limits.setdefault(
            user_id, RateLimit(self.user_per_minute, 60.0, self.clock)
        )

    async def search(self, user_id: int, query: str, page: int = 1) -> dict:
        key = (query.casefold(), page)
        cached = self._cached(key)
        if cached is not None:
            return cached
        user_limit = self._user_limit(user_id)
        if not user_limit.available() or not self.server_limit.available():
            raise SearchBusy
        user_limit.take()
        self.server_limit.take()
        try:
            payload = await self.fetch(query, page)
        except SearchUnavailable:
            raise
        except (asyncio.TimeoutError, OSError, ValueError) as exc:
            raise SearchUnavailable(type(exc).__name__) from exc
        page_count = (
            _number(payload.get("page_count")) if isinstance(payload, dict) else None
        )
        result = {
            "items": products(payload),
            "page": page,
            "has_next": page < min(MAX_PAGE, int(page_count or 0)),
        }
        self.cache[key] = (self.clock() + self.cache_seconds, result)
        while len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return result


class OpenFoodFacts:
    """Fetcher with its own aiohttp session, opened on first use."""

    def __init__(self):
        self.session = None

    async def __call__(self, query: str, page: int) -> dict:
        from aiohttp import ClientError, ClientSession, ClientTimeout

        if self.session is None:
            self.session = ClientSession(
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=ClientTimeout(total=8),
            )
        params = {
            "q": query,
            "langs": "ru,en",
            "page": str(page),
            "page_size": str(PAGE_SIZE),
            "fields": FIELDS,
        }
        try:
            async with self.session.get(SEARCH_URL, params=params) as response:
                if response.status != 200:
                    raise SearchUnavailable(f"HTTP {response.status}")
                return await response.json(content_type=None)
        except ClientError as exc:
            raise SearchUnavailable(type(exc).__name__) from exc

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
