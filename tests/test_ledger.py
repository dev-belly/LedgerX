from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from random import Random

import pytest

from ledgerx import CashEvent, FillEvent, IntegrityError, Ledger, LedgerError
from ledgerx.cli import main

T0 = datetime(2024, 1, 2, tzinfo=UTC)


def cash(event_id: str, direction: str, amount: str, days: int = 0) -> CashEvent:
    return CashEvent(event_id, T0 + timedelta(days=days), direction, Decimal(amount))


def fill(
    event_id: str, side: str, quantity: str, price: str, fee: str = "0", days: int = 1
) -> FillEvent:
    return FillEvent(
        event_id,
        T0 + timedelta(days=days),
        "ACME",
        side,
        Decimal(quantity),
        Decimal(price),
        Decimal(fee),
    )


def funded() -> Ledger:
    ledger = Ledger()
    ledger.append(cash("fund", "deposit", "10000"))
    return ledger


def test_fees_cost_basis_realized_pnl_and_balance_sheet() -> None:
    ledger = funded()
    ledger.append(fill("buy", "buy", "10", "100", "1"))
    sale = ledger.append(fill("sell", "sell", "4", "110", "1", days=2))
    assert sum((entry.amount for entry in sale.entries), Decimal("0")) == 0
    assert ledger.positions["ACME"].quantity == Decimal("6")
    assert ledger.positions["ACME"].cost_basis == Decimal("600.6")
    summary = ledger.summary()
    assert Decimal(summary["cash"]) == Decimal("9438")
    assert Decimal(summary["realized_pnl"]) == Decimal("38.6")
    assert Decimal(summary["book_equity"]) == Decimal("10038.6")
    assert ledger.balance("realized_pnl") == Decimal("-38.6")


def test_full_close_releases_remaining_fractional_cost() -> None:
    ledger = funded()
    ledger.append(fill("buy", "buy", "3", "1", days=1))
    ledger.append(fill("sell-1", "sell", "1", "2", days=2))
    ledger.append(fill("sell-2", "sell", "2", "2", days=3))
    assert ledger.positions == {}
    assert ledger.balance("inventory:ACME") == 0
    assert Decimal(ledger.summary()["realized_pnl"]) == Decimal("3")


def test_repeated_fractional_closes_preserve_exact_balance_sheet() -> None:
    rng = Random(7)
    ledger = Ledger()
    ledger.append(cash("fund", "deposit", "1000000"))
    for n in range(1, 201):
        quantity = Decimal(rng.randrange(1, 1000)) / Decimal("1000")
        side = "sell" if ledger.positions.get("ACME") and rng.random() < 0.4 else "buy"
        if side == "sell":
            quantity = min(quantity, ledger.positions["ACME"].quantity)
        ledger.append(
            FillEvent(
                str(n),
                T0 + timedelta(minutes=n),
                "ACME",
                side,
                quantity,
                Decimal(rng.randrange(100, 3000)) / Decimal("100"),
                Decimal("0.001"),
            )
        )
        summary = ledger.summary()
        assert Decimal(summary["book_equity"]) == (
            Decimal(summary["net_contributions"]) + Decimal(summary["realized_pnl"])
        )


def test_money_quantum_and_half_even_fill_rounding() -> None:
    with pytest.raises(LedgerError, match="multiple"):
        CashEvent("tiny", T0, "deposit", Decimal("0.000000001"))
    with pytest.raises(LedgerError, match="multiple"):
        FillEvent("tiny", T0, "ACME", "buy", Decimal("1"), Decimal("1"), Decimal("0.000000001"))

    ledger = funded()
    ledger.append(
        FillEvent(
            "round", T0 + timedelta(days=1), "ACME", "buy", Decimal("1"), Decimal("0.000000015")
        )
    )
    assert ledger.positions["ACME"].cost_basis == Decimal("0.00000002")


def test_large_position_retains_small_fractional_fill_exactly() -> None:
    ledger = Ledger()
    ledger.append(cash("fund", "deposit", "1000000000"))
    ledger.append(fill("large", "buy", "10000000000000000", "0.00000001"))
    ledger.append(fill("small", "buy", "0.000000000001", "10000000000000000"))
    assert ledger.positions["ACME"].quantity == Decimal("10000000000000000.000000000001")
    assert ledger.positions["ACME"].cost_basis == Decimal("100010000")
    assert ledger.summary()["events"] == 3


def test_accounting_is_independent_of_callers_decimal_precision() -> None:
    ledger = funded()
    ledger.append(fill("buy", "buy", "1.00000001", "123.45678901"))
    expected = ledger.summary()
    with localcontext() as context:
        context.prec = 6
        assert ledger.summary() == expected
        assert ledger.positions["ACME"].cost_basis == Decimal(expected["inventory_cost"])


def test_fill_precision_limit_is_explicit() -> None:
    with pytest.raises(LedgerError, match="at most 24 decimal places"):
        fill("too-fine", "buy", "1.0000000000000000000000001", "1")


def test_rejected_events_leave_ledger_unchanged() -> None:
    ledger = funded()
    before = ledger.summary()
    with pytest.raises(LedgerError, match="Insufficient cash"):
        ledger.append(fill("expensive", "buy", "101", "100"))
    with pytest.raises(LedgerError, match="Insufficient position"):
        ledger.append(fill("sell", "sell", "1", "100"))
    with pytest.raises(LedgerError, match="Insufficient cash"):
        ledger.append(cash("withdraw", "withdrawal", "10001", days=1))
    assert ledger.summary() == before


