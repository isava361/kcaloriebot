"""Recognize food (and body-weight scale readings) in a photo with Claude."""

from __future__ import annotations

import base64
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

import anthropic

from .domain import (
    MAX_SERVING_GRAMS,
    MAX_WEIGHT_KG,
    MIN_WEIGHT_KG,
    ValidationError,
    check_calories_per_100g,
    normalize_food_name,
    validate_macro_sum,
)

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
# Claude rejects images larger than 5 MB.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUPPORTED_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

SYSTEM_PROMPT = """\
You log food for a calorie-tracking diary from a single photo sent by its owner.

Identify every distinct food or drink the person is about to eat or has eaten
and estimate, for each one:
- name: a short Russian name, e.g. "Гречка отварная", "Куриная грудка жареная";
- grams: the weight of the portion in the photo;
- calories_per_100g, protein_per_100g, fat_per_100g, carbs_per_100g: typical
  nutrition per 100 g of that food as prepared.

Rules:
- If a nutrition label or package is visible, use its per-100 g values, and the
  net weight when the whole package is clearly being eaten.
- If the caption names the food, its weight or its nutrition, trust the caption
  over your visual estimate.
- Estimate portion size from plates, cutlery, hands and packaging. List
  components separately when they are clearly separate (a side dish and a
  cutlet), but keep a mixed dish (soup, salad, pizza) as one item.
- If the photo shows a body-weight scale display, put the reading in weight_kg;
  otherwise set weight_kg to 0.
- If there is nothing to log, return an empty foods list.
- comment: one or two short Russian sentences on what you logged and anything
  you were unsure about.
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "foods": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "grams": {"type": "number"},
                    "calories_per_100g": {"type": "number"},
                    "protein_per_100g": {"type": "number"},
                    "fat_per_100g": {"type": "number"},
                    "carbs_per_100g": {"type": "number"},
                },
                "required": [
                    "name",
                    "grams",
                    "calories_per_100g",
                    "protein_per_100g",
                    "fat_per_100g",
                    "carbs_per_100g",
                ],
                "additionalProperties": False,
            },
        },
        "weight_kg": {"type": "number"},
        "comment": {"type": "string"},
    },
    "required": ["foods", "weight_kg", "comment"],
    "additionalProperties": False,
}


class PhotoUnavailable(Exception):
    """Raised when the photo could not be analyzed."""


@dataclass(frozen=True)
class FoodGuess:
    name: str
    grams: float
    calories_per_100g: float
    protein_per_100g: Optional[float]
    fat_per_100g: Optional[float]
    carbs_per_100g: Optional[float]


@dataclass(frozen=True)
class PhotoAnalysis:
    foods: tuple[FoodGuess, ...]
    weight_kg: Optional[float]
    comment: str


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _food_guess(item: Any) -> Optional[FoodGuess]:
    """Turn one model-reported item into a storable guess, or None if unusable."""
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        return None
    grams = _number(item.get("grams"))
    calories = _number(item.get("calories_per_100g"))
    if grams is None or not 0 < grams <= MAX_SERVING_GRAMS or calories is None:
        return None
    try:
        name = normalize_food_name(item["name"])
        calories = check_calories_per_100g(calories)
    except ValidationError:
        return None
    macros = tuple(
        _number(item.get(key))
        for key in ("protein_per_100g", "fat_per_100g", "carbs_per_100g")
    )
    try:
        validate_macro_sum(*macros)
    except ValidationError:
        # Implausible macros should not cost the entry its calories.
        macros = (None, None, None)
    return FoodGuess(name, grams, calories, *macros)


def parse_analysis(data: Any) -> PhotoAnalysis:
    if not isinstance(data, dict):
        raise PhotoUnavailable("Unexpected analysis format")
    raw_foods = data.get("foods")
    foods = tuple(
        guess
        for guess in map(_food_guess, raw_foods if isinstance(raw_foods, list) else [])
        if guess is not None
    )
    weight = _number(data.get("weight_kg"))
    if weight is not None and not MIN_WEIGHT_KG <= weight <= MAX_WEIGHT_KG:
        weight = None
    comment = data.get("comment")
    return PhotoAnalysis(
        foods, weight, " ".join(comment.split()) if isinstance(comment, str) else ""
    )


class PhotoAnalyzer:
    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client

    async def analyze(
        self, image: bytes, media_type: str, caption: Optional[str] = None
    ) -> PhotoAnalysis:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image).decode("ascii"),
                },
            },
            {
                "type": "text",
                "text": f"Caption: {caption.strip()}"
                if caption and caption.strip()
                else "No caption.",
            },
        ]
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
            )
        except anthropic.APIError as exc:
            logger.warning("Photo analysis request failed: %s", type(exc).__name__)
            raise PhotoUnavailable("Claude request failed") from exc
        if response.stop_reason != "end_turn":
            logger.warning(
                "Photo analysis stopped early stop_reason=%s request_id=%s",
                response.stop_reason,
                response._request_id,
            )
            raise PhotoUnavailable(f"Analysis stopped: {response.stop_reason}")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return parse_analysis(json.loads(text))
        except json.JSONDecodeError as exc:
            raise PhotoUnavailable("Analysis was not valid JSON") from exc


def build_analyzer(api_key: Optional[str]) -> Optional[PhotoAnalyzer]:
    if not api_key:
        return None
    return PhotoAnalyzer(anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0))
