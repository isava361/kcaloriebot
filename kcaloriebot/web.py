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
    MAX_ENTRY_AGE_SECONDS,
    NotFound,
    StateConflict,
    ValidationError,
    day_bounds,
    local_date,
    local_datetime,
    parse_daily_goal,
)
from .web_store import WebStore, entry_data, favorite_data, number

DATABASE = web.AppKey("database", Database)
SETTINGS = web.AppKey("settings", Settings)
ASSETS = web.AppKey("assets", dict)
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
    except StateConflict as exc:
        response = web.json_response({"error": str(exc)}, status=409)
    except ValidationError as exc:
        message = str(exc)
        if not any("А" <= char <= "я" for char in message):
            if "future" in message:
                message = "Дата и время не могут быть в будущем."
            elif "year in the past" in message:
                message = (
                    "Можно добавить или перенести запись не более чем на год назад."
                )
            elif "Weight" in message:
                message = "Укажите вес от 1 до 500 кг."
            else:
                message = "Проверьте формат и допустимые значения полей."
        response = web.json_response({"error": message}, status=400)
    except (ValueError, TypeError, KeyError, OverflowError):
        response = web.json_response(
            {"error": "Проверьте введённые данные и допустимые значения."}, status=400
        )
    except Exception:
        # Do not log headers or request contents: initData is a credential.
        LOGGER.error("Mini App request failed")
        response = web.json_response(
            {"error": "Не удалось выполнить запрос. Попробуйте ещё раз."}, status=500
        )
    if request.path.startswith("/api/") or response.status >= 400:
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


