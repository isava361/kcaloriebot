"""One-device, append-only Health export with durable per-sample receipts.

SQLite and HealthKit cannot commit together. An unacknowledged sample therefore
requires human reconciliation; it is never automatically issued for writing twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timezone

from .database import Database
from .domain import (
    EARLIEST_DIARY_DATE,
    StateConflict,
    ValidationError,
    day_bounds,
    local_date,
)


class HealthUnauthorized(Exception):
    pass


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def revision(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()[:16]


class HealthStore(Database):
    @staticmethod
    def _day(value: str, today: date) -> date:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise ValidationError("Дата подключения: ГГГГ-ММ-ДД.") from None
        if not EARLIEST_DIARY_DATE <= day <= today:
            raise ValidationError(
                f"Укажите дату от {EARLIEST_DIARY_DATE} до сегодняшнего дня."
            )
        return day

    def connect(
        self, user_id: int, start: str | None = None, end: str | None = None
    ) -> str:
        zone = self.get_timezone(user_id)
        if zone is None:
            raise ValidationError("Сначала настройте часовой пояс через /start.")
        today = local_date(self.now_epoch(), zone)
        if end is not None and start is None:
            raise ValidationError("Укажите начало периода: /health connect ОТ ДО.")
        first = self._day(start, today) if start is not None else today
        last = self._day(end, today) if end is not None else None
        if last is not None and last < first:
            raise ValidationError("Конец периода не может быть раньше его начала.")
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT start_utc, end_utc FROM health_connections WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            # Rotating a key preserves the window; any given date redefines it,
            # so a start without an end reopens the export to new records.
            if previous and start is None:
                start_utc, end_utc = previous["start_utc"], previous["end_utc"]
            else:
                start_utc = day_bounds(first, zone).start_utc
                end_utc = None if last is None else day_bounds(last, zone).end_utc
            conn.execute(
                "INSERT INTO health_connections "
                "(user_id, token_hash, start_utc, end_utc) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET token_hash=excluded.token_hash, "
                "start_utc=excluded.start_utc, end_utc=excluded.end_utc",
                (
                    user_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    start_utc,
                    end_utc,
                ),
            )
        return token

    def disconnect(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE health_connections SET token_hash=NULL WHERE user_id=?",
                (user_id,),
            )

    @staticmethod
    def _authenticate(conn, token: str):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HealthUnauthorized
        row = conn.execute(
            "SELECT user_id, start_utc, end_utc FROM health_connections "
            "WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if row is None:
            raise HealthUnauthorized
        return row

    @staticmethod
    def _samples(conn, user_id: int) -> dict:
        samples = {}

        def add(sample_id, kind, value, epoch, unit):
            if value is not None:
                samples[sample_id] = {
                    "id": sample_id,
                    "type": kind,
                    "value": value,
                    "unit": unit,
                    "date": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                    "epoch": epoch,
                }

        for row in conn.execute(
            "SELECT entry_id, eaten_at_utc, calories, protein, fat, carbs "
            "FROM food_entries WHERE user_id=?",
            (user_id,),
        ):
            for kind in ("calories", "protein", "fat", "carbs"):
                add(
                    f"food:{row['entry_id']}:{kind}",
                    kind,
                    row[kind],
                    row["eaten_at_utc"],
                    "kcal" if kind == "calories" else "g",
                )
        for row in conn.execute("SELECT * FROM weights WHERE user_id=?", (user_id,)):
            add(
                f"weight:{row['weight_id']}",
                "weight",
                row["weight_kg"],
                row["measured_at_utc"],
                "kg",
            )
        return samples

    def _state(
        self, conn, user_id: int, start_utc: int, end_utc: int | None = None
    ) -> dict:
        samples = self._samples(conn, user_id)
        ledger = {
            row["sample_id"]: row
            for row in conn.execute(
                "SELECT * FROM health_samples WHERE user_id=?", (user_id,)
            )
        }
        pending = next(
            (row for row in ledger.values() if row["state"] == "pending"), None
        )
        issues = []
        for sample_id, row in ledger.items():
            current = samples.get(sample_id)
            if row["state"] == "confirmed" and row["payload_json"] != encoded(current):
                issues.append(
                    {
                        "id": sample_id,
                        "previous": json.loads(row["payload_json"]),
                        "current": current,
                        "revision": revision(current),
                    }
                )
        new = sorted(
            (
                sample
                for key, sample in samples.items()
                if key not in ledger
                and start_utc <= sample["epoch"] <= self.now_epoch()
                # end_utc is the local midnight after the last exported day.
                and (end_utc is None or sample["epoch"] < end_utc)
            ),
            key=lambda sample: (sample["epoch"], sample["id"]),
        )
        result = {
            "status": "done",
            "remaining": len(new),
            "issues": issues[:10],
            "issue_count": len(issues),
            "confirmed": sum(row["state"] == "confirmed" for row in ledger.values()),
        }
        if pending:
            result.update(
                status="review",
                sample=json.loads(pending["payload_json"]),
                receipt=pending["receipt"],
            )
        elif issues:
            result["status"] = "changed"
        elif new:
            result.update(status="ready", sample=new[0])
        return result

    def status(self, user_id: int) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN")
            connection = conn.execute(
                "SELECT * FROM health_connections WHERE user_id=?", (user_id,)
            ).fetchone()
            if connection is None:
                return {"connected": False}
            return {
                "connected": connection["token_hash"] is not None,
                "start_utc": connection["start_utc"],
                "end_utc": connection["end_utc"],
                **self._state(
                    conn, user_id, connection["start_utc"], connection["end_utc"]
                ),
            }

    def request(self, token: str, action: str, data: dict) -> dict:
        with self._connect() as conn:
            # Authentication and the mutation share a transaction, including revocation.
            conn.execute("BEGIN IMMEDIATE")
            connection = self._authenticate(conn, token)
            user_id = connection["user_id"]
            if action == "ack":
                if set(data) != {"receipt"} or not isinstance(data["receipt"], str):
                    raise ValidationError("Нужен receipt последней записи.")
                self._ack(conn, user_id, data["receipt"])
                return {"status": "acknowledged"}
            if action not in {"next", "status"} or data:
                raise ValidationError("Неподдерживаемый запрос экспорта.")
            result = self._state(
                conn, user_id, connection["start_utc"], connection["end_utc"]
            )
            if action == "next" and result["status"] == "ready":
                receipt = secrets.token_hex(16)
                sample = result["sample"]
                conn.execute(
                    "INSERT INTO health_samples VALUES (?, ?, ?, ?, 'pending')",
                    (user_id, sample["id"], encoded(sample), receipt),
                )
                result.update(status="sample", receipt=receipt)
            return result

    @staticmethod
    def _ack(conn, user_id: int, receipt: str) -> None:
        row = conn.execute(
            "SELECT state FROM health_samples WHERE user_id=? AND receipt=?",
            (user_id, receipt),
        ).fetchone()
        if row is None:
            raise StateConflict("Подтверждение устарело или принадлежит другой записи.")
        conn.execute(
            "UPDATE health_samples SET state='confirmed' WHERE user_id=? AND receipt=?",
            (user_id, receipt),
        )

    def recover(self, user_id: int, action: str, receipt: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if action == "saved":
                self._ack(conn, user_id, receipt)
            elif action == "retry":
                cursor = conn.execute(
                    "DELETE FROM health_samples WHERE user_id=? AND receipt=? AND state='pending'",
                    (user_id, receipt),
                )
                if not cursor.rowcount:
                    raise StateConflict(
                        "Нет такой незавершённой записи. Отправьте /health."
                    )
            else:
                raise ValidationError("Неизвестное действие восстановления.")

    def corrected(self, user_id: int, sample_id: str, expected: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._samples(conn, user_id).get(sample_id)
            if revision(current) != expected:
                raise StateConflict("Запись снова изменилась. Отправьте /health.")
            cursor = conn.execute(
                "UPDATE health_samples SET payload_json=? "
                "WHERE user_id=? AND sample_id=? AND state='confirmed'",
                (encoded(current), user_id, sample_id),
            )
            if not cursor.rowcount:
                raise StateConflict(
                    "Нет такой подтверждённой записи. Отправьте /health."
                )
