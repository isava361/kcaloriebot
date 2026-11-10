"""Recognize food (and body-weight scale readings) in photos with Claude.

The same request handles a meal the owner describes in a text message: it is
sent with no images, as the description to record.
"""

from __future__ import annotations

import base64
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional, Sequence

import anthropic

from .domain import (
    MAX_SERVING_GRAMS,
    MAX_WEIGHT_KG,
    MIN_WEIGHT_KG,
    UNIT_SERVING,
    FavoriteFood,
    FoodEntry,
    ValidationError,
    check_calories_per_100g,
    normalize_food_name,
    per_unit_from_totals,
    validate_macro_sum,
)

logger = logging.getLogger(__name__)

# The API takes up to 10 MB per image once base64-encoded (a third larger than
# the raw bytes); 5 MB of raw bytes stays well inside that on every platform.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# A Telegram album holds at most 10 items. Above 20 images the API applies a
# stricter per-image size limit, and the whole request must stay under 128 MB.
MAX_IMAGES_PER_REQUEST = 10
MAX_REQUEST_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

# Searches allowed per request, and pause_turn resumptions of a long search turn.
MAX_WEB_SEARCHES = 3
MAX_CONTINUATIONS = 3
RECORD_TOOL_NAME = "record_foods"

# The owner tracks calories to stay lean, and portion weights judged by eye
# come out too low more often than too high. The model reports its honest best
# estimate and a plausible range; the bot logs this share of the way from the
# best estimate up to the top of the range (0 = the best estimate, 1 = the top).
# Only estimated weights are adjusted: judged by eye from a photo, or a typical
# portion assumed for food the owner named without a weight, which errs low
# for the same reason. A weight from a package, the owner or a saved food, and
# every nutrition value, are logged as reported.
EYE_ESTIMATE_UPWARD_SHARE = 0.5
# A range topping out above this multiple of the best estimate says the model
# had no real idea; the excess is ignored rather than inflating the entry.
MAX_ESTIMATE_RANGE_FACTOR = 2.0

# Where an item's nutrition per 100 g came from.
NUTRITION_LABEL = "label"
NUTRITION_MANUFACTURER = "manufacturer"
NUTRITION_OWNER = "owner"
NUTRITION_SAVED = "saved"
NUTRITION_ESTIMATE = "estimate"
NUTRITION_SOURCES = (
    NUTRITION_LABEL,
    NUTRITION_MANUFACTURER,
    NUTRITION_OWNER,
    NUTRITION_SAVED,
    NUTRITION_ESTIMATE,
)
# Where an item's weight came from.
GRAMS_PACKAGE = "package"
GRAMS_OWNER = "owner"
GRAMS_SAVED = "saved"
GRAMS_ESTIMATE = "estimate"
GRAMS_UNKNOWN = "unknown"
GRAMS_SOURCES = (
    GRAMS_PACKAGE,
    GRAMS_OWNER,
    GRAMS_SAVED,
    GRAMS_ESTIMATE,
    GRAMS_UNKNOWN,
)

# The owner's own foods sent with each request: his newest favorites, then his
# latest distinct logged foods. About 60-100 characters per line, so the block
# stays around a thousand tokens at most.
MAX_OWNER_FAVORITES = 30
MAX_OWNER_RECENT = 20
MAX_OWNER_FOODS_CHARS = 4000
MAX_OWNER_FOOD_NAME_CHARS = 80

# Bounds on free text the model writes, which ends up in a Telegram message.
MAX_FOODS = 30
MAX_COMMENT_CHARS = 600
MAX_QUESTION_CHARS = 300