def snapshot(store: Database, user_id: int, query) -> dict:
    zone = store.get_timezone(user_id)
    if zone is None:
        return {"needs_timezone": True, "user_id": user_id}
    today = local_date(store.now_epoch(), zone)
    day = date.fromisoformat(query.get("day", today.isoformat()))
    if not EARLIEST_DIARY_DATE <= day <= today:
        raise ValueError
    bounds = day_bounds(day, zone)
    offset = int(query.get("offset", "0"))
    page = store.page_entries(user_id, bounds.start_utc, bounds.end_utc, offset, 30)
    return {
        "needs_timezone": False,
        "user_id": user_id,
        "server_now": store.now_epoch(),
        "timezone": zone,
        "today": today.isoformat(),
        "day": day.isoformat(),
        "local_now": local_datetime(store.now_epoch(), zone).strftime("%Y-%m-%dT%H:%M"),
        "earliest_day": EARLIEST_DIARY_DATE.isoformat(),
        "earliest_entry_time": local_datetime(
            store.now_epoch() - MAX_ENTRY_AGE_SECONDS, zone
        ).strftime("%Y-%m-%dT%H:%M"),
        "goal": store.get_daily_goal(user_id),
        "stats": asdict(store.stats(user_id, bounds.start_utc, bounds.end_utc)),
        "entries": {
            **asdict(page),
            "items": [entry_data(entry) for entry in page.items],
        },
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
    query = request.query.get("q", "").strip()
    if len(query) > 200:
        raise ValidationError("Поисковый запрос слишком длинный.")
    offset = int(request.query.get("offset", "0"))
    if query:
        items = await asyncio.to_thread(
            request.app[DATABASE].search_favorites,
            request["user_id"],
            query,
            31,
            offset,
        )
        return web.json_response(
            {
                "items": [favorite_data(item) for item in items[:30]],
                "offset": offset,
                "has_previous": offset > 0,
                "has_next": len(items) > 30,
            }
        )
    result = await asyncio.to_thread(
        request.app[DATABASE].page_favorites,
        request["user_id"],
        int(request.query.get("offset", "0")),
        30,
    )
    return web.json_response(
        {**asdict(result), "items": [favorite_data(item) for item in result.items]}
    )


async def recent(request: web.Request) -> web.Response:
    items = await asyncio.to_thread(
        request.app[DATABASE].recent_entry_templates, request["user_id"], 20
    )
    return web.json_response({"items": [entry_data(item) for item in items]})


async def mutate(request: web.Request, action: str, status: int = 200) -> web.Response:
    data = await payload(request)
    if "entry_id" in request.match_info:
        data["entry_id"] = int(request.match_info["entry_id"])
    if "weight_id" in request.match_info:
        data["weight_id"] = int(request.match_info["weight_id"])
    result = await asyncio.to_thread(
        WebStore(request.app[DATABASE].path).mutate,
        request["user_id"],
        request.headers.get("Idempotency-Key", ""),
        action,
        data,
    )
    return web.json_response(result, status=status)


async def add_entry(request: web.Request) -> web.Response:
    return await mutate(request, "entry.create", 201)


async def statistics(request: web.Request) -> web.Response:
    result = await asyncio.to_thread(
        WebStore(request.app[DATABASE].path).statistics,
        request["user_id"],
        dict(request.query),
    )
    return web.json_response(result)


async def weights(request: web.Request) -> web.Response:
    result = await asyncio.to_thread(
        WebStore(request.app[DATABASE].path).weight_snapshot,
        request["user_id"],
        dict(request.query),
    )
    return web.json_response(result)


async def add_weight(request: web.Request) -> web.Response:
    return await mutate(request, "weight.create", 201)


async def edit_weight(request: web.Request) -> web.Response:
    return await mutate(request, "weight.update")


async def delete_weight(request: web.Request) -> web.Response:
    return await mutate(request, "weight.delete")


async def edit_entry(request: web.Request) -> web.Response:
    return await mutate(request, "entry.update")


async def restore_entry(request: web.Request) -> web.Response:
    return await mutate(request, "entry.restore")


async def delete_entry(request: web.Request) -> web.Response:
    return await mutate(request, "entry.delete")


async def get_entry(request: web.Request) -> web.Response:
    entry = await asyncio.to_thread(
        request.app[DATABASE].get_entry,
        request["user_id"],
        int(request.match_info["entry_id"]),
    )
    if entry is None:
        raise NotFound
    return web.json_response(entry_data(entry))


async def save_favorite(request: web.Request) -> web.Response:
    result, _created = await asyncio.to_thread(
        request.app[DATABASE].add_favorite_from_entry,
        request["user_id"],
        int(request.match_info["entry_id"]),
    )
    return web.json_response(favorite_data(result), status=201)


async def static_file(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "index.html")
    types = {
        "index.html": "text/html",
        "app.js": "text/javascript",
        "app.css": "text/css",
    }
    if name not in types:
        raise web.HTTPNotFound()
    body, etag = request.app[ASSETS][name]
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    validators = request.headers.get("If-None-Match", "").split(",")
    if any(value.strip().removeprefix("W/") in (etag, "*") for value in validators):
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type=types[name], headers=headers)


def build_web_app(
    settings: Settings, database: Database | None = None
) -> web.Application:
    app = web.Application(middlewares=[boundary], client_max_size=16384)
    store = database or Database(settings.database_path)
    store.initialize()
    app[DATABASE], app[SETTINGS] = store, settings
    # Restart on deployment: each process serves one consistent asset snapshot.
    app[ASSETS] = {}
    for name in ("index.html", "app.js", "app.css"):
        body = (STATIC / name).read_bytes()
        app[ASSETS][name] = (body, '"' + hashlib.sha256(body).hexdigest() + '"')
    app.add_routes(
        [
            web.get("/", static_file),
            web.get("/static/{name}", static_file),
            web.get("/api/diary", diary),
            web.put("/api/profile", profile),
            web.get("/api/favorites", favorites),
            web.get("/api/recent", recent),
            web.get("/api/statistics", statistics),
            web.get("/api/weights", weights),
            web.post("/api/weights", add_weight),
            web.patch("/api/weights/{weight_id}", edit_weight),
            web.delete("/api/weights/{weight_id}", delete_weight),
            web.post("/api/entries", add_entry),
            web.get("/api/entries/{entry_id}", get_entry),
            web.patch("/api/entries/{entry_id}", edit_entry),
            web.delete("/api/entries/{entry_id}", delete_entry),
            web.post("/api/entries/{entry_id}/restore", restore_entry),
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
