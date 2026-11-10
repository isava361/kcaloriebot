"""Photo recognition results waiting for the owner to confirm them.

Proposals live only in memory: the feature has a single user, and a proposal
lost on restart is answered with a request to send the photo again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import FoodEntry, WeightRecord, scale_per_100
from .photo_ai import (
    GRAMS_ESTIMATE,
    NUTRITION_ESTIMATE,
    FoodGuess,
    OwnerFood,
    PhotoAnalysis,
    PhotoImage,
)
from .render import number, short

# Long enough to confirm a meal photo later in the day; the caps bound the
# image bytes held for corrections (an album keeps all of its photos).
PROPOSAL_TTL_SECONDS = 24 * 60 * 60
MAX_PROPOSALS = 20
MAX_STORED_IMAGE_BYTES = 100 * 1024 * 1024
# Telegram shows a callback alert of at most 200 characters.
MAX_ALERT_CHARS = 200

OPEN = "open"
SAVED = "saved"
CANCELLED = "cancelled"

CORRECTION_HINT = (
    "Чтобы исправить, ответьте на это сообщение, например: «гречки было 150 г»."
)


@dataclass
class PhotoProposal:
    # The owner's photo message ID (the first one of an album): unique in the
    # chat and never reused, so a button left over from before a restart
    # cannot hit a newer proposal.
    key: int
    user_id: int
    chat_id: int
    images: tuple[PhotoImage, ...]
    caption: Optional[str]
    eaten_at_utc: int
    analysis: PhotoAnalysis
    # The owner's foods the analysis was given; corrections reuse the same
    # list so its numbering stays valid.
    owner_foods: tuple[OwnerFood, ...] = ()
    # Every photo of an album, so a reply to any of them finds the proposal.
    photo_ids: frozenset[int] = frozenset()
    # The bot message currently showing this proposal.
    message: Any = None
    created_at: float = 0.0
    corrections: list[str] = field(default_factory=list)
    message_ids: set[int] = field(default_factory=set)
    state: str = OPEN
    # A correction is being analyzed; saving now would save the old numbers.
    busy: bool = False

    def show_in(self, message: Any) -> None:
        self.message = message
        message_id = getattr(message, "message_id", None)
        if message_id is not None:
            self.message_ids.add(message_id)

    def close(self, state: str) -> None:
        self.state = state
        self.busy = False
        self.images = ()

    @property
    def image_bytes(self) -> int:
        return sum(len(image.data) for image in self.images)


class PhotoProposalStore:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = PROPOSAL_TTL_SECONDS,
        limit: int = MAX_PROPOSALS,
        max_image_bytes: int = MAX_STORED_IMAGE_BYTES,
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._limit = limit
        self._max_image_bytes = max_image_bytes
        self._items: dict[int, PhotoProposal] = {}

    def add(self, proposal: PhotoProposal) -> None:
        proposal.created_at = self._clock()
        self._items.pop(proposal.key, None)
        self._items[proposal.key] = proposal
        self._prune()

    def get(self, key: int, user_id: int, chat_id: int) -> Optional[PhotoProposal]:
        self._prune()
        proposal = self._items.get(key)
        if proposal is None or (proposal.user_id, proposal.chat_id) != (
            user_id,
            chat_id,
        ):
            return None
        return proposal

    def find_by_message(
        self, user_id: int, chat_id: int, message_id: int
    ) -> Optional[PhotoProposal]:
        self._prune()
        return next(
            (
                proposal
                for proposal in self._items.values()
                # A reply to the photo itself counts as well as one to the proposal.
                if (
                    message_id == proposal.key
                    or message_id in proposal.photo_ids
                    or message_id in proposal.message_ids
                )
                and (proposal.user_id, proposal.chat_id) == (user_id, chat_id)
            ),
            None,
        )

    def is_current(self, proposal: PhotoProposal) -> bool:
        return self._items.get(proposal.key) is proposal and proposal.state == OPEN

    def _prune(self) -> None:
        cutoff = self._clock() - self._ttl
        for key in [k for k, p in self._items.items() if p.created_at < cutoff]:
            del self._items[key]
        while len(self._items) > self._limit or (
            len(self._items) > 1
            and sum(p.image_bytes for p in self._items.values()) > self._max_image_bytes
        ):
            del self._items[next(iter(self._items))]


def has_proposal_buttons(message: Any) -> bool:
    """True for a bot message that showed proposal buttons (even an old one)."""
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or ()
    return any(
        str(getattr(button, "callback_data", "") or "").startswith("photo:")
        for row in rows
        for button in row
    )


def _grams(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.0f}"


def _macros(
    protein: Optional[float], fat: Optional[float], carbs: Optional[float]
) -> str:
    return f"Б {_grams(protein)} · Ж {_grams(fat)} · У {_grams(carbs)}"


def _estimate_note(food: FoodGuess) -> str:
    """Point out what was judged by eye: those are the numbers worth checking."""
    parts = []
    if food.grams_source == GRAMS_ESTIMATE:
        if food.grams_range is not None:
            low, _, high = food.grams_range
            parts.append(f"вес {number(low)}–{number(high)} г")
        else:
            parts.append("вес")
    if food.nutrition_source == NUTRITION_ESTIMATE:
        parts.append("калорийность")
    return f" · на глаз: {' и '.join(parts)}" if parts else ""


def _food_line(food: FoodGuess) -> tuple[str, Any]:
    note = _estimate_note(food)
    if food.grams is None:
        return (
            f"• {food.name} — вес не указан · "
            f"{food.calories_per_100g:.0f} ккал на 100 г{note}",
            None,
        )
    totals = scale_per_100(
        food.calories_per_100g,
        food.grams,
        food.protein_per_100g,
        food.fat_per_100g,
        food.carbs_per_100g,
    )
    line = f"• {food.name} — {number(food.grams)} г · {totals.calories:.0f} ккал"
    if (totals.protein, totals.fat, totals.carbs) != (None, None, None):
        line += f" · {_macros(totals.protein, totals.fat, totals.carbs)}"
    return line + note, totals


def _sum(values: Iterable[Optional[float]]) -> Optional[float]:
    values = list(values)
    return None if any(value is None for value in values) else sum(values)


def _names(names: Iterable[str]) -> str:
    return ", ".join(f"«{short(name, 40)}»" for name in names)


def proposal_text(analysis: PhotoAnalysis, prefix: str = "") -> str:
    lines = [prefix] if prefix else []
    if analysis.comment:
        lines.append(analysis.comment)
    if lines:
        lines.append("")
    foods = [_food_line(food) for food in analysis.foods]
    lines.extend(line for line, _ in foods)
    totals = [total for _, total in foods if total is not None]
    missing = analysis.missing_weight
    if totals:
        total_line = (
            f"Итого{' без продуктов без веса' if missing else ''}: "
            f"{sum(total.calories for total in totals):.0f} ккал"
        )
        macros = tuple(
            _sum(getattr(total, name) for total in totals)
            for name in ("protein", "fat", "carbs")
        )
        if macros != (None, None, None):
            total_line += f" · {_macros(*macros)}"
        lines.append(total_line)
    if analysis.weight_kg is not None:
        lines.append(f"Вес на весах: {analysis.weight_kg:.1f} кг")
    if missing:
        lines.extend(
            [
                "",
                f"Сколько грамм съели: {_names(missing)}? Без веса не записать — "
                "ответьте на это сообщение, например «120 г».",
            ]
        )
    if analysis.question:
        lines.extend(
            [
                "",
                f"Вопрос: {analysis.question}",
                "Можно ответить на это сообщение — или записать как есть.",
            ]
        )
    lines.extend(["", f"Записать в дневник? {CORRECTION_HINT}"])
    return "\n".join(lines)


def missing_weight_alert(names: Iterable[str]) -> str:
    """Why «Записать» did nothing; fits Telegram's callback alert."""
    head = "Не указан вес: "
    tail = (
        ". Ответьте на предложение, сколько грамм съели, или попросите убрать продукт."
    )
    return head + short(_names(names), MAX_ALERT_CHARS - len(head) - len(tail)) + tail