SYSTEM_PROMPT = """\
You help the owner of a personal calorie diary log what he eats. He sends one \
or more photos, sometimes with a caption, or describes in a message what he \
ate. Photos sent together show the same meal, for example a dish and the \
label of its package. You record what he ate by calling the record_foods \
tool exactly once.

The bot shows your record to the owner as a proposal. He checks it, may reply \
with a correction or an answer to your question, and only then saves it to \
the diary. So the record should be your honest best reading of what he sent, \
and it should make clear which numbers he can trust and which are guesses \
worth checking.

What to record. Each distinct food or drink he is about to eat or has eaten \
is one item; list clearly separate components separately (a side dish and a \
cutlet) and keep a mixed dish (soup, salad, pizza) as one item. Names are \
short and in Russian, e.g. "Гречка отварная", "Куриная грудка жареная". \
Nutrition is per 100 g of the food as eaten, including the cooking oil, \
butter and sauce it was made or served with; convert per-serving or per-item \
figures to per 100 g. If a body-weight scale display is in a photo, put the \
reading in weight_kg, otherwise 0. If there is nothing to log (a message \
that names no food or drink, such as a thank-you or a question), record an \
empty foods list and say why in a few words in comment.

Sources. For every item, say where its nutrition and its weight came from; \
the owner uses this to decide what to double-check, and the bot treats \
estimated weights differently. nutrition_source is "label" (a nutrition \
table in a photo), "manufacturer" (the maker's or restaurant's published \
figures, including what web_search found), "owner" (figures he wrote in the \
caption, his message or a reply), "saved" (one of his own foods listed in \
the message) or "estimate" (typical values you know). grams_source is \
"package" (net weight printed on the package), "owner" (a weight he wrote), \
"saved" (a clear number of servings of a saved food), "estimate" (judged by \
eye from a photo, or a typical portion you assume for food he named without \
a weight) or "unknown". A count he gives ("два куска хлеба") is his, but the \
weight of one piece is your estimate. The owner's words come first, then a \
visible label, then his saved foods, then manufacturer data, then your own \
estimate. When his photos or words clearly show one of his saved foods, use \
its listed values exactly and put its number in saved_food; otherwise \
saved_food is 0. A saved food listed per \
serving with no serving weight still needs grams and per-100 g values: \
estimate the weight of one serving and convert, so the calories per serving \
stay as listed. Label and manufacturer figures are exact; do not adjust them \
or call them approximate.

Weight. When you judge the weight by eye from plates, cutlery, hands and \
packaging, or assume a typical portion ("тарелка борща", "круассан"), give \
your honest best estimate in grams and the plausible range around it in \
grams_low and grams_high; do not skew the estimate up or down, the bot does \
its own adjustment. For any other grams_source, set grams_low \
and grams_high equal to grams. When the nutrition is known but nothing shows \
how much was eaten (only a label in frame, say), set grams_source to \
"unknown" and the three weight fields to 0 instead of inventing a weight; the \
bot will ask the owner.

Questions. If something neither the photos nor his words show would change \
the calories a lot (whether food was fried in oil, whether a multi-serving \
package was eaten whole or only partly, what a dish is made of), ask the \
owner about it in question: one short question in Russian. Still record your \
best guess for it, so he can save without answering. Ask at most one \
question, and leave question empty when the remaining uncertainty is minor. \
A single-serving package (a bar, a yogurt, a can of a drink) is eaten whole \
unless he says otherwise.

Searching. Use web_search only for a specific branded or packaged product, or \
a dish from a named restaurant or chain, whose nutrition you do not know and \
whose label is not visible. Never search for ordinary home food. Your \
knowledge of products may be out of date, so prefer a search over a guess for \
those.

comment: one or two short Russian sentences on what you recorded and where \
the main numbers come from ("по этикетке", "по данным производителя", \
"оценка по фото", "типичная порция"). Do not repeat the question there.
"""

_FOOD_PROPERTIES: dict[str, Any] = {
    "name": {"type": "string"},
    "nutrition_source": {"type": "string", "enum": list(NUTRITION_SOURCES)},
    "saved_food": {
        "type": "integer",
        "description": "Number of the owner's saved food used, or 0.",
    },
    "calories_per_100g": {"type": "number"},
    "protein_per_100g": {"type": "number"},
    "fat_per_100g": {"type": "number"},
    "carbs_per_100g": {"type": "number"},
    "grams_source": {"type": "string", "enum": list(GRAMS_SOURCES)},
    "grams": {
        "type": "number",
        "description": "Best estimate of the weight eaten; 0 when unknown.",
    },
    "grams_low": {"type": "number"},
    "grams_high": {"type": "number"},
}

