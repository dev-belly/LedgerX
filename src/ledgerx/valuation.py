"""Point-in-time USD marks kept separate from the immutable fill journal.

Quotes are external observations, never inferred from execution prices. A mark
must be observed and available by the cutoff; stale or missing marks fail.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from ledgerx.core import (
    _SYMBOL,
    DECIMAL_PRECISION,
    MAX_FILL_DECIMAL_PLACES,
    Ledger,
    LedgerError,
    _decimal,
    _money_text,
    _units,
)

QUOTE_COLUMNS = ("symbol", "observed_at", "available_at", "price")


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: str, label: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), label)
    except ValueError as exc:
        raise LedgerError(f"{label} must be an ISO 8601 timestamp with UTC offset") from exc


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    observed_at: datetime
    available_at: datetime
    price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise LedgerError("Quote symbol must be an uppercase ASCII ticker")
        observed = _utc(self.observed_at, "observed_at")
        available = _utc(self.available_at, "available_at")
        if available < observed:
            raise LedgerError("A quote cannot be available before observation")
        price = _decimal(self.price, "quote price")
        exponent = price.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -MAX_FILL_DECIMAL_PLACES:
            raise LedgerError("Quote price supports at most 24 decimal places")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "price", price)


def load_quotes(path: str | Path) -> tuple[list[PriceQuote], str]:
    """Read one strict CSV byte snapshot and return its SHA-256 provenance."""
    raw = Path(path).read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
        if reader.fieldnames != list(QUOTE_COLUMNS):
            raise LedgerError("Quote CSV needs symbol,observed_at,available_at,price headers")
        quotes: list[PriceQuote] = []
        for line_no, row in enumerate(reader, 2):
            if set(row) != set(QUOTE_COLUMNS) or any(value is None for value in row.values()):
                raise LedgerError(f"Malformed quote CSV row {line_no}")
            try:
                quotes.append(
                    PriceQuote(
                        row["symbol"],
                        _timestamp(row["observed_at"], "observed_at"),
                        _timestamp(row["available_at"], "available_at"),
                        Decimal(row["price"]),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise LedgerError(f"Invalid quote CSV row {line_no}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise LedgerError("Quote CSV must be UTF-8") from exc
    return quotes, hashlib.sha256(raw).hexdigest()


def mark_to_market(
    ledger: Ledger,
    quotes: list[PriceQuote],
    as_of: datetime,
    *,
    max_age_seconds: int = 86_400,
) -> dict[str, Any]:
    """Value open positions with available marks, without posting a journal event.

    The result is a mid-price research estimate; no spread, liquidation fee,
    currency conversion or tradeability adjustment is implied.
    """
    cutoff = _utc(as_of, "as_of")
    if type(max_age_seconds) is not int or not 0 < max_age_seconds <= 365 * 86_400:
        raise LedgerError("max_age_seconds must be an integer from 1 to 31536000")
    if ledger.records and ledger.records[-1].event.occurred_at > cutoff:
        raise LedgerError("Journal contains an event after valuation cutoff")
    book = ledger.summary()  # Check the historical-cost accounting invariant first.
    cash = _units(ledger.balance("cash"), "cash", exact=True)
    contributed = -_units(ledger.balance("external_equity"), "contributions", exact=True)
    realized = -_units(ledger.balance("realized_pnl"), "realized P&L", exact=True)
    keys: set[tuple[str, datetime, datetime]] = set()
    usable: dict[str, PriceQuote] = {}
    for quote in quotes:
        if not isinstance(quote, PriceQuote):
            raise LedgerError("Expected PriceQuote records")
        key = (quote.symbol, quote.observed_at, quote.available_at)
        if key in keys:
            raise LedgerError(f"Duplicate quote revision: {quote.symbol}")
        keys.add(key)
        if quote.observed_at > cutoff or quote.available_at > cutoff:
            continue
        previous = usable.get(quote.symbol)
        if previous is None or (quote.observed_at, quote.available_at) > (
            previous.observed_at,
            previous.available_at,
        ):
            usable[quote.symbol] = quote

    market = 0
    rows: dict[str, dict[str, str]] = {}
    for symbol, position in sorted(ledger.positions.items()):
        selected = usable.get(symbol)
        if selected is None:
            raise LedgerError(f"No available quote for open position: {symbol}")
        if cutoff - selected.observed_at > timedelta(seconds=max_age_seconds):
            raise LedgerError(f"Stale quote for open position: {symbol}")
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            value = position.quantity * selected.price
        value_units = _units(value, "market value")
        cost_units = _units(position.cost_basis, "cost basis", exact=True)
        market += value_units
        rows[symbol] = {
            "quantity": str(position.quantity),
            "cost_basis": _money_text(cost_units),
            "market_value": _money_text(value_units),
            "unrealized_pnl": _money_text(value_units - cost_units),
            "price": str(selected.price),
            "observed_at": selected.observed_at.isoformat().replace("+00:00", "Z"),
            "available_at": selected.available_at.isoformat().replace("+00:00", "Z"),
        }
    inventory = _units(Decimal(book["inventory_cost"]), "inventory cost", exact=True)
    unrealized = market - inventory
    if cash + market != contributed + realized + unrealized:
        raise LedgerError("Marked equity does not reconcile to contributions and P&L")
    return {
        "as_of": cutoff.isoformat().replace("+00:00", "Z"),
        "max_age_seconds": max_age_seconds,
        "basis": "available observed quote, mid-price estimate without liquidation costs",
        "journal_head": ledger.head_hash,
        "events": len(ledger.records),
        "cash": _money_text(cash),
        "inventory_cost": _money_text(inventory),
        "market_value": _money_text(market),
        "net_contributions": _money_text(contributed),
        "realized_pnl": _money_text(realized),
        "unrealized_pnl": _money_text(unrealized),
        "marked_equity": _money_text(cash + market),
        "positions": rows,
    }
