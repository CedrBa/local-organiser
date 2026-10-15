"""Validate inbox payloads and normalize device timestamps without side effects."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field, ValidationError, field_validator

INBOX_FILE_MISSING_FIELDS_DETAIL = (
    "Die JSON-Datei muss die Felder id, created_at und text enthalten."
)

INBOX_FILE_CREATED_AT_DETAIL = (
    "created_at muss ein ISO-8601-Zeitstempel mit Zeitzone sein, z. B. 2026-09-26T12:32:00+02:00."
)

SHORTCUT_CREATED_AT_FORMATS = (
    "%d.%m.%y, %H:%M",
    "%d.%m.%y, %H:%M:%S",
    "%d.%m.%Y, %H:%M",
    "%d.%m.%Y, %H:%M:%S",
)


class InboxItem(BaseModel):
    id: str = Field(min_length=1)
    created_at: datetime
    text: str

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at muss eine Zeitzone enthalten.")
        return value.astimezone(UTC)


def with_shortcut_created_at(data: object, timezone_name: str) -> object:
    """Ersetzt die lokale Shortcut-Schreibweise von created_at durch einen Zeitstempel mit Zeitzone.

    ISO-8601 und bereits geparste Werte bleiben unveraendert; die Zeitzone der
    lokalen Schreibweise ist die konfigurierte ORGANISER_CALENDAR_TIMEZONE.
    """
    if not isinstance(data, dict) or not isinstance(data.get("created_at"), str):
        return data

    value = data["created_at"].strip().replace("\u00a0", " ").replace("\u202f", " ")
    for timestamp_format in SHORTCUT_CREATED_AT_FORMATS:
        try:
            parsed = datetime.strptime(value, timestamp_format)
        except ValueError:
            continue
        return {**data, "created_at": parsed.replace(tzinfo=ZoneInfo(timezone_name))}
    return data


def inbox_file_validation_detail(data: object, error: ValidationError) -> str:
    """Unterscheidet fehlende Pflichtfelder von einem unbrauchbaren created_at."""
    invalid_fields = {str(part) for entry in error.errors() for part in entry["loc"]}
    if isinstance(data, dict) and not invalid_fields - {"created_at"}:
        return INBOX_FILE_CREATED_AT_DETAIL
    return INBOX_FILE_MISSING_FIELDS_DETAIL