# Every property is required and none is nullable: strict tool schemas allow
# only a small number of optional and union-typed parameters per request, so
# "nothing" is expressed as 0, an empty string or the "unknown" source.
RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "foods": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _FOOD_PROPERTIES,
                "required": list(_FOOD_PROPERTIES),
                "additionalProperties": False,
            },
        },
        "weight_kg": {"type": "number"},
        "question": {
            "type": "string",
            "description": "One short question to the owner in Russian, or empty.",
        },
        "comment": {"type": "string"},
    },
    "required": ["foods", "weight_kg", "question", "comment"],
    "additionalProperties": False,
}


TOOLS: list[dict[str, Any]] = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": MAX_WEB_SEARCHES,
    },
    {
        "name": RECORD_TOOL_NAME,
        "description": "Record the foods he ate (and a scale reading in a photo).",
        "strict": True,
        "input_schema": RECORD_SCHEMA,
    },
]


class PhotoUnavailable(Exception):
    """Raised when the photo could not be analyzed."""


@dataclass(frozen=True)
class PhotoImage:
    data: bytes
    media_type: str


@dataclass(frozen=True)
class OwnerFood:
    """One of the owner's own foods, as offered to the model.

    Values are per 100 g, unless ``per_serving`` is set: then they are per one
    serving whose weight was never saved, so they cannot be converted. A
    serving food with a known weight is converted to per 100 g, and
    ``serving_grams`` keeps that weight so the model can count servings.
    """

    name: str
    calories: float
    protein: Optional[float]
    fat: Optional[float]
    carbs: Optional[float]
    per_serving: bool = False
    serving_grams: Optional[float] = None

    def per_100g(
        self,
    ) -> Optional[tuple[float, Optional[float], Optional[float], Optional[float]]]:
        if self.per_serving:
            return None
        return self.calories, self.protein, self.fat, self.carbs


@dataclass(frozen=True)
class FoodGuess:
    name: str
    # The weight to log, or None when the model could not tell it.
    grams: Optional[float]
    calories_per_100g: float
    protein_per_100g: Optional[float]
    fat_per_100g: Optional[float]
    carbs_per_100g: Optional[float]
    nutrition_source: str = NUTRITION_ESTIMATE
    grams_source: str = GRAMS_ESTIMATE
    # (low, best, high) of a weight judged by eye, as the model reported it;
    # ``grams`` is then adjusted upwards within this range.
    grams_range: Optional[tuple[float, float, float]] = None
    # 1-based number of the owner's food used, or 0.
    saved_food: int = 0


@dataclass(frozen=True)
class PhotoAnalysis:
    foods: tuple[FoodGuess, ...]
    weight_kg: Optional[float]
    comment: str
    question: str = ""

    @property
    def missing_weight(self) -> tuple[str, ...]:
        """Names of the foods that cannot be logged until the owner gives a weight."""
        return tuple(food.name for food in self.foods if food.grams is None)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _round(value: float) -> float:
    return round(value, 1)


def _owner_food(
    name: Optional[str],
    unit: str,
    calories: Optional[float],
    protein: Optional[float],
    fat: Optional[float],
    carbs: Optional[float],
    serving_grams: Optional[float],
) -> Optional[OwnerFood]:
    """Normalize one saved or logged food; None when it is unusable."""
    calories = _number(calories)
    if not isinstance(name, str) or calories is None or calories < 0:
        return None
    try:
        name = normalize_food_name(name)
    except ValidationError:
        return None
    if len(name) > MAX_OWNER_FOOD_NAME_CHARS:
        name = name[: MAX_OWNER_FOOD_NAME_CHARS - 1] + "…"
    macros = tuple(_number(value) for value in (protein, fat, carbs))
    if unit != UNIT_SERVING:
        per_serving, serving = False, None
    else:
        serving = _number(serving_grams)
        if serving is None or not 0 < serving <= MAX_SERVING_GRAMS:
            # Per-serving numbers with no weight: never pass them off as per 100 g.
            if any(value is not None and value < 0 for value in macros):
                macros = (None, None, None)
            return OwnerFood(name, _round(calories), *_rounded(macros), True)
        factor = 100.0 / serving
        calories *= factor
        macros = tuple(None if value is None else value * factor for value in macros)
        per_serving = False
    try:
        check_calories_per_100g(calories)
    except ValidationError:
        return None
    try:
        validate_macro_sum(*macros)
    except ValidationError:
        macros = (None, None, None)
    return OwnerFood(
        name,
        _round(calories),
        *_rounded(macros),
        per_serving,
        None if serving is None else _round(serving),
    )


