"""Record-local entity lookup for admission and later replay/visibility owners.

Only signals, events, candidates and threads are entities; decision append
receipts are not. Identifiers are literal, never coerced or trimmed. A duplicate
is ambiguous even when both records are byte-identical. Projection tombstones
are accepted only when the caller has established an internal snapshot boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RecordLookup:
    state: Literal["unique", "missing", "ambiguous"]
    row: dict | None = None


def record_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def record_index(rows: list[dict], *, projected: bool = False) -> dict[str, RecordLookup]:
    result: dict[str, RecordLookup] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = record_id(row.get("id"))
        if identifier is None:
            continue
        if identifier in result or (projected and row.get("_admission_ambiguous") is True):
            result[identifier] = RecordLookup("ambiguous")
        else:
            result[identifier] = RecordLookup("unique", row)
    return result


def lookup_record(index: dict[str, RecordLookup], identifier: object) -> RecordLookup:
    key = record_id(identifier)
    return index.get(key, RecordLookup("missing")) if key is not None else RecordLookup("missing")
