from datetime import datetime
from decimal import Decimal

import pytest

from ledgerx import CashEvent, FillEvent, LedgerError, PriceQuote
from ledgerx.cli import main

INVALID_TIMES = ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"]


@pytest.mark.parametrize("text", INVALID_TIMES)
@pytest.mark.parametrize("kind", ["cash", "fill", "quote"])
def test_unrepresentable_utc_times_are_ledger_errors(text, kind) -> None:
    value = datetime.fromisoformat(text)
    with pytest.raises(LedgerError, match="UTC datetime range"):
        if kind == "cash":
            CashEvent("fund", value, "deposit", Decimal("10"))
        elif kind == "fill":
            FillEvent("fill", value, "ACME", "buy", Decimal("1"), Decimal("1"), Decimal("0"))
        else:
            PriceQuote("ACME", value, value, Decimal("1"))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0001-01-01T14:00:00+14:00", "0001-01-01T00:00:00+00:00"),
        ("9999-12-31T11:59:59-12:00", "9999-12-31T23:59:59+00:00"),
    ],
)
def test_representable_utc_boundaries_remain_valid(text, expected) -> None:
    value = datetime.fromisoformat(text)
    event = CashEvent("fund", value, "deposit", Decimal("10"))
    quote = PriceQuote("ACME", value, value, Decimal("1"))
    assert event.occurred_at.isoformat() == expected
    assert quote.observed_at.isoformat() == expected


@pytest.mark.parametrize("text", INVALID_TIMES)
def test_cli_mark_reports_invalid_cutoff_without_output_or_journal_changes(
    tmp_path, capsys, text
) -> None:
    journal = tmp_path / "journal.jsonl"
    assert main(["demo", "--out", str(journal)]) == 0
    capsys.readouterr()
    before = journal.read_bytes()
    assert (
        main(
            [
                "mark",
                str(journal),
                "--quotes",
                "examples/demo_quotes.csv",
                "--as-of",
                text,
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "UTC datetime range" in captured.err
    assert journal.read_bytes() == before
