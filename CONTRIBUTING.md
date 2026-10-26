# Contributing to LedgerX

Read [the accounting and valuation contract](README.md) and [batch ingestion](docs/BATCH_INGESTION.md) first. Contributions should preserve balanced postings, deterministic replay, exact retry identity and the separation between historical cost and independent market marks.

## Set up and validate

From the repository root, use Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m ruff format --check src tests
python -m mypy src
python -m pytest -q
```

On Windows PowerShell, activation is optional:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest -q
```

CI runs these checks on Linux Python 3.11–3.13 and Windows Python 3.12. Keep committed text in LF form: quote CSV byte hashes are evidence, and a newline conversion must not be hidden by refreshing a digest.

## Evidence for behavior changes

Use small synthetic events with independently calculated cash, cost release, fees and realized P&L. For rejected batches, check both in-memory state and the unchanged journal; include conflicting retry IDs or a later failing row when relevant. Snapshot-write failures must not leave a success result or temporary files.

For marks, test observed and available times separately, missing/stale quotes and the as-of cutoff. Do not infer a price from a trade fill or post a valuation as a journal event. A stored journal head is only an independent checkpoint when retained separately before verification.

Explain the trigger, before/after behavior and checks actually run. Documentation/configuration changes may use relevant link, YAML and diff checks. Do not add real trading journals, account identifiers or credentials. See [SUPPORT.md](SUPPORT.md) for questions and [SECURITY.md](SECURITY.md) for sensitive reports.
