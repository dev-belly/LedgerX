# Support for LedgerX

Start with [the accounting and valuation contract](README.md) and [batch ingestion](docs/BATCH_INGESTION.md). To reproduce the small synthetic journal after installation:

```bash
ledgerx demo --out support-demo.jsonl
ledgerx verify support-demo.jsonl
```

Use a new output path so the demo does not replace a journal you need. Open a [bug report](https://github.com/dev-belly/LedgerX/issues/new/choose) with version/commit, OS/Python, exact command and a few synthetic events or quotes. Retain event IDs, ordering, timezone offsets and Decimal strings; explain expected cash, released cost and realized P&L separately.

For retry problems, show which IDs repeated and whether their economic fields changed. For valuation, retain observed/available timestamps and the as-of cutoff. Missing or stale eligible quotes intentionally fail. A hash chain cannot detect a valid truncated prefix without a separately retained head checkpoint; copying the head from the same file is not an independent check.

[CONTRIBUTING.md](CONTRIBUTING.md) lists lint, types and regression checks. Do not upload real account histories or credentials. Follow [SECURITY.md](SECURITY.md) for sensitive reports.
