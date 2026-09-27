"""Small reproducible demo and journal verification CLI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ledgerx.core import CashEvent, FillEvent, Ledger, LedgerError


def demo() -> Ledger:
    ledger = Ledger()
    utc = UTC
    ledger.append(
        CashEvent("fund-001", datetime(2024, 1, 2, tzinfo=utc), "deposit", Decimal("10000"))
    )
    ledger.append(
        FillEvent(
            "fill-001",
            datetime(2024, 1, 3, tzinfo=utc),
            "ACME",
            "buy",
            Decimal("10"),
            Decimal("100"),
            Decimal("1"),
        )
    )
    ledger.append(
        FillEvent(
            "fill-002",
            datetime(2024, 1, 4, tzinfo=utc),
            "ACME",
            "sell",
            Decimal("4"),
            Decimal("110"),
            Decimal("1"),
        )
    )
    return ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledgerx", description="Replayable trade-fill ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    make_demo = commands.add_parser("demo", help="Run a three-event synthetic example")
    make_demo.add_argument("--out", type=Path, help="Write an auditable JSONL journal")
    verify = commands.add_parser("verify", help="Verify and replay a JSONL journal")
    verify.add_argument("journal", type=Path)
    verify.add_argument(
        "--expected-head", help="Check against a previously saved SHA-256 head checkpoint"
    )
    args = parser.parse_args(argv)
    try:
        ledger = (
            demo()
            if args.command == "demo"
            else Ledger.load(args.journal, expected_head=args.expected_head)
        )
        if args.command == "demo" and args.out is not None:
            ledger.save(args.out)
        print(json.dumps(ledger.summary(), indent=2))
    except (LedgerError, OSError) as exc:
        print(f"ledgerx: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
