"""Parsing and validation of inline-keyboard callback data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


PAGE_SIZE = 5
STATS_PAGE_SIZE = 7
MAX_PAGE_OFFSET = 10_000
CONFIRMATION_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class CallbackAction:
    kind: str
    record_id: Optional[int] = None
    offset: int = 0
    nutrient: Optional[str] = None
    issued_at: Optional[int] = None
    period: Optional[str] = None


def parse_callback(data: str) -> Optional[CallbackAction]:
    if data in {"cancel", "cancel_all"}:
        return CallbackAction("cancel")
    if data == "dismiss":
        return CallbackAction("dismiss")

    parts = data.split(":")
    try:
        if len(parts) == 3 and parts[:2] == ["entry", "list"]:
            return CallbackAction("entry_list", offset=_parse_offset(parts[2]))
        if len(parts) == 3 and parts[:2] == ["fav", "list"]:
            return CallbackAction("favorite_list", offset=_parse_offset(parts[2]))
        if len(parts) == 3 and parts[:2] == ["recent", "use"]:
            return CallbackAction("recent_use", record_id=_parse_id(parts[2]))
        if len(parts) == 3 and parts[0] == "stats" and parts[1] in {"week", "month"}:
            return CallbackAction(
                "stats_page",
                period=parts[1],
                offset=_parse_offset(parts[2], STATS_PAGE_SIZE),
            )
        if len(parts) == 3 and parts[0] == "entry":
            kinds = {
                "view": "entry_view",
                "delete": "entry_delete",
                "delete-confirm": "entry_delete_confirm",
                "grams": "entry_grams",
                "time": "entry_time",
            }
            if parts[1] in kinds:
                return CallbackAction(kinds[parts[1]], record_id=_parse_id(parts[2]))
        if len(parts) == 4 and parts[:2] == ["entry", "view"]:
            return CallbackAction(
                "entry_view",
                record_id=_parse_id(parts[2]),
                offset=_parse_offset(parts[3]),
            )
        if len(parts) == 4 and parts[:2] == ["entry", "delete-confirm"]:
            return CallbackAction(
                "entry_delete_confirm",
                record_id=_parse_id(parts[2]),
                issued_at=_parse_timestamp(parts[3]),
            )
        if len(parts) == 3 and parts[0] == "fav":
            kinds = {
                "view": "favorite_view",
                "use": "favorite_use",
                "edit": "favorite_edit",
                "delete": "favorite_delete",
                "delete-confirm": "favorite_delete_confirm",
            }
            if parts[1] in kinds:
                return CallbackAction(kinds[parts[1]], record_id=_parse_id(parts[2]))
        if len(parts) == 4 and parts[:2] == ["fav", "view"]:
            return CallbackAction(
                "favorite_view",
                record_id=_parse_id(parts[2]),
                offset=_parse_offset(parts[3]),
            )
        if len(parts) == 4 and parts[:2] == ["fav", "delete-confirm"]:
            return CallbackAction(
                "favorite_delete_confirm",
                record_id=_parse_id(parts[2]),
                issued_at=_parse_timestamp(parts[3]),
            )
        if len(parts) == 4 and parts[:2] == ["fav", "field"]:
            if parts[3] not in {"calories", "protein", "fat", "carbs"}:
                return None
            return CallbackAction(
                "favorite_field", record_id=_parse_id(parts[2]), nutrient=parts[3]
            )

        # Compatibility with inline keyboards sent by the retired Go version.
        # Telegram keeps old messages tappable indefinitely, but a year without
        # the Go bot is long enough: safe to remove these two blocks and their
        # tests after 2027-07.
        legacy_offset_prefixes = {
            "previous:": "entry_list",
            "next:": "entry_list",
            "previous_fav:": "favorite_list",
            "next_fav:": "favorite_list",
        }
        for prefix, kind in legacy_offset_prefixes.items():
            if data.startswith(prefix):
                return CallbackAction(kind, offset=_parse_offset(data[len(prefix) :]))

        legacy_id_prefixes = (
            ("entry_confirm_delete_", "entry_delete_confirm", None),
            ("entry_cancel_delete_", "dismiss", None),
            ("entry_delete_", "entry_delete", None),
            ("entry_choose_", "entry_view", None),
            ("fave_confirm_delete_", "favorite_delete_confirm", None),
            ("fave_cancel_delete_", "dismiss", None),
            ("favedelete_", "favorite_delete", None),
            ("fave_amend_", "favorite_edit", None),
            ("choose_favorite_", "favorite_view", None),
            ("calories_amend_", "favorite_field", "calories"),
            ("protein_amend_", "favorite_field", "protein"),
            ("fat_amend_", "favorite_field", "fat"),
            ("carbs_amend_", "favorite_field", "carbs"),
            ("favorite_", "favorite_use", None),
        )
        for prefix, kind, nutrient in legacy_id_prefixes:
            if data.startswith(prefix):
                return CallbackAction(
                    kind, record_id=_parse_id(data[len(prefix) :]), nutrient=nutrient
                )
    except ValueError:
        return None
    return None


def confirmation_expired(action: CallbackAction, now_utc: int) -> bool:
    if action.issued_at is None:
        return True
    age = now_utc - action.issued_at
    return not 0 <= age <= CONFIRMATION_TTL_SECONDS


def _parse_id(value: str) -> int:
    record_id = int(value)
    if record_id <= 0:
        raise ValueError("record ID must be positive")
    return record_id


def _parse_offset(value: str, page_size: int = PAGE_SIZE) -> int:
    offset = int(value)
    if offset < 0 or offset > MAX_PAGE_OFFSET or offset % page_size != 0:
        raise ValueError("invalid page offset")
    return offset


def _parse_timestamp(value: str) -> int:
    timestamp = int(value)
    if timestamp <= 0:
        raise ValueError("invalid timestamp")
    return timestamp
