"""Append-only, double-entry ledger with deterministic replay.

Amounts are Decimal values; floats are refused at the event boundary. A sale
releases average acquisition cost, including buy fees. Nothing here estimates
market value or uses prices that were not present in the fill stream.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Literal, TypeAlias

GENESIS_HASH = "0" * 64
MONEY_QUANTUM = Decimal("0.00000001")  # one hundred-millionth of USD
MAX_MONEY_UNITS = 10**24
MAX_FILL_DECIMAL_PLACES = 24
DECIMAL_PRECISION = 128
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z")


class LedgerError(ValueError):
    """A fill or transfer violates the ledger contract."""


class IntegrityError(LedgerError):
    """A stored journal cannot be replayed exactly."""


def _decimal(value: Decimal | str | int, label: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, (bool, float)):
        raise LedgerError(f"{label} must be a decimal string or Decimal, not float/bool")
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise LedgerError(f"{label} must be a finite decimal") from exc
    if not amount.is_finite() or (amount <= 0 if positive else amount < 0):
        sign = "positive" if positive else "non-negative"
        raise LedgerError(f"{label} must be finite and {sign}")
    if amount > Decimal("1e16"):
        raise LedgerError(f"{label} exceeds the supported range")
    return amount


def _units(value: Decimal, label: str, *, exact: bool = False) -> int:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        scaled = value / MONEY_QUANTUM
        rounded = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
    if exact and scaled != rounded:
        raise LedgerError(f"{label} must be a multiple of {MONEY_QUANTUM}")
    units = int(rounded)
    if abs(units) > MAX_MONEY_UNITS:
        raise LedgerError(f"{label} exceeds the supported range")
    return units


def _money(units: int) -> Decimal:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return Decimal(units).scaleb(-8)


def _money_text(units: int) -> str:
    return format(_money(units), "f").rstrip("0").rstrip(".") or "0"


def _identity(event_id: str, occurred_at: datetime) -> datetime:
    if not isinstance(event_id, str) or not _ID.fullmatch(event_id):
        raise LedgerError(
            "event_id must be 1–128 ASCII letters, digits, dot, underscore, colon or dash"
        )
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise LedgerError("occurred_at must be a timezone-aware datetime")
    if occurred_at.utcoffset() is None:
        raise LedgerError("occurred_at must have a valid UTC offset")
    return occurred_at.astimezone(UTC)


@dataclass(frozen=True)
class CashEvent:
    event_id: str
    occurred_at: datetime
    direction: Literal["deposit", "withdrawal"]
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _identity(self.event_id, self.occurred_at))
        if self.direction not in {"deposit", "withdrawal"}:
            raise LedgerError("Cash direction must be deposit or withdrawal")
        amount = _decimal(self.amount, "amount")
        _units(amount, "amount", exact=True)
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True)
class FillEvent:
    event_id: str
    occurred_at: datetime
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _identity(self.event_id, self.occurred_at))
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise LedgerError("symbol must be an uppercase ASCII ticker")
        if self.side not in {"buy", "sell"}:
            raise LedgerError("Fill side must be buy or sell")
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))
        object.__setattr__(self, "price", _decimal(self.price, "price"))
        for label in ("quantity", "price"):
            if getattr(self, label).as_tuple().exponent < -MAX_FILL_DECIMAL_PLACES:
                raise LedgerError(
                    f"{label} supports at most {MAX_FILL_DECIMAL_PLACES} decimal places"
                )
        fee = _decimal(self.fee, "fee", positive=False)
        _units(fee, "fee", exact=True)
        object.__setattr__(self, "fee", fee)


Event: TypeAlias = CashEvent | FillEvent


@dataclass(frozen=True)
class Position:
    quantity: Decimal
    cost_basis: Decimal


@dataclass(frozen=True)
class _PositionUnits:
    quantity: Decimal
    cost_units: int


@dataclass(frozen=True)
class Entry:
    account: str
    units: int  # debit positive, credit negative

    @property
    def amount(self) -> Decimal:
        return _money(self.units)

    def to_dict(self) -> dict[str, str]:
        return {"account": self.account, "amount": _money_text(self.units)}


def _event_dict(event: Event) -> dict[str, str]:
    base = {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
    }
    if isinstance(event, CashEvent):
        return {**base, "kind": "cash", "direction": event.direction, "amount": str(event.amount)}
    return {
        **base,
        "kind": "fill",
        "symbol": event.symbol,
        "side": event.side,
        "quantity": str(event.quantity),
        "price": str(event.price),
        "fee": str(event.fee),
    }


def _event_from_dict(data: Any) -> Event:
    if not isinstance(data, dict):
        raise IntegrityError("Event must be an object")
    kind = data.get("kind")
    common = {"kind", "event_id", "occurred_at"}
    expected = common | (
        {"direction", "amount"}
        if kind == "cash"
        else {"symbol", "side", "quantity", "price", "fee"}
    )
    if kind not in {"cash", "fill"} or set(data) != expected:
        raise IntegrityError("Unknown event kind or unexpected event fields")
    when = datetime.fromisoformat(data["occurred_at"].replace("Z", "+00:00"))
    if kind == "cash":
        return CashEvent(data["event_id"], when, data["direction"], data["amount"])
    return FillEvent(
        data["event_id"],
        when,
        data["symbol"],
        data["side"],
        data["quantity"],
        data["price"],
        data["fee"],
    )


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class JournalRecord:
    sequence: int
    event: Event
    entries: tuple[Entry, ...]
    previous_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event": _event_dict(self.event),
            "entries": [entry.to_dict() for entry in self.entries],
            "previous_hash": self.previous_hash,
            "hash": self.hash,
        }


class Ledger:
    """Long-only USD cost ledger; events are posted in nondecreasing UTC order."""

    def __init__(self) -> None:
        self._balances: dict[str, int] = {}
        self._positions: dict[str, _PositionUnits] = {}
        self._records: list[JournalRecord] = []
        self._seen_ids: set[str] = set()

    @property
    def records(self) -> tuple[JournalRecord, ...]:
        return tuple(self._records)

    @property
    def positions(self) -> dict[str, Position]:
        return {
            symbol: Position(position.quantity, _money(position.cost_units))
            for symbol, position in self._positions.items()
        }

    @property
    def head_hash(self) -> str:
        return self._records[-1].hash if self._records else GENESIS_HASH

    def balance(self, account: str) -> Decimal:
        return _money(self._balances.get(account, 0))

    def append(self, event: Event) -> JournalRecord:
        """Post an event atomically or leave every balance and record unchanged."""
        if not isinstance(event, (CashEvent, FillEvent)):
            raise LedgerError("Unsupported event type")
        if event.event_id in self._seen_ids:
            raise LedgerError(f"Duplicate event_id: {event.event_id}")
        if self._records and event.occurred_at < self._records[-1].event.occurred_at:
            raise LedgerError("Events must be appended in nondecreasing occurred_at order")

        positions = dict(self._positions)
        entries = self._entries(event, positions)
        if sum(entry.units for entry in entries) != 0:
            raise LedgerError("Unbalanced journal entries")
        balances = dict(self._balances)
        for entry in entries:
            balances[entry.account] = balances.get(entry.account, 0) + entry.units
            if abs(balances[entry.account]) > MAX_MONEY_UNITS:
                raise LedgerError("Account balance exceeds the supported range")

        body: dict[str, Any] = {
            "sequence": len(self._records) + 1,
            "event": _event_dict(event),
            "entries": [entry.to_dict() for entry in entries],
            "previous_hash": self.head_hash,
        }
        digest = hashlib.sha256(_canonical(body)).hexdigest()
        record = JournalRecord(body["sequence"], event, entries, self.head_hash, digest)
        self._balances = balances
        self._positions = positions
        self._seen_ids.add(event.event_id)
        self._records.append(record)
        return record

    def _entries(self, event: Event, positions: dict[str, _PositionUnits]) -> tuple[Entry, ...]:
        if isinstance(event, CashEvent):
            amount = _units(event.amount, "amount", exact=True)
            if event.direction == "withdrawal" and self._balances.get("cash", 0) < amount:
                raise LedgerError("Insufficient cash for withdrawal")
            signed = amount if event.direction == "deposit" else -amount
            return (Entry("cash", signed), Entry("external_equity", -signed))

        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            gross = event.quantity * event.price
        gross_units = _units(gross, "gross fill amount")
        if gross_units == 0:
            raise LedgerError("Gross fill amount is below the accounting quantum")
        fee_units = _units(event.fee, "fee", exact=True)
        account = f"inventory:{event.symbol}"
        if event.side == "buy":
            paid = gross_units + fee_units
            if self._balances.get("cash", 0) < paid:
                raise LedgerError("Insufficient cash for buy")
            old = positions.get(event.symbol, _PositionUnits(Decimal("0"), 0))
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                quantity = old.quantity + event.quantity
            positions[event.symbol] = _PositionUnits(quantity, old.cost_units + paid)
            return (Entry("cash", -paid), Entry(account, paid))

        old = positions.get(event.symbol, _PositionUnits(Decimal("0"), 0))
        if old.quantity < event.quantity:
            raise LedgerError("Insufficient position for sell")
        if old.quantity == event.quantity:
            released = old.cost_units
        else:
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                fraction = Decimal(old.cost_units) * event.quantity / old.quantity
                released = int(fraction.to_integral_value(rounding=ROUND_HALF_EVEN))
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            remaining = old.quantity - event.quantity
        if remaining:
            positions[event.symbol] = _PositionUnits(remaining, old.cost_units - released)
        else:
            positions.pop(event.symbol, None)
        proceeds = gross_units - fee_units
        if self._balances.get("cash", 0) + proceeds < 0:
            raise LedgerError("Insufficient cash to cover sell fee")
        realized = proceeds - released
        return (
            Entry("cash", proceeds),
            Entry(account, -released),
            Entry("realized_pnl", -realized),
        )

    def summary(self) -> dict[str, Any]:
        cash = self._balances.get("cash", 0)
        inventory = sum(pos.cost_units for pos in self._positions.values())
        contributed = -self._balances.get("external_equity", 0)
        realized = -self._balances.get("realized_pnl", 0)
        if cash + inventory != contributed + realized:
            raise LedgerError("Balance-sheet invariant failed")
        return {
            "events": len(self._records),
            "head_hash": self.head_hash,
            "cash": _money_text(cash),
            "inventory_cost": _money_text(inventory),
            "net_contributions": _money_text(contributed),
            "realized_pnl": _money_text(realized),
            "book_equity": _money_text(cash + inventory),
            "positions": {
                symbol: {
                    "quantity": str(pos.quantity),
                    "cost_basis": _money_text(pos.cost_units),
                }
                for symbol, pos in sorted(self._positions.items())
            },
        }

    def save(self, path: str | Path) -> None:
        """Atomically replace a JSONL snapshot; load always verifies every row."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                delete=False,
            ) as handle:
                temporary = handle.name
                for record in self._records:
                    handle.write(_canonical(record.to_dict()).decode() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load(cls, path: str | Path, *, expected_head: str | None = None) -> Ledger:
        """Replay a snapshot, optionally checking a separately retained head digest."""
        if expected_head is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_head):
            raise IntegrityError("Expected head hash must be 64 lowercase hex characters")
        ledger = cls()
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise IntegrityError("Journal must be UTF-8") from exc
        for line_no, line in enumerate(lines, 1):
            try:
                row = json.loads(line, object_pairs_hook=_unique_object)
                event = _event_from_dict(row["event"])
                record = ledger.append(event)
                if _canonical(record.to_dict()) != _canonical(row):
                    raise IntegrityError("Journal row differs from deterministic replay")
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise IntegrityError(f"Invalid journal row {line_no}: {exc}") from exc
        if expected_head is not None and ledger.head_hash != expected_head:
            raise IntegrityError("Journal head differs from expected checkpoint")
        return ledger
