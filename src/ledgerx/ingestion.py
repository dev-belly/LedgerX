"""Strict event-feed intake with atomic batch posting and snapshot publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ledgerx.core import (
    GENESIS_HASH,
    Event,
    IntegrityError,
    Ledger,
    LedgerError,
    _event_from_dict,
    _unique_object,
)


def load_events(path: str | Path) -> tuple[list[Event], str]:
    """Read one validated raw event per line, preserving the supplied order."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LedgerError("Event feed must be UTF-8") from error
    lines = text.splitlines()
    if not lines:
        raise LedgerError("Event feed is empty")
    events = []
    for line_number, line in enumerate(lines, 1):
        try:
            row = json.loads(line, object_pairs_hook=_unique_object)
            if not isinstance(row, dict) or any(
                not isinstance(value, str) for value in row.values()
            ):
                raise LedgerError("Event feed fields must all be strings")
            events.append(_event_from_dict(row))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise LedgerError(f"Invalid event feed row {line_number}: {error}") from error
    return events, hashlib.sha256(raw).hexdigest()


def ingest_journal(
    journal_path: str | Path,
    event_path: str | Path,
    *,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Verify, stage and publish a full batch; failed intake never publishes a prefix.

    Only this call's private ledger is staged. A fully repeated batch does not
    rewrite the file. Callers must ensure there is only one snapshot writer.
    """
    destination = Path(journal_path)
    if destination.exists():
        ledger = Ledger.load(destination, expected_head=expected_head)
    else:
        if expected_head is not None and expected_head != GENESIS_HASH:
            raise IntegrityError("A new journal requires the genesis head checkpoint")
        ledger = Ledger()
    prior_head = ledger.head_hash
    prior_count = len(ledger.records)
    events, digest = load_events(event_path)
    ledger.append_batch(events)
    appended = len(ledger.records) - prior_count
    report = {
        "input_rows": len(events),
        "appended_events": appended,
        "reused_events": len(events) - appended,
        "input_sha256": digest,
        "previous_head": prior_head,
        "ledger": ledger.summary(),
    }
    if appended:
        ledger.save(destination)
    return report
