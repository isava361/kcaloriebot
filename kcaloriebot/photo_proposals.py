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
from .photo_ai import FoodGuess, PhotoAnalysis
from .render import number, short

# Long enough to confirm a meal photo later in the day; the cap bounds the
# image bytes held for corrections.
PROPOSAL_TTL_SECONDS = 24 * 60 * 60
MAX_PROPOSALS = 20

OPEN = "open"
SAVED = "saved"
CANCELLED = "cancelled"

CORRECTION_HINT = (
    "Чтобы исправить, ответьте на это сообщение, например: «гречки было 150 г»."
)


@dataclass
class PhotoProposal:
    # The owner's photo message ID: unique in the chat and never reused, so a
    # button left over from before a restart cannot hit a newer proposal.
    key: int
    user_id: int
    chat_id: int
    image: Optional[bytes]
    media_type: str
    caption: Optional[str]
    eaten_at_utc: int
    analysis: PhotoAnalysis
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
        self.image = None


class PhotoProposalStore:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = PROPOSAL_TTL_SECONDS,
        limit: int = MAX_PROPOSALS,
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._limit = limit
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
                if (message_id == proposal.key or message_id in proposal.message_ids)
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
        while len(self._items) > self._limit:
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


def _food_line(food: FoodGuess) -> tuple[str, Any]:
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
    return line, totals


def _sum(values: Iterable[Optional[float]]) -> Optional[float]:
    values = list(values)
    return None if any(value is None for value in values) else sum(values)


def proposal_text(analysis: PhotoAnalysis, prefix: str = "") -> str:
    lines = [prefix] if prefix else []
    if analysis.comment:
        lines.append(analysis.comment)
    if lines:
        lines.append("")
    foods = [_food_line(food) for food in analysis.foods]
    lines.extend(line for line, _ in foods)
    if foods:
        totals = [total for _, total in foods]
        total_line = f"Итого: {sum(total.calories for total in totals):.0f} ккал"
        macros = tuple(
            _sum(getattr(total, name) for total in totals)
            for name in ("protein", "fat", "carbs")
        )
        if macros != (None, None, None):
            total_line += f" · {_macros(*macros)}"
        lines.append(total_line)
    if analysis.weight_kg is not None:
        lines.append(f"Вес на весах: {analysis.weight_kg:.1f} кг")
    lines.extend(["", f"Записать в дневник? {CORRECTION_HINT}"])
    return "\n".join(lines)


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