def test_sell_fee_cannot_overdraw_settled_cash() -> None:
    ledger = Ledger()
    ledger.append(cash("fund", "deposit", "10"))
    ledger.append(fill("buy", "buy", "1", "10"))
    before = ledger.summary()
    with pytest.raises(LedgerError, match="Insufficient cash to cover sell fee"):
        ledger.append(fill("sell", "sell", "1", "1", fee="2", days=2))
    assert ledger.summary() == before


def test_duplicate_id_and_out_of_order_timestamp_are_rejected() -> None:
    ledger = funded()
    with pytest.raises(LedgerError, match="Duplicate event_id"):
        ledger.append(cash("fund", "deposit", "1", days=1))
    with pytest.raises(LedgerError, match="nondecreasing"):
        ledger.append(cash("earlier", "deposit", "1", days=-1))
    assert len(ledger.records) == 1


@pytest.mark.parametrize(
    "event",
    [
        lambda: CashEvent("bad", T0.replace(tzinfo=None), "deposit", Decimal("1")),
        lambda: CashEvent("bad", T0, "deposit", 1.2),
        lambda: CashEvent("bad", T0, "deposit", Decimal("NaN")),
        lambda: FillEvent("bad", T0, "lowercase", "buy", Decimal("1"), Decimal("2")),
        lambda: FillEvent("bad", T0, "ACME", "buy", Decimal("0"), Decimal("2")),
    ],
)
def test_invalid_event_is_rejected(event) -> None:
    with pytest.raises(LedgerError):
        event()


def test_round_trip_and_hash_chain_are_deterministic(tmp_path) -> None:
    ledger = funded()
    ledger.append(fill("buy", "buy", "10", "100", "1"))
    ledger.append(fill("sell", "sell", "4", "110", "1", days=2))
    journal = tmp_path / "nested" / "journal.jsonl"
    ledger.save(journal)
    assert Ledger.load(journal).summary() == ledger.summary()
    assert Ledger.load(journal).head_hash == ledger.head_hash
    assert [record.sequence for record in ledger.records] == [1, 2, 3]
    assert ledger.records[1].previous_hash == ledger.records[0].hash


def test_independent_checkpoint_detects_truncated_journal(tmp_path) -> None:
    ledger = funded()
    ledger.append(fill("buy", "buy", "10", "100"))
    journal = tmp_path / "journal.jsonl"
    ledger.save(journal)
    original_head = ledger.head_hash
    journal.write_text(journal.read_text().splitlines(keepends=True)[0])
    assert Ledger.load(journal).summary()["events"] == 1
    with pytest.raises(IntegrityError, match="expected checkpoint"):
        Ledger.load(journal, expected_head=original_head)
    with pytest.raises(IntegrityError, match="64 lowercase hex"):
        Ledger.load(journal, expected_head="bad")


@pytest.mark.parametrize("replacement", ['"sequence": true', '"sequence": 1.0'])
def test_replay_rejects_sequence_with_wrong_json_type(tmp_path, replacement: str) -> None:
    journal = tmp_path / "journal.jsonl"
    funded().save(journal)
    journal.write_text(journal.read_text().replace('"sequence":1', replacement))
    with pytest.raises(IntegrityError, match="row"):
        Ledger.load(journal)


def test_replay_rejects_duplicate_json_keys(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    funded().save(journal)
    journal.write_text(journal.read_text().replace('"sequence":1', '"sequence":1,"sequence":1'))
    with pytest.raises(IntegrityError, match="Duplicate JSON key"):
        Ledger.load(journal)


@pytest.mark.parametrize("tamper", ["amount", "entry", "hash", "reorder", "duplicate"])
def test_tampered_journal_is_rejected(tmp_path, tamper: str) -> None:
    ledger = funded()
    ledger.append(fill("buy", "buy", "10", "100", days=1))
    journal = tmp_path / "journal.jsonl"
    ledger.save(journal)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    if tamper == "amount":
        rows[0]["event"]["amount"] = "9999"
    elif tamper == "entry":
        rows[1]["entries"][0]["amount"] = "0"
    elif tamper == "hash":
        rows[0]["hash"] = "0" * 64
    elif tamper == "reorder":
        rows.reverse()
    else:
        rows.append(rows[-1])
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(IntegrityError, match="row"):
        Ledger.load(journal)


def test_cli_demo_and_verify(tmp_path, capsys) -> None:
    journal = tmp_path / "demo.jsonl"
    assert main(["demo", "--out", str(journal)]) == 0
    demo_summary = json.loads(capsys.readouterr().out)
    assert demo_summary["realized_pnl"] == "38.6"
    assert main(["verify", str(journal)]) == 0
    assert json.loads(capsys.readouterr().out) == demo_summary
    assert main(["verify", str(journal), "--expected-head", demo_summary["head_hash"]]) == 0
    capsys.readouterr()
    assert main(["verify", str(journal), "--expected-head", "0" * 64]) == 2
    assert "expected checkpoint" in capsys.readouterr().err


def test_empty_journal_round_trip(tmp_path) -> None:
    journal = tmp_path / "empty.jsonl"
    Ledger().save(journal)
    assert Ledger.load(journal).summary()["events"] == 0