def _rounded(
    values: Iterable[Optional[float]],
) -> tuple[Optional[float], ...]:
    return tuple(None if value is None else _round(value) for value in values)


def _favorite_food(favorite: FavoriteFood) -> Optional[OwnerFood]:
    return _owner_food(
        favorite.name,
        favorite.unit,
        favorite.calories_per_100g,
        favorite.protein_per_100g,
        favorite.fat_per_100g,
        favorite.carbs_per_100g,
        favorite.serving_grams,
    )


def _logged_food(entry: FoodEntry) -> Optional[OwnerFood]:
    totals = entry.nutrition
    try:
        unit, calories, protein, fat, carbs = per_unit_from_totals(totals)
    except ValidationError:
        return None
    serving_grams = None
    if unit == UNIT_SERVING and totals.grams and totals.servings:
        serving_grams = totals.grams / totals.servings
    return _owner_food(entry.name, unit, calories, protein, fat, carbs, serving_grams)


def _value(value: Optional[float]) -> str:
    return "unknown" if value is None else f"{value:g}"


def owner_food_line(number: int, food: OwnerFood) -> str:
    if food.per_serving:
        basis = "per serving (serving weight not saved)"
    elif food.serving_grams is not None:
        basis = f"one serving = {food.serving_grams:g} g; per 100 g"
    else:
        basis = "per 100 g"
    return (
        f"{number}. {food.name}: {basis}: {food.calories:g} kcal, "
        f"protein {_value(food.protein)}, fat {_value(food.fat)}, "
        f"carbs {_value(food.carbs)}"
    )


def owner_foods_from(
    favorites: Iterable[FavoriteFood], recent: Iterable[FoodEntry]
) -> tuple[OwnerFood, ...]:
    """The owner's foods to send: favorites first, one per name, size-bounded."""
    candidates = [*map(_favorite_food, favorites), *map(_logged_food, recent)]
    foods: list[OwnerFood] = []
    seen: set[str] = set()
    size = 0
    for food in candidates:
        if food is None or food.name.casefold() in seen:
            continue
        size += len(owner_food_line(len(foods) + 1, food)) + 1
        if size > MAX_OWNER_FOODS_CHARS:
            break
        seen.add(food.name.casefold())
        foods.append(food)
    return tuple(foods)


def owner_foods_text(foods: Sequence[OwnerFood]) -> str:
    lines = [
        "The owner's saved and recently logged foods, with his own nutrition "
        "figures. Use one when his photos or words clearly show it, and put "
        "its number in saved_food:"
    ]
    lines.extend(owner_food_line(number, food) for number, food in enumerate(foods, 1))
    return "\n".join(lines)


def _source(value: Any, allowed: Sequence[str], fallback: str) -> str:
    # Strict output does not guarantee the capitalization of enum values.
    if isinstance(value, str) and value.strip().casefold() in allowed:
        return value.strip().casefold()
    return fallback


def _saved_food_number(value: Any, owner_foods: Sequence[OwnerFood]) -> int:
    number = _number(value)
    if number is None or not number.is_integer():
        return 0
    return int(number) if 1 <= number <= len(owner_foods) else 0


def adjusted_eye_estimate(best: float, high: float) -> float:
    """The weight to log for an eye estimate: up from best towards high."""
    top = min(high, best * MAX_ESTIMATE_RANGE_FACTOR)
    return max(best, _round(best + EYE_ESTIMATE_UPWARD_SHARE * (top - best)))


_Weight = tuple[str, Optional[float], Optional[tuple[float, float, float]]]