def proposal_keyboard(key: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Записать", callback_data=f"photo:save:{key}"),
                InlineKeyboardButton("Отмена", callback_data=f"photo:cancel:{key}"),
            ]
        ]
    )


def saved_text(
    entries: Iterable[FoodEntry], weight: Optional[WeightRecord], progress: str
) -> str:
    entries = list(entries)
    lines = []
    if entries:
        total = sum(entry.nutrition.calories for entry in entries)
        lines.append(f"Записано в дневник — {total:.0f} ккал:")
        lines.extend(
            f"• {entry.name or 'Еда'} — {number(entry.nutrition.grams or 0)} г · "
            f"{entry.nutrition.calories:.0f} ккал"
            for entry in entries
        )
    if weight is not None:
        lines.append(f"Вес записан: {weight.weight_kg:.1f} кг")
    if progress:
        lines.append(progress)
    return "\n".join(lines)


def saved_keyboard(
    entries: Iterable[FoodEntry], weight: Optional[WeightRecord], issued_at: int
) -> InlineKeyboardMarkup:
    """Open (to edit) or undo each saved item with the existing diary callbacks."""
    rows = [
        [
            InlineKeyboardButton(
                f"{short(entry.name or 'Еда', 24)} · {entry.nutrition.calories:.0f} ккал",
                callback_data=f"entry:view:{entry.entry_id}:0",
            ),
            InlineKeyboardButton(
                "Убрать",
                callback_data=f"entry:undo:{entry.entry_id}:{issued_at}",
            ),
        ]
        for entry in entries
    ]
    if weight is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Вес {weight.weight_kg:.1f} кг",
                    callback_data=f"weight:view:{weight.weight_id}",
                ),
                InlineKeyboardButton(
                    "Убрать", callback_data=f"weight:undo:{weight.weight_id}"
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)
