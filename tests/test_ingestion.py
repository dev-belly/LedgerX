from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ledgerx import CashEvent, FillEvent, IntegrityError, Ledger, LedgerError, ingest_journal
from ledgerx.cli import demo, main
from ledgerx.core import GENESIS_HASH
from ledgerx.ingestion import load_events

T0 = datetime(2024, 1, 2, tzinfo=UTC)


def funding(identity: str = "fund", amount: str = "1000") -> CashEvent:
    return CashEvent(identity, T0, "deposit", Decimal(amount))


def buy(identity: str = "buy", *, price: str = "100", day: int = 1) -> FillEvent:
    return FillEvent(
        identity, T0 + timedelta(days=day), "ACME", "buy", Decimal("2"), Decimal(price)
    )


def event_rows() -> list[dict[str, str]]:
    return [record.to_dict()["event"] for record in demo().records]


def write_feed(path, rows: list[dict[str, str]]) -> None:
    path.write_bytes(("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8"))


def test_batch_retries_return_original_records_without_reposting() -> None:
    ledger = Ledger()
    fund = ledger.append(funding())
    records = ledger.append_batch([funding(), buy(), buy()])
    assert records[0] is fund
    assert records[1] is records[2]
    assert [record.sequence for record in records] == [1, 2, 2]
    assert ledger.summary()["cash"] == "800"
    assert ledger.summary()["inventory_cost"] == "200"
    assert ledger.summary()["events"] == 2
    before = ledger.summary()
    assert ledger.append_batch([buy(), funding()]) == (records[1], fund)
    assert ledger.summary() == before
    # The stricter single-event API and deterministic replay remain unchanged.
    with pytest.raises(LedgerError, match="Duplicate event_id"):
        ledger.append(buy())


def test_retry_identity_uses_validated_utc_time_and_decimal_value() -> None:
    ledger = Ledger()
    original = funding(amount="1000.00")
    record = ledger.append(original)
    equivalent = CashEvent(
        "fund", T0.astimezone(timezone(timedelta(hours=8))), "deposit", Decimal("1E+3")
    )
    assert ledger.append_batch([equivalent]) == (record,)
    assert ledger.head_hash == record.hash
    assert record.to_dict()["event"]["amount"] == "1000.00"


@pytest.mark.parametrize(
    "conflict",
    [
        funding(amount="999"),
        buy("fund"),
        buy(price="101"),
        replace(funding(), direction="withdrawal"),
        replace(buy(), symbol="BETA"),
        replace(buy(), side="sell"),
        replace(buy(), quantity=Decimal("3")),
        replace(buy(), fee=Decimal("1")),
        replace(buy(), occurred_at=T0 + timedelta(days=2)),
    ],
)
def test_conflicting_identity_rolls_back_prior_new_events(conflict) -> None:
    ledger = Ledger()
    ledger.append_batch([funding(), buy()])
    before = ledger.summary(), ledger.records, ledger.positions
    with pytest.raises(LedgerError, match="Conflicting event_id"):
        ledger.append_batch([buy("new", day=2), conflict])
    assert (ledger.summary(), ledger.records, ledger.positions) == before
    ledger.append(buy("new", day=2))  # Failure did not reserve the new ID.


def test_conflicting_repetition_within_batch_is_not_a_new_posting() -> None:
    ledger = Ledger()
    before = ledger.summary()
    with pytest.raises(LedgerError, match="Conflicting event_id"):
        ledger.append_batch([funding(), buy(), buy(price="101")])
    assert ledger.summary() == before
    assert ledger.records == ()


def test_unfunded_later_fill_rolls_back_whole_batch() -> None:
    ledger = Ledger()
    ledger.append(funding())
    before = ledger.summary(), ledger.positions, ledger.records
    with pytest.raises(LedgerError, match="Insufficient cash"):
        ledger.append_batch([buy(), buy("too-large", price="1000", day=2)])
    assert (ledger.summary(), ledger.positions, ledger.records) == before


def test_new_facts_are_not_silently_sorted_to_make_a_batch_pass() -> None:
    ledger = Ledger()
    before = ledger.summary()
    with pytest.raises(LedgerError, match="nondecreasing"):
        ledger.append_batch([funding(), buy("later", day=2), buy("earlier", day=1)])
    assert ledger.summary() == before


def test_broken_input_iterator_does_not_commit_its_valid_prefix() -> None:
    ledger = Ledger()
    before = ledger.summary()

    def interrupted_feed():
        yield funding()
        raise RuntimeError("Upstream read failed")

    with pytest.raises(RuntimeError, match="Upstream read failed"):
        ledger.append_batch(interrupted_feed())
    assert ledger.summary() == before


