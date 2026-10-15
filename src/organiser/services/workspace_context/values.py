"""Stable timestamps and hashes for deterministic packet comparison."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Iterable


def _stamp(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to an aware UTC value."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _stamp_key(value: datetime | None) -> str:
    """Comparable string form of a timestamp, used in the manifest and verifier."""
    moment = _stamp(value)
    return moment.isoformat() if moment is not None else ""


def _instant_key(value: datetime | None) -> str:
    """Total, comparable order key for a stored timestamp.

    ``_stamp_key`` renders the manifest value and drops microseconds when they are
    zero; that is fine for equality but *not* for ordering. Sorting needs one fixed
    format, otherwise ``...:00+00:00`` and ``...:00.000001+00:00`` compare by the
    ``+``/``.`` character instead of by the instant.

    The normalisation is not cosmetic. SQLite has no timezone, so a row read back from
    the database carries a naive ``datetime`` while a row that was inserted in *this*
    session keeps the aware value the Python default produced -- and the session
    factory runs with ``expire_on_commit=False``. Two snapshots of one resource with
    the same content hash (different extractor versions) therefore compared aware
    against naive and raised ``TypeError``, i.e. an HTTP 500 instead of a packet.
    """
    moment = _stamp(value)
    return moment.isoformat(timespec="microseconds") if moment is not None else ""


def _digest_json(value: Any) -> str:
    """Stable hash of a JSON-serialisable value, for manifest fingerprints."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _first_int(mapping: dict, names: Iterable[str]) -> int | None:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return None