def _weight(item: dict[str, Any], grams_source: str) -> Optional[_Weight]:
    """(source, grams to log, eye-estimate range) for one item; None to drop it."""
    if grams_source == GRAMS_UNKNOWN:
        return GRAMS_UNKNOWN, None, None
    best = _number(item.get("grams"))
    if best is None or not 0 < best <= MAX_SERVING_GRAMS:
        # A known weight of zero, or nonsense: nothing sensible to log.
        return None
    if grams_source != GRAMS_ESTIMATE:
        return grams_source, best, None
    low, high = _number(item.get("grams_low")), _number(item.get("grams_high"))
    if low is None or high is None or not 0 < low <= best <= high <= MAX_SERVING_GRAMS:
        # An inconsistent range: keep the best estimate, without adjusting it.
        return GRAMS_ESTIMATE, best, None
    return GRAMS_ESTIMATE, adjusted_eye_estimate(best, high), (low, best, high)


def _food_guess(item: Any, owner_foods: Sequence[OwnerFood]) -> Optional[FoodGuess]:
    """Turn one model-reported item into a storable guess, or None if unusable."""
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        return None
    try:
        name = normalize_food_name(item["name"])
    except ValidationError:
        return None
    nutrition_source = _source(
        item.get("nutrition_source"), NUTRITION_SOURCES, NUTRITION_ESTIMATE
    )
    grams_source = _source(item.get("grams_source"), GRAMS_SOURCES, GRAMS_ESTIMATE)
    saved = _saved_food_number(item.get("saved_food"), owner_foods)
    # A saved source without a usable reference cannot be trusted as exact.
    if nutrition_source == NUTRITION_SAVED and not saved:
        nutrition_source = NUTRITION_ESTIMATE
    if grams_source == GRAMS_SAVED and not saved:
        grams_source = GRAMS_ESTIMATE
    if nutrition_source != NUTRITION_SAVED and grams_source != GRAMS_SAVED:
        saved = 0
    exact = (
        owner_foods[saved - 1].per_100g()
        if saved and nutrition_source == NUTRITION_SAVED
        else None
    )
    if exact is not None:
        # The owner's own numbers, not the model's copy of them.
        calories, *macros = exact
    else:
        calories = _number(item.get("calories_per_100g"))
        macros = [
            _number(item.get(key))
            for key in ("protein_per_100g", "fat_per_100g", "carbs_per_100g")
        ]
    if calories is None:
        return None
    try:
        calories = check_calories_per_100g(calories)
    except ValidationError:
        return None
    try:
        validate_macro_sum(*macros)
    except ValidationError:
        # Implausible macros should not cost the entry its calories.
        macros = [None, None, None]
    weight = _weight(item, grams_source)
    if weight is None or (weight[1] is None and calories == 0):
        # Asking for the weight of a zero-calorie drink would only block saving.
        return None
    grams_source, grams, grams_range = weight
    return FoodGuess(
        name,
        grams,
        calories,
        *macros,
        nutrition_source=nutrition_source,
        grams_source=grams_source,
        grams_range=grams_range,
        saved_food=saved,
    )


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_analysis(data: Any, owner_foods: Sequence[OwnerFood] = ()) -> PhotoAnalysis:
    if not isinstance(data, dict):
        raise PhotoUnavailable("Unexpected analysis format")
    raw_foods = data.get("foods")
    foods = tuple(
        guess
        for guess in (
            _food_guess(item, owner_foods)
            for item in (raw_foods if isinstance(raw_foods, list) else [])
        )
        if guess is not None
    )[:MAX_FOODS]
    weight = _number(data.get("weight_kg"))
    if weight is not None and not MIN_WEIGHT_KG <= weight <= MAX_WEIGHT_KG:
        weight = None
    return PhotoAnalysis(
        foods,
        weight,
        _text(data.get("comment"), MAX_COMMENT_CHARS),
        _text(data.get("question"), MAX_QUESTION_CHARS),
    )


def _previous_food(food: FoodGuess) -> str:
    if food.grams is None:
        weight = "weight unknown"
    elif food.grams_range is not None:
        low, best, high = food.grams_range
        weight = f"{best:g} g (by eye, plausible {low:g}-{high:g} g)"
    else:
        weight = f"{food.grams:g} g"
    line = (
        f"- {food.name}: {weight}; per 100 g: {food.calories_per_100g:g} kcal, "
        f"protein {_value(food.protein_per_100g)}, fat {_value(food.fat_per_100g)}, "
        f"carbs {_value(food.carbs_per_100g)}; nutrition from {food.nutrition_source}"
        f", weight from {food.grams_source}"
    )
    if food.saved_food:
        line += f", saved food {food.saved_food}"
    return line


