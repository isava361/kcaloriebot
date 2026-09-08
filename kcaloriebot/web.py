"""Optional Mini App server: install with pip install -e '.[miniapp]'."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from .config import Settings, load_settings
from .database import Database
from .domain import (
    EARLIEST_DIARY_DATE,
    NotFound,
    ValidationError,
    check_calories_per_100g,
    check_serving_grams,
    check_servings,
    day_bounds,
    local_date,
    parse_daily_goal,
)

DATABASE = web.AppKey("database", Database)
SETTINGS = web.AppKey("settings", Settings)
STATIC = Path(__file__).with_name("static")
LOGGER = logging.getLogger(__name__)


def authenticate(raw: str, token: str, now: int | None = None) -> int:
    """Validate Telegram initData before trusting its user ID (one-hour expiry)."""
    try:
        if not raw or len(raw) > 16384:
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError
        supplied = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        age = (int(time.time()) if now is None else now) - int(data["auth_date"])
        if not -30 <= age <= 3600:
            raise ValueError
        user_id = json.loads(data["user"])["id"]
        if type(user_id) is not int or not 0 < user_id < 2**63:
            raise ValueError
        return user_id
    except (ValueError, KeyError, TypeError, OverflowError):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Откройте приложение заново из Telegram."}),
            content_type="application/json",
        ) from None


@web.middleware
async def boundary(request: web.Request, handler):
    try:
        if request.path.startswith("/api/"):
            raw = request.headers.get("Authorization", "")
            if not raw.startswith("tma "):
                raw = ""
            request["user_id"] = authenticate(raw[4:], request.app[SETTINGS].bot_token)
        response = await handler(request)
    except web.HTTPException as exc:
        response = web.json_response(
            {"error": "Откройте приложение заново из Telegram."}
            if exc.status == 401
            else {"error": "Запрос не поддерживается."},
            status=exc.status,
        )
    except NotFound:
        response = web.json_response({"error": "Запись не найдена."}, status=404)
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError):
        response = web.json_response(
            {"error": "Проверьте введённые данные и допустимые значения."}, status=400
        )
    except Exception:
        # Do not log headers or request contents: initData is a credential.
        LOGGER.error("Mini App request failed")
        response = web.json_response(
            {"error": "Не удалось выполнить запрос. Попробуйте ещё раз."}, status=500
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
    )
    return response


async def payload(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise ValueError
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError
    return data


def number(data: dict, key: str, optional: bool = False) -> float | None:
    value = data.get(key)
    if optional and value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError
    return float(value)


def snapshot(store: Database, user_id: int, query) -> dict:
    zone = store.get_timezone(user_id)
    if zone is None:
        return {"needs_timezone": True}
    today = local_date(store.now_epoch(), zone)
    day = date.fromisoformat(query.get("day", today.isoformat()))
    if not EARLIEST_DIARY_DATE <= day <= today:
        raise ValueError
    bounds = day_bounds(day, zone)
    offset = int(query.get("offset", "0"))
    return {
        "needs_timezone": False,
        "timezone": zone,
        "today": today.isoformat(),
        "day": day.isoformat(),
        "goal": store.get_daily_goal(user_id),
        "stats": asdict(store.stats(user_id, bounds.start_utc, bounds.end_utc)),
        "entries": asdict(
            store.page_entries(user_id, bounds.start_utc, bounds.end_utc, offset, 30)
        ),
    }


async def diary(request: web.Request) -> web.Response:
    result = await asyncio.to_thread(
        snapshot, request.app[DATABASE], request["user_id"], dict(request.query)
    )
    return web.json_response(result)


async def profile(request: web.Request) -> web.Response:
    data = await payload(request)
    store, user_id = request.app[DATABASE], request["user_id"]
    if set(data) == {"timezone"} and isinstance(data["timezone"], str):
        await asyncio.to_thread(store.set_timezone, user_id, data["timezone"])
    elif set(data) == {"goal"}:
        goal = data["goal"]
        if goal is not None:
            goal = parse_daily_goal(str(number(data, "goal")))
        await asyncio.to_thread(store.ensure_user, user_id)
        await asyncio.to_thread(store.set_daily_goal, user_id, goal)
    else:
        raise ValueError
    return web.json_response({"ok": True})


async def favorites(request: web.Request) -> web.Response:
    result = await asyncio.to_thread(
        request.app[DATABASE].page_favorites,
        request["user_id"],
        int(request.query.get("offset", "0")),
        30,
    )
    return web.json_response(asdict(result))


def create_entry(store: Database, user_id: int, data: dict):
    if store.get_timezone(user_id) is None:
        raise ValidationError("Timezone required")
    now = store.now_epoch()
    if "favorite_id" in data:
        favorite_id = data["favorite_id"]
        if type(favorite_id) is not int:
            raise ValueError
        favorite = store.get_favorite(user_id, favorite_id)
        if favorite is None:
            raise NotFound
        amount = number(data, "amount")
        if favorite.unit == "serving":
            check_servings(amount)
        else:
            check_serving_grams(amount)
        return store.log_favorite(user_id, favorite_id, amount, now)
    if not isinstance(data.get("name"), str):
        raise ValueError
    return store.add_entry(
        user_id,
        now,
        data["name"],
        check_calories_per_100g(number(data, "calories")),
        check_serving_grams(number(data, "grams")),
        number(data, "protein", True),
        number(data, "fat", True),
        number(data, "carbs", True),
    )


async def add_entry(request: web.Request) -> web.Response:
    data = await payload(request)
    entry = await asyncio.to_thread(
        create_entry, request.app[DATABASE], request["user_id"], data
    )
    return web.json_response(asdict(entry), status=201)


async def delete_entry(request: web.Request) -> web.Response:
    await asyncio.to_thread(
        request.app[DATABASE].delete_entry,
        request["user_id"],
        int(request.match_info["entry_id"]),
    )
    return web.json_response({"ok": True})


async def save_favorite(request: web.Request) -> web.Response:
    result, _created = await asyncio.to_thread(
        request.app[DATABASE].add_favorite_from_entry,
        request["user_id"],
        int(request.match_info["entry_id"]),
    )
    return web.json_response(asdict(result), status=201)


async def static_file(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "index.html")
    types = {
        "index.html": "text/html",
        "app.js": "text/javascript",
        "app.css": "text/css",
    }
    if name not in types:
        raise web.HTTPNotFound()
    return web.Response(body=(STATIC / name).read_bytes(), content_type=types[name])


def build_web_app(
    settings: Settings, database: Database | None = None
) -> web.Application:
    app = web.Application(middlewares=[boundary], client_max_size=16384)
    store = database or Database(settings.database_path)
    store.initialize()
    app[DATABASE], app[SETTINGS] = store, settings
    app.add_routes(
        [
            web.get("/", static_file),
            web.get("/static/{name}", static_file),
            web.get("/api/diary", diary),
            web.put("/api/profile", profile),
            web.get("/api/favorites", favorites),
            web.post("/api/entries", add_entry),
            web.delete("/api/entries/{entry_id}", delete_entry),
            web.post("/api/entries/{entry_id}/favorite", save_favorite),
        ]
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="KCalorieBot Mini App HTTP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    web.run_app(
        build_web_app(load_settings()), host=args.host, port=args.port, access_log=None
    )


if __name__ == "__main__":
    main()
