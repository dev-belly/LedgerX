# LedgerX

An append-only execution ledger for **research fills**. It records cash movements
and long-only fills as balanced journal entries, replays them deterministically,
and verifies that a stored JSONL journal has not changed accidentally.

This project sits after an execution simulator such as TradeForge: it consumes
actual fill facts, not target weights or forecasts. The core uses only Python's
standard library and `Decimal`; there are no market data calls. An optional,
separate price snapshot can estimate unrealized P&L without changing the ledger.

Accounting and valuation use an isolated 128-digit, half-even Decimal context.
An importing application's precision, rounding, exponent limits and inexact
traps cannot change journal replay. Cash transfers and fees must equal their
original amount at the `1e-8` USD quantum; digits beyond working precision are
checked against that original amount rather than silently rounded away.

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

For protection against truncation or replacement of the entire journal, save
the printed `head_hash` somewhere independent of the JSONL file and check it:

```bash
ledgerx verify demo.jsonl --expected-head <previously-recorded-head-hash>
```

The demo output is synthetic; the checkpoint must come from a prior trusted
run, not from the same file being verified.

## Point-in-time portfolio marks

The accounting journal records **historical cost and realized P&L**. To value
open positions, pass independent USD quotes with both an observation timestamp
and the time they became available:

```bash
ledgerx demo --out demo.jsonl
ledgerx mark demo.jsonl --quotes examples/demo_quotes.csv --as-of 2024-01-04T17:00:00Z
```

The [published quote input](examples/demo_quotes.csv) deliberately includes a
price observed before 17:00 but published at 18:00, and another observed at
18:00. Neither is eligible at the cutoff. The [reproducible output](examples/valuation.json)
uses the available **105 USD** quote: six ACME shares have a market value of
**630 USD** and historical cost **600.60 USD**. Unrealized P&L is **29.40 USD**;
cash **9,438 USD** plus the marked holding is **10,068 USD**, reconciling to
10,000 USD net contributions + 38.60 USD realized P&L + 29.40 USD unrealized
P&L. No quote is inferred from a trade fill.

Quotes must be finite, positive decimal prices for uppercase symbols, in the
exact CSV schema `symbol,observed_at,available_at,price`. Timestamps need UTC
offsets. The latest observed quote available by `--as-of` is used; revisions
of one observation use the latest available version. Missing or older than
`--max-age-seconds` (default 86,400) fails rather than silently using cost or a
future price. A full journal with an event after the cutoff is rejected; use a
verified journal prefix for an earlier valuation. The output includes selected
quote timestamps, the journal head and SHA-256 of the quote CSV bytes. Use
`--expected-head` with a separately saved checkpoint to detect journal
truncation. The quote hash proves which file was read, not whether its prices
are genuine.

This is a **mid-price research estimate**, without a sale spread, fees,
liquidity constraints, FX or a claim about obtainable liquidation proceeds.
The price file is separate from immutable journal events; generating a mark
never posts an accounting transaction. The design follows the distinction
between cost and external market prices in [hledger](https://hledger.org/investments.html)
and [Beancount](https://beancount.github.io/docs/running_beancount_and_generating_reports/);
[LEAN](https://www.quantconnect.com/docs/v2/writing-algorithms/portfolio/holdings)
also distinguishes holdings cost from value at a price.

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
buy, withdrawal or sell fee fails before mutating the ledger. USD cost basis includes
buy fees; sale proceeds subtract sell fees. For partial sales, cost basis is
released at average acquisition cost. On a full close, the remaining cost is
released exactly.

Cash, fees and account balances use integer units of `0.00000001` USD. Cash
and fee inputs must be exact multiples of that unit. Fill notionals and partial
cost release round to the nearest unit, ties to even. This keeps the balance
identity exact across long histories of fractional fills. Quantity and price
accept at most 24 decimal places; position quantity arithmetic retains those
places even for large existing positions.

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
  unrealized P&L without independently supplied, point-in-time marks. The
  optional `mark` report does not revalue journal accounts.
- The hash chain detects edits to existing rows. A truncated prefix remains a
  valid chain unless an independent head checkpoint is supplied. Someone able
  to replace the entire file and checkpoint can forge both; use signed storage
  when adversarial tampering matters.
- `save` atomically replaces a snapshot. It is a local research journal, not a
  concurrent transaction service or production accounting system.

## Development

CI runs `ruff check`, `ruff format --check`, `mypy` and `pytest` on Python
3.11–3.13. Tests cover fee accounting, partial and full closes, rejection
atomicity, deterministic replay, tampering and the CLI.
