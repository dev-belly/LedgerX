# LedgerX

An append-only execution ledger for **research fills**. It records cash movements
and long-only fills as balanced journal entries, replays them deterministically,
and verifies that a stored JSONL journal has not changed accidentally.

This project sits after an execution simulator such as TradeForge: it consumes
actual fill facts, not target weights or forecasts. The core uses only Python's
standard library and `Decimal`; there are no market data calls or invented P&L.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ledgerx demo --out demo.jsonl
ledgerx verify demo.jsonl
pytest
```

The demo posts a **synthetic** USD 10,000 deposit, buys 10 ACME shares at 100
with a 1 fee, then sells 4 at 110 with a 1 fee. It yields cash 9,438,
remaining cost basis 600.6, and realized P&L 38.6. The JSONL file has one
balanced entry set and chained SHA-256 digest per event; `verify` replays and
checks every row, including sequence and previous hash.

## Python API

```python
from datetime import datetime, timezone
from decimal import Decimal
from ledgerx import CashEvent, FillEvent, Ledger

ledger = Ledger()
ledger.append(CashEvent("fund-1", datetime(2024, 1, 2, tzinfo=timezone.utc),
                        "deposit", Decimal("10000")))
ledger.append(FillEvent("fill-1", datetime(2024, 1, 3, tzinfo=timezone.utc),
                        "ACME", "buy", Decimal("10"), Decimal("100"), Decimal("1")))
ledger.save("fills.jsonl")
print(Ledger.load("fills.jsonl").summary())
```

The caller assigns unique event IDs and timezone-aware timestamps. Posting
order must be nondecreasing by timestamp. A duplicate ID, oversell, unfunded
buy or withdrawal fails before mutating the ledger. USD cost basis includes
buy fees; sale proceeds subtract sell fees. For partial sales, cost basis is
released at average acquisition cost. On a full close, the remaining cost is
released exactly.

Cash, fees and account balances use integer units of `0.00000001` USD. Cash
and fee inputs must be exact multiples of that unit. Fill notionals and partial
cost release round to the nearest unit, ties to even. This keeps the balance
identity exact across long histories of fractional fills.

| Account | Deposit | Buy | Sell |
| --- | ---: | ---: | ---: |
| `cash` | +amount | −(gross + fee) | +(gross − fee) |
| `external_equity` | −amount | 0 | 0 |
| `inventory:SYMBOL` | 0 | +(gross + fee) | −released cost |
| `realized_pnl` | 0 | 0 | −(proceeds − released cost) |

Every event's entries sum to zero. Thus `cash + inventory_cost =
net_contributions + realized_pnl`, with negative account balances representing
credits. A journal load independently recomputes entries and hashes rather
than trusting stored balances.

## Boundaries

- USD, long-only and settled cash; no shorting, margin, FX or corporate actions.
- `book_equity` is cash plus **historical cost**, not market value. There is no
  unrealized P&L without independently supplied, point-in-time marks.
- The hash chain detects accidental or naive edits. Someone able to replace the
  entire file and recompute hashes can forge it; use signed storage or an
  external checkpoint when adversarial tampering matters.
- `save` atomically replaces a snapshot. It is a local research journal, not a
  concurrent transaction service or production accounting system.

## Development

CI runs `ruff check`, `ruff format --check`, `mypy` and `pytest` on Python
3.11–3.13. Tests cover fee accounting, partial and full closes, rejection
atomicity, deterministic replay, tampering and the CLI.