def correction_text(previous: PhotoAnalysis, corrections: Sequence[str]) -> str:
    """Ask for the previous answer again with the owner's replies applied."""
    lines = ["Your previous answer:"]
    # Eye estimates are shown as reported, before the bot's adjustment, so a
    # second round does not adjust them twice.
    lines.extend(_previous_food(food) for food in previous.foods)
    if not previous.foods:
        lines.append("- no foods")
    if previous.weight_kg is not None:
        lines.append(f"- scale reading: {previous.weight_kg:g} kg")
    if previous.question:
        lines.append(f"You asked the owner: {previous.question}")
    lines.append("The owner replied (corrections, or answers to your question):")
    lines.extend(f"- {correction}" for correction in corrections)
    lines.append(
        "Record the corrected result. Trust his replies over what he sent first "
        "and over your own estimates (a weight or nutrition he states has source "
        '"owner"), keep everything they do not mention as before, and ask a new '
        "question only if something that matters is still unclear."
    )
    return "\n".join(lines)


def _image_block(image: PhotoImage) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": base64.standard_b64encode(image.data).decode("ascii"),
        },
    }


class PhotoAnalyzer:
    def __init__(
        self, client: anthropic.AsyncAnthropic, model: str, effort: str
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort

    async def analyze(
        self,
        images: Sequence[PhotoImage],
        caption: Optional[str] = None,
        *,
        owner_foods: Sequence[OwnerFood] = (),
        previous: Optional[PhotoAnalysis] = None,
        corrections: Sequence[str] = (),
    ) -> PhotoAnalysis:
        """Analyze photos with an optional caption, or, with no images, the
        owner's text description of a meal (passed as ``caption``)."""
        words = caption.strip() if caption else ""
        if not images and not words:
            raise ValueError("Nothing to analyze: no images and no description")
        content: list[dict[str, Any]] = []
        # The owner's foods change rarely, so they go before the images.
        if owner_foods:
            content.append({"type": "text", "text": owner_foods_text(owner_foods)})
        for number, image in enumerate(images, 1):
            if len(images) > 1:
                content.append({"type": "text", "text": f"Image {number}:"})
            content.append(_image_block(image))
        if not images:
            request = f"No photos. The owner's message: {words}"
        elif words:
            request = f"Caption: {words}"
        else:
            request = "No caption."
        content.append({"type": "text", "text": request})
        if previous is not None and corrections:
            content.append(
                {"type": "text", "text": correction_text(previous, corrections)}
            )
        system = f"{SYSTEM_PROMPT}\nThe current date is {self._today().isoformat()}."
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        for _ in range(MAX_CONTINUATIONS + 1):
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=16000,
                    system=system,
                    messages=messages,
                    tools=TOOLS,
                    output_config={"effort": self._effort},
                )
            except anthropic.APIError as exc:
                logger.warning("Photo analysis request failed: %s", type(exc).__name__)
                raise PhotoUnavailable("Claude request failed") from exc
            record = next(
                (
                    block.input
                    for block in response.content
                    if block.type == "tool_use" and block.name == RECORD_TOOL_NAME
                ),
                None,
            )
            if record is not None:
                return parse_analysis(record, owner_foods)
            if response.stop_reason != "pause_turn":
                break
            # A long search turn was paused; send it back unchanged to resume.
            messages.append({"role": "assistant", "content": response.content})
        logger.warning(
            "Photo analysis returned no record stop_reason=%s request_id=%s",
            response.stop_reason,
            response._request_id,
        )
        raise PhotoUnavailable(f"Analysis stopped: {response.stop_reason}")

    @staticmethod
    def _today() -> date:
        return datetime.now(timezone.utc).date()


def build_analyzer(
    api_key: Optional[str],
    model: str,
    effort: str,
    workspace_id: Optional[str] = None,
) -> Optional[PhotoAnalyzer]:
    if not api_key:
        return None
    # Keys that are not scoped to a workspace must name one on every request.
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    client = anthropic.AsyncAnthropic(
        api_key=api_key, timeout=120.0, default_headers=headers
    )
    return PhotoAnalyzer(client, model, effort)