def test_cli_ingestion_retry_preserves_exact_journal_bytes(tmp_path, capsys) -> None:
    journal = tmp_path / "ledger.jsonl"
    feed = tmp_path / "feed.jsonl"
    rows = event_rows()
    write_feed(feed, [*rows, rows[1]])
    assert main(["ingest", str(journal), "--events", str(feed)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["input_rows"] == 4
    assert result["appended_events"] == 3
    assert result["reused_events"] == 1
    assert result["previous_head"] == GENESIS_HASH
    assert result["input_sha256"] == hashlib.sha256(feed.read_bytes()).hexdigest()
    assert result["ledger"] == demo().summary()
    original = journal.read_bytes()
    assert main(["ingest", str(journal), "--events", str(feed)]) == 0
    retry = json.loads(capsys.readouterr().out)
    assert retry["appended_events"] == 0
    assert retry["reused_events"] == 4
    assert journal.read_bytes() == original
    assert (
        Ledger.load(journal, expected_head=result["ledger"]["head_hash"]).summary()
        == result["ledger"]
    )


def test_mixed_retry_and_new_fill_only_extends_history(tmp_path) -> None:
    journal = tmp_path / "ledger.jsonl"
    demo().save(journal)
    before = Ledger.load(journal)
    feed = tmp_path / "feed.jsonl"
    next_fill = FillEvent(
        "fill-003", T0 + timedelta(days=3), "ACME", "sell", Decimal("6"), Decimal("120")
    )
    row = {
        "kind": "fill",
        "event_id": next_fill.event_id,
        "occurred_at": next_fill.occurred_at.isoformat(),
        "symbol": "ACME",
        "side": "sell",
        "quantity": "6",
        "price": "120",
        "fee": "0",
    }
    write_feed(feed, [event_rows()[1], row])
    result = ingest_journal(journal, feed, expected_head=before.head_hash)
    after = Ledger.load(journal)
    assert result["appended_events"] == result["reused_events"] == 1
    assert after.records[:3] == before.records
    assert after.summary()["positions"] == {}
    assert after.summary()["cash"] == "10158"
    assert after.summary()["realized_pnl"] == "158"


@pytest.mark.parametrize("create_new", [False, True])
def test_cli_failure_never_publishes_prefix_or_success_output(tmp_path, capsys, create_new) -> None:
    journal = tmp_path / "ledger.jsonl"
    if not create_new:
        demo().save(journal)
    before = journal.read_bytes() if journal.exists() else None
    feed = tmp_path / "invalid-batch.jsonl"
    rows = event_rows()
    valid = {**rows[1], "event_id": "new-fill", "occurred_at": "2024-01-05T00:00:00Z"}
    invalid = {**valid, "event_id": "too-large", "quantity": "1000000"}
    write_feed(feed, [*rows, valid, invalid])
    assert main(["ingest", str(journal), "--events", str(feed)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Insufficient cash" in output.err
    assert (journal.read_bytes() if journal.exists() else None) == before


def test_snapshot_publication_failure_preserves_original_and_cleans_temporary_file(
    tmp_path, monkeypatch, capsys
) -> None:
    journal = tmp_path / "ledger.jsonl"
    demo().save(journal)
    before = journal.read_bytes()
    feed = tmp_path / "feed.jsonl"
    rows = event_rows()
    write_feed(
        feed, [{**rows[0], "event_id": "extra-funds", "occurred_at": "2024-01-05T00:00:00Z"}]
    )

    def failed_replace(source, destination):
        raise OSError("Synthetic publication failure")

    monkeypatch.setattr("ledgerx.core.os.replace", failed_replace)
    assert main(["ingest", str(journal), "--events", str(feed)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Synthetic publication failure" in output.err
    assert journal.read_bytes() == before
    assert list(tmp_path.glob(".ledger.jsonl.*")) == []


@pytest.mark.parametrize(
    "content, message",
    [
        (b"", "empty"),
        (b"\xff", "UTF-8"),
        (b"\n", "row 1"),
        (b'{"kind":"cash","kind":"cash"}\n', "Duplicate JSON key"),
        (b"[]\n", "must all be strings"),
        (b'{"amount":1.0}\n', "must all be strings"),
        (b'{"amount":null}\n', "must all be strings"),
        (b'{"amount":NaN}\n', "must all be strings"),
    ],
)
def test_event_feed_contract_rejects_ambiguous_or_malformed_rows(
    tmp_path, content, message
) -> None:
    feed = tmp_path / "bad.jsonl"
    feed.write_bytes(content)
    with pytest.raises(LedgerError, match=message):
        load_events(feed)


def test_feed_invalid_second_row_reports_line_without_mutating_journal(tmp_path, capsys) -> None:
    journal = tmp_path / "ledger.jsonl"
    demo().save(journal)
    before = journal.read_bytes()
    feed = tmp_path / "feed.jsonl"
    row = {**event_rows()[0], "event_id": "new-deposit", "occurred_at": "2024-01-05T00:00:00Z"}
    write_feed(feed, [row, {**row, "extra_field": "unexpected"}])
    assert main(["ingest", str(journal), "--events", str(feed)]) == 2
    assert "row 2" in capsys.readouterr().err
    assert journal.read_bytes() == before


def test_expected_head_is_checked_before_import(tmp_path) -> None:
    journal = tmp_path / "ledger.jsonl"
    demo().save(journal)
    before = journal.read_bytes()
    feed = tmp_path / "feed.jsonl"
    write_feed(feed, event_rows())
    with pytest.raises(IntegrityError, match="checkpoint"):
        ingest_journal(journal, feed, expected_head=GENESIS_HASH)
    assert journal.read_bytes() == before
    with pytest.raises(IntegrityError, match="genesis"):
        ingest_journal(tmp_path / "new.jsonl", feed, expected_head=demo().head_hash)


def test_ingestion_does_not_replace_a_broken_existing_journal(tmp_path, capsys) -> None:
    journal = tmp_path / "ledger.jsonl"
    demo().save(journal)
    broken = journal.read_bytes().replace(b'"cash"', b'"fabricated_account"', 1)
    journal.write_bytes(broken)
    feed = tmp_path / "feed.jsonl"
    write_feed(feed, event_rows())
    assert main(["ingest", str(journal), "--events", str(feed)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Invalid journal row 1" in output.err
    assert journal.read_bytes() == broken


def test_published_batch_result_is_reproducible(tmp_path) -> None:
    journal = tmp_path / "published.jsonl"
    feed = Path("examples/ingestion_events.jsonl")
    first = ingest_journal(journal, feed)
    original_bytes = journal.read_bytes()
    retry = ingest_journal(journal, feed)
    expected = json.loads(Path("examples/ingestion_result.json").read_text(encoding="utf-8"))
    assert {"first_import": first, "full_retry": retry} == expected
    assert journal.read_bytes() == original_bytes
