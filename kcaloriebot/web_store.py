"""Owner-scoped web operations, independent of the bot's conversation sessions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta

from .database import Database, name_filter
from .domain import (
    MAX_CALORIES_PER_100G,
    MAX_SERVING_GRAMS,
    FavoriteFood,
    FoodEntry,
    Ingredient,
    NotFound,
    NutritionTotals,
    StateConflict,
    ValidationError,
    check_calories_per_100g,
    check_serving_grams,
    check_weight_kg,
    day_bounds,
    local_date,
    EARLIEST_DIARY_DATE,
    month_bounds,
    local_datetime,
    normalize_food_name,
    parse_entry_time,
    parse_quick_add,
    per_unit_from_totals,
    recipe_nutrition,
    scale_per_100,
    scale_per_serving,
)

MAX_RECIPE_INGREDIENTS = 40


def json_text(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def number(data: dict, key: str, optional: bool = False) -> float | None:
    value = data.get(key)
    if optional and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValidationError(f"Некорректное число: {key}.")
    return float(value)


def entry_data(entry: FoodEntry) -> dict:
    result = asdict(entry)
    result["version"] = hashlib.sha256(json_text(result).encode()).hexdigest()
    unit, calories, protein, fat, carbs = per_unit_from_totals(entry.nutrition)
    result["unit"] = unit
    result["values"] = dict(calories=calories, protein=protein, fat=fat, carbs=carbs)
    return result


def weight_data(record) -> dict:
    result = asdict(record)
    result["version"] = hashlib.sha256(json_text(result).encode()).hexdigest()
    return result


def favorite_data(favorite: FavoriteFood) -> dict:
    result = asdict(favorite)
    result["version"] = hashlib.sha256(json_text(result).encode()).hexdigest()
    return result


def recipe_data(recipe, ingredients) -> dict:
    """A recipe with the nutrition of the whole dish and of 100 g of it."""
    items = [
        {
            "name": row["name"],
            "grams": row["grams"],
            "calories": row["calories_per_100g"],
            "protein": row["protein_per_100g"],
            "fat": row["fat_per_100g"],
            "carbs": row["carbs_per_100g"],
        }
        for row in ingredients
    ]
    result = {
        "recipe_id": recipe["recipe_id"],
        "name": recipe["name"],
        "cooked_grams": recipe["cooked_grams"],
        "updated_at_utc": recipe["updated_at_utc"],
        "ingredients": items,
    }
    result["version"] = hashlib.sha256(json_text(result).encode()).hexdigest()
    nutrition = recipe_nutrition(
        tuple(ingredient(item) for item in items), recipe["cooked_grams"]
    )
    result["totals"] = {
        "raw_grams": nutrition.raw_grams,
        "grams": nutrition.grams,
        "calories": nutrition.calories,
        "protein": nutrition.protein,
        "fat": nutrition.fat,
        "carbs": nutrition.carbs,
    }
    result["per_100g"] = dict(
        zip(("calories", "protein", "fat", "carbs"), nutrition.per_100g())
    )
    result["incomplete"] = list(nutrition.incomplete)
    return result


def ingredient(item: dict) -> Ingredient:
    return Ingredient(
        item["name"],
        item["grams"],
        item["calories"],
        item["protein"],
        item["fat"],
        item["carbs"],
    )


def entry_time(raw, zone: str, now: int, existing: FoodEntry | None = None) -> int:
    if not isinstance(raw, str):
        raise ValidationError("Укажите дату и время записи.")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValidationError("Укажите дату и время записи.") from None
    if (
        existing
        and local_datetime(existing.eaten_at_utc, zone).strftime("%Y-%m-%dT%H:%M")
        == raw
    ):
        return existing.eaten_at_utc
    epoch = parse_entry_time(parsed.strftime("%Y-%m-%d %H:%M"), zone, now)
    if local_datetime(epoch, zone).replace(tzinfo=None) != parsed:
        raise ValidationError("Такого местного времени нет из-за перевода часов.")
    return epoch


class WebStore(Database):
    def selected_day(self, user_id, query):
        zone = self.get_timezone(user_id)
        if zone is None:
            raise ValidationError("Сначала укажите часовой пояс.")
        today = local_date(self.now_epoch(), zone)
        day = date.fromisoformat(query.get("day", today.isoformat()))
        if not EARLIEST_DIARY_DATE <= day <= today:
            raise ValidationError("Выберите доступную дату дневника.")
        return day, zone

    def statistics(self, user_id, query):
        day, zone = self.selected_day(user_id, query)
        period = query.get("period", "week")
        if period == "week":
            start_day = max(EARLIEST_DIARY_DATE, day - timedelta(days=6))
            end = day_bounds(day, zone).end_utc
        elif period == "month":
            start_day = day.replace(day=1)
            end = min(
                month_bounds(day.year, day.month, zone).end_utc,
                day_bounds(local_date(self.now_epoch(), zone), zone).end_utc,
            )
        else:
            raise ValidationError("Выберите неделю или месяц.")
        start = day_bounds(start_day, zone).start_utc
        end_day = local_date(end - 1, zone)
        rows = self.daily_breakdown(user_id, start, end, zone, limit=31)
        known = {item.day: item for item in rows.items}
        days = []
        cursor = start_day
        while cursor <= end_day:
            values = (
                asdict(known[cursor])
                if cursor in known
                else {"entry_count": 0, "calories": None}
            )
            days.append({**values, "day": cursor.isoformat()})
            cursor += timedelta(days=1)
        totals = asdict(self.stats(user_id, start, end))
        return {
            "period": period,
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "days": days,
            "totals": totals,
            "logged_days": len(known),
            "average_logged_day": totals["calories"] / len(known) if known else None,
        }

    def weight_snapshot(self, user_id, query):
        day, zone = self.selected_day(user_id, query)
        offset = int(query.get("offset", 0))
        self._validate_page(offset, 30)
        end = day_bounds(day, zone).end_utc
        week = day_bounds(day - timedelta(days=6), zone).start_utc
        previous = day_bounds(day - timedelta(days=13), zone).start_utc
        chart_start = day_bounds(day - timedelta(days=29), zone).start_utc
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM weights WHERE user_id=? AND measured_at_utc < ? ORDER BY measured_at_utc DESC, weight_id DESC LIMIT 31 OFFSET ?",
                (user_id, end, offset),
            ).fetchall()
            latest = connection.execute(
                "SELECT * FROM weights WHERE user_id=? AND measured_at_utc < ? ORDER BY measured_at_utc DESC, weight_id DESC LIMIT 1",
                (user_id, end),
            ).fetchone()
            chart = connection.execute(
                "SELECT measured_at_utc, weight_kg FROM weights WHERE user_id=? AND measured_at_utc>=? AND measured_at_utc<? ORDER BY measured_at_utc",
                (user_id, chart_start, end),
            ).fetchall()
        daily = {}
        for row in chart:
            key = local_date(row["measured_at_utc"], zone).isoformat()
            daily.setdefault(key, []).append(row["weight_kg"])
        return {
            "day": day.isoformat(),
            "items": [weight_data(self._row_to_weight(row)) for row in rows[:30]],
            "offset": offset,
            "has_next": len(rows) > 30,
            "latest": weight_data(self._row_to_weight(latest)) if latest else None,
            "average": self.average_weight(user_id, week, end),
            "previous_average": self.average_weight(user_id, previous, week),
            "days": [
                {"day": key, "weight_kg": sum(values) / len(values)}
                for key, values in daily.items()
            ],
        }

    def mutate(self, user_id: int, key: str, action: str, data: dict) -> dict:
        """Commit the result and its replay receipt in the SAME SQLite transaction.

        Receipts are retained: even a retry after a restart or a later edit/delete
        cannot repeat the original operation. Invalid requests consume no key.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise ValidationError("Нужен корректный Idempotency-Key.")
        digest = hashlib.sha256(json_text([action, data]).encode()).hexdigest()
        now = self.now_epoch()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                "SELECT request_hash, response_json FROM web_operations WHERE user_id=? AND operation_key=?",
                (user_id, key),
            ).fetchone()
            if receipt:
                if receipt["request_hash"] != digest:
                    raise StateConflict(
                        "Этот ключ уже использован для другого запроса."
                    )
                return json.loads(receipt["response_json"])
            user = connection.execute(
                "SELECT timezone FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if not user or not user["timezone"]:
                raise ValidationError("Сначала укажите часовой пояс.")
            result = self._apply(
                connection, user_id, action, data, user["timezone"], now
            )
            connection.execute(
                "INSERT INTO web_operations VALUES (?, ?, ?, ?, ?)",
                (user_id, key, digest, json_text(result), now),
            )
            connection.execute(
                "DELETE FROM web_deleted_entries WHERE deleted_at_utc < ?", (now - 900,)
            )
            return result

    def _owned_entry(self, connection, user_id, entry_id):
        row = connection.execute(
            "SELECT * FROM food_entries WHERE user_id=? AND entry_id=?",
            (user_id, entry_id),
        ).fetchone()
        if row is None:
            raise NotFound("Запись не найдена.")
        return self._row_to_entry(row)

    def _apply(self, connection, user_id, action, data, zone, now):
        if action.startswith("weight."):
            return self._weight(connection, user_id, action, data, zone, now)
        if action.startswith("recipe."):
            return self._recipe(connection, user_id, action, data, zone, now)
        if action == "entry.create":
            return self._create(connection, user_id, data, zone, now)
        entry_id = data.get("entry_id")
        if type(entry_id) is not int or entry_id <= 0:
            raise ValidationError("Некорректная запись.")
        if action == "entry.restore":
            row = connection.execute(
                "SELECT * FROM web_deleted_entries WHERE user_id=? AND entry_id=?",
                (user_id, entry_id),
            ).fetchone()
            if not row or now - row["deleted_at_utc"] > 900:
                raise NotFound("Время отмены удаления истекло.")
            saved = json.loads(row["entry_json"])
            totals = saved["nutrition"]
            connection.execute(
                "INSERT INTO food_entries(entry_id,user_id,eaten_at_utc,name,grams,servings,calories,protein,fat,carbs) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    user_id,
                    saved["eaten_at_utc"],
                    saved["name"],
                    totals["grams"],
                    totals["servings"],
                    totals["calories"],
                    totals["protein"],
                    totals["fat"],
                    totals["carbs"],
                ),
            )
            connection.execute(
                "DELETE FROM web_deleted_entries WHERE user_id=? AND entry_id=?",
                (user_id, entry_id),
            )
            return entry_data(self._owned_entry(connection, user_id, entry_id))
        entry = self._owned_entry(connection, user_id, entry_id)
        if (
            action in ("entry.update", "entry.delete")
            and data.get("version") != entry_data(entry)["version"]
        ):
            raise StateConflict(
                "Запись изменилась. Обновите дневник и откройте её заново."
            )
        if action == "entry.update":
            timestamp = entry_time(data.get("eaten_at"), zone, now, entry)
            unit = "serving" if entry.nutrition.servings is not None else "100g"
            serving_grams = None
            if unit == "serving" and entry.nutrition.grams is not None:
                serving_grams = entry.nutrition.grams / entry.nutrition.servings
            totals = self._totals(data, unit, serving_grams)
            name = (
                None
                if entry.name is None and data.get("name") in (None, "")
                else self._name(data)
            )
            connection.execute(
                "UPDATE food_entries SET name=?,eaten_at_utc=?,grams=?,servings=?,calories=?,protein=?,fat=?,carbs=? WHERE user_id=? AND entry_id=?",
                (
                    name,
                    timestamp,
                    totals.grams,
                    totals.servings,
                    totals.calories,
                    totals.protein,
                    totals.fat,
                    totals.carbs,
                    user_id,
                    entry_id,
                ),
            )
            return entry_data(self._owned_entry(connection, user_id, entry_id))
        if action == "entry.delete":
            connection.execute(
                "INSERT INTO web_deleted_entries VALUES (?,?,?,?)",
                (user_id, entry_id, json_text(asdict(entry)), now),
            )
            connection.execute(
                "DELETE FROM food_entries WHERE user_id=? AND entry_id=?",
                (user_id, entry_id),
            )
            return {"ok": True, "entry_id": entry_id, "undo_until": now + 900}
        raise ValidationError("Неизвестная операция.")

    @staticmethod
    def _name(data):
        if not isinstance(data.get("name"), str):
            raise ValidationError("Укажите название еды.")
        return normalize_food_name(data["name"])

    def _weight(self, connection, user_id, action, data, zone, now):
        if action == "weight.create":
            timestamp = entry_time(data.get("measured_at"), zone, now)
            value = check_weight_kg(number(data, "weight_kg"))
            cursor = connection.execute(
                "INSERT INTO weights(user_id, measured_at_utc, weight_kg) VALUES (?,?,?)",
                (user_id, timestamp, value),
            )
            weight_id = cursor.lastrowid
        else:
            weight_id = data.get("weight_id")
            row = connection.execute(
                "SELECT * FROM weights WHERE user_id=? AND weight_id=?",
                (user_id, weight_id),
            ).fetchone()
            if row is None:
                raise NotFound("Измерение не найдено.")
            if data.get("version") != weight_data(self._row_to_weight(row))["version"]:
                raise StateConflict("Измерение изменилось. Откройте журнал заново.")
            if action == "weight.delete":
                connection.execute(
                    "DELETE FROM weights WHERE user_id=? AND weight_id=?",
                    (user_id, weight_id),
                )
                return {"ok": True}
            value = check_weight_kg(number(data, "weight_kg"))
            connection.execute(
                "UPDATE weights SET weight_kg=? WHERE user_id=? AND weight_id=?",
                (value, user_id, weight_id),
            )
        row = connection.execute(
            "SELECT * FROM weights WHERE user_id=? AND weight_id=?",
            (user_id, weight_id),
        ).fetchone()
        return weight_data(self._row_to_weight(row))

    @staticmethod
    def _totals(data, unit="100g", serving_grams=None) -> NutritionTotals:
        calories = number(data, "calories")
        amount = number(data, "amount" if unit == "serving" else "grams")
        macros = [number(data, key, True) for key in ("protein", "fat", "carbs")]
        if unit == "serving":
            return scale_per_serving(
                calories, amount, *macros, serving_grams=serving_grams
            )
        return scale_per_100(
            check_calories_per_100g(calories), check_serving_grams(amount), *macros
        )

    def _create(self, connection, user_id, data, zone, now):
        timestamp = entry_time(data.get("eaten_at"), zone, now)
        if "quick_add" in data:
            if not isinstance(data["quick_add"], str) or len(data["quick_add"]) > 500:
                raise ValidationError("Введите название, ккал на 100 г и вес.")
            quick = parse_quick_add(data["quick_add"])
            if quick is None:
                raise ValidationError("Пример: овсянка 370 60 б12 ж6 у62")
            name = quick.name
            totals = self._totals(
                {
                    "calories": quick.calories_per_100g,
                    "grams": quick.serving_grams,
                    "protein": quick.protein_per_100g,
                    "fat": quick.fat_per_100g,
                    "carbs": quick.carbs_per_100g,
                }
            )
        elif "favorite_id" in data:
            if type(data["favorite_id"]) is not int:
                raise ValidationError("Некорректное избранное.")
            row = connection.execute(
                "SELECT * FROM favorite_foods WHERE user_id=? AND favorite_id=?",
                (user_id, data["favorite_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Избранное не найдено.")
            favorite = self._row_to_favorite(row)
            if data.get("favorite_version") != favorite_data(favorite)["version"]:
                raise StateConflict(
                    "Избранное изменилось. Найдите и выберите продукт заново."
                )
            values = {
                key: getattr(favorite, key + "_per_100g")
                for key in ("calories", "protein", "fat", "carbs")
            }
            values["amount" if favorite.unit == "serving" else "grams"] = number(
                data, "amount"
            )
            totals = self._totals(values, favorite.unit, favorite.serving_grams)
            name = favorite.name
        else:
            unit = data.get("unit", "100g")
            if unit not in ("100g", "serving"):
                raise ValidationError("Некорректная единица измерения.")
            name = self._name(data)
            totals = self._totals(data, unit, number(data, "serving_grams", True))
        entry_id = self._insert_entry(connection, user_id, timestamp, name, totals)
        return entry_data(self._owned_entry(connection, user_id, entry_id))

    def recipes(self, user_id: int) -> dict:
        with self._connect() as connection:
            recipes = connection.execute(
                "SELECT * FROM recipes WHERE user_id=? ORDER BY updated_at_utc DESC, recipe_id DESC",
                (user_id,),
            ).fetchall()
            rows = connection.execute(
                "SELECT i.* FROM recipe_ingredients i JOIN recipes r USING (recipe_id) WHERE r.user_id=? ORDER BY i.recipe_id, i.position",
                (user_id,),
            ).fetchall()
        ingredients = {}
        for row in rows:
            ingredients.setdefault(row["recipe_id"], []).append(row)
        return {
            "items": [
                recipe_data(row, ingredients.get(row["recipe_id"], []))
                for row in recipes
            ]
        }

    def ingredient_suggestions(self, user_id: int, query: str) -> dict:
        """Per-100 g values to fill an ingredient from: favorites first, then
        ingredients of the user's recipes, newest recipe first."""
        items, seen = [], set()

        def add(name, calories, protein, fat, carbs, source):
            macros = (protein, fat, carbs)
            if (
                name.casefold() in seen
                or not 0 <= calories <= MAX_CALORIES_PER_100G
                or any(value is not None and value > 100 for value in macros)
                or sum(value or 0.0 for value in macros) > 100.000001
            ):
                return
            seen.add(name.casefold())
            items.append(
                {
                    "name": name,
                    "calories": calories,
                    "protein": protein,
                    "fat": fat,
                    "carbs": carbs,
                    "source": source,
                }
            )

        for favorite in self.search_favorites(user_id, query, 20):
            values = [
                getattr(favorite, key + "_per_100g")
                for key in ("calories", "protein", "fat", "carbs")
            ]
            if favorite.unit == "serving":
                # A serving of unknown weight cannot be weighed into a recipe.
                if not favorite.serving_grams:
                    continue
                factor = 100.0 / favorite.serving_grams
                values = [None if value is None else value * factor for value in values]
            add(favorite.name, *values, "favorite")
        conditions, patterns = name_filter(query, "i.name_key")
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT i.* FROM recipe_ingredients i JOIN recipes r USING (recipe_id) WHERE r.user_id=? AND {conditions} ORDER BY r.updated_at_utc DESC, r.recipe_id DESC, i.position LIMIT 60",
                (user_id, *patterns),
            ).fetchall()
        for row in rows:
            add(
                row["name"],
                row["calories_per_100g"],
                row["protein_per_100g"],
                row["fat_per_100g"],
                row["carbs_per_100g"],
                "recipe",
            )
        return {"items": items[:8]}

    def _owned_recipe(self, connection, user_id, recipe_id):
        row = connection.execute(
            "SELECT * FROM recipes WHERE user_id=? AND recipe_id=?",
            (user_id, recipe_id),
        ).fetchone()
        if row is None:
            raise NotFound("Блюдо не найдено.")
        ingredients = connection.execute(
            "SELECT * FROM recipe_ingredients WHERE recipe_id=? ORDER BY position",
            (recipe_id,),
        ).fetchall()
        return recipe_data(row, ingredients)

    def _recipe(self, connection, user_id, action, data, zone, now):
        recipe_id = None
        if action != "recipe.create":
            recipe_id = data.get("recipe_id")
            if type(recipe_id) is not int or recipe_id <= 0:
                raise ValidationError("Некорректное блюдо.")
            current = self._owned_recipe(connection, user_id, recipe_id)
            if data.get("version") != current["version"]:
                raise StateConflict("Блюдо изменилось. Откройте его заново.")
            if action == "recipe.delete":
                favorite_id = connection.execute(
                    "SELECT favorite_id FROM recipes WHERE recipe_id=?", (recipe_id,)
                ).fetchone()["favorite_id"]
                connection.execute(
                    "DELETE FROM recipes WHERE user_id=? AND recipe_id=?",
                    (user_id, recipe_id),
                )
                if favorite_id is not None:
                    connection.execute(
                        "DELETE FROM favorite_foods WHERE user_id=? AND favorite_id=?",
                        (user_id, favorite_id),
                    )
                return {"ok": True, "recipe_id": recipe_id}
            if action != "recipe.update":
                raise ValidationError("Неизвестная операция.")
        if set(data) - {
            "recipe_id",
            "version",
            "name",
            "cooked_grams",
            "ingredients",
            "portion",
        }:
            raise ValueError
        name = self._name(data)
        items = self._ingredients(data.get("ingredients"))
        cooked = number(data, "cooked_grams", True)
        if cooked is not None and not 0 < cooked <= MAX_SERVING_GRAMS:
            raise ValidationError("Вес готового блюда — больше 0 и не больше 100 кг.")
        per_100 = self._dish_per_100(
            recipe_nutrition(tuple(ingredient(item) for item in items), cooked)
        )
        portion = self._portion(data.get("portion"), zone, now)
        if connection.execute(
            "SELECT 1 FROM recipes WHERE user_id=? AND name_key=? AND recipe_id IS NOT ?",
            (user_id, name.casefold(), recipe_id),
        ).fetchone():
            raise ValidationError(
                "Блюдо с таким названием уже есть — откройте его из списка или назовите иначе."
            )
        if recipe_id is None:
            recipe_id = connection.execute(
                "INSERT INTO recipes(user_id,name,name_key,cooked_grams,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?)",
                (user_id, name, name.casefold(), cooked, now, now),
            ).lastrowid
        else:
            connection.execute(
                "UPDATE recipes SET name=?,name_key=?,cooked_grams=?,updated_at_utc=? WHERE user_id=? AND recipe_id=?",
                (name, name.casefold(), cooked, now, user_id, recipe_id),
            )
            connection.execute(
                "DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,)
            )
        connection.executemany(
            "INSERT INTO recipe_ingredients VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    recipe_id,
                    position,
                    item["name"],
                    item["name"].casefold(),
                    item["grams"],
                    item["calories"],
                    item["protein"],
                    item["fat"],
                    item["carbs"],
                )
                for position, item in enumerate(items)
            ],
        )
        self._publish_recipe(connection, user_id, recipe_id, name, per_100, now)
        entry = None
        if portion is not None:
            grams, timestamp = portion
            entry_id = self._insert_entry(
                connection,
                user_id,
                timestamp,
                name,
                scale_per_100(per_100[0], grams, *per_100[1:]),
            )
            entry = entry_data(self._owned_entry(connection, user_id, entry_id))
        return {
            "recipe": self._owned_recipe(connection, user_id, recipe_id),
            "entry": entry,
        }

    @staticmethod
    def _ingredients(raw) -> list[dict]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("Добавьте хотя бы один ингредиент.")
        if len(raw) > MAX_RECIPE_INGREDIENTS:
            raise ValidationError(
                f"В блюде может быть не больше {MAX_RECIPE_INGREDIENTS} ингредиентов."
            )
        items = []
        for position, item in enumerate(raw, 1):
            if not isinstance(item, dict) or set(item) - {
                "name",
                "grams",
                "calories",
                "protein",
                "fat",
                "carbs",
            }:
                raise ValueError
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValidationError(f"Ингредиент {position}: укажите название.")
            if len(" ".join(item["name"].split())) > 200:
                raise ValidationError(
                    f"Ингредиент {position}: название длиннее 200 символов."
                )
            name = normalize_food_name(item["name"])
            grams = number(item, "grams")
            if not 0 < grams <= MAX_SERVING_GRAMS:
                raise ValidationError(f"«{name}»: вес — больше 0 и не больше 100 кг.")
            calories = number(item, "calories")
            if not 0 <= calories <= MAX_CALORIES_PER_100G:
                raise ValidationError(f"«{name}»: ккал на 100 г — от 0 до 10 000.")
            macros = [number(item, key, True) for key in ("protein", "fat", "carbs")]
            if (
                any(value is not None and not 0 <= value <= 100 for value in macros)
                or sum(value or 0.0 for value in macros) > 100.000001
            ):
                raise ValidationError(
                    f"«{name}»: белки, жиры и углеводы — от 0 до 100 г на 100 г, в сумме не больше 100."
                )
            protein, fat, carbs = macros
            items.append(
                dict(
                    name=name,
                    grams=grams,
                    calories=calories,
                    protein=protein,
                    fat=fat,
                    carbs=carbs,
                )
            )
        return items

    @staticmethod
    def _dish_per_100(nutrition):
        """Per-100 g values of the cooked dish, checked like any food's."""
        calories, *macros = nutrition.per_100g()
        if calories > MAX_CALORIES_PER_100G:
            raise ValidationError(
                "Больше 10 000 ккал на 100 г готового блюда — проверьте вес готового блюда."
            )
        # Rounding can push a pure 100 g of macros past 100 by a hair.
        if sum(value or 0.0 for value in macros) > 100.000001:
            raise ValidationError(
                "Белков, жиров и углеводов больше, чем весит готовое блюдо — проверьте его вес."
            )
        return (
            calories,
            *(None if value is None else min(value, 100.0) for value in macros),
        )

    @staticmethod
    def _portion(raw, zone, now):
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != {"grams", "eaten_at"}:
            raise ValueError
        grams = number(raw, "grams")
        if not 0 < grams <= MAX_SERVING_GRAMS:
            raise ValidationError("Порция — больше 0 и не больше 100 кг.")
        return grams, entry_time(raw["eaten_at"], zone, now)

    def _publish_recipe(self, connection, user_id, recipe_id, name, per_100, now):
        """Keep the recipe's favorite in step, so the bot can log it by weight.

        An unlinked recipe adopts a plain favorite of the same name, as saving
        a favorite under an existing name does, but never another recipe's.
        """
        linked = connection.execute(
            "SELECT f.favorite_id FROM recipes r JOIN favorite_foods f ON f.favorite_id=r.favorite_id AND f.user_id=r.user_id WHERE r.recipe_id=?",
            (recipe_id,),
        ).fetchone()
        if linked is None:
            linked = connection.execute(
                "SELECT favorite_id FROM favorite_foods WHERE user_id=? AND name_key=? AND favorite_id NOT IN (SELECT favorite_id FROM recipes WHERE user_id=? AND favorite_id IS NOT NULL) ORDER BY favorite_id DESC LIMIT 1",
                (user_id, name.casefold(), user_id),
            ).fetchone()
        if linked is None:
            favorite_id = self._insert_favorite(
                connection, user_id, name, *per_100, now
            )
        else:
            favorite_id = linked["favorite_id"]
            connection.execute(
                "UPDATE favorite_foods SET name=?,name_key=?,unit='100g',serving_grams=NULL,calories_per_100g=?,protein_per_100g=?,fat_per_100g=?,carbs_per_100g=?,updated_at_utc=? WHERE user_id=? AND favorite_id=?",
                (name, name.casefold(), *per_100, now, user_id, favorite_id),
            )
        connection.execute(
            "UPDATE recipes SET favorite_id=? WHERE recipe_id=?",
            (favorite_id, recipe_id),
        )
