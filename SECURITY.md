# Security reports for LedgerX

Security-sensitive areas include journal and event-feed JSONL parsing, quote CSV inputs, snapshot replacement, hash/checkpoint validation and file handling. For ordinary questions about balanced postings, retry identity and journal/valuation replay, use [SUPPORT.md](SUPPORT.md).

Check the repository's [Security page](https://github.com/dev-belly/LedgerX/security) first. If GitHub displays **Report a vulnerability**, use that private channel. If neither that button nor a private maintainer contact is available, open an issue asking only for a private reporting route; withhold exploit details, sensitive samples and credentials. This document does not assert that private vulnerability reporting is enabled.

Prepare the exact package version or full commit, OS/Python, affected command, expected security boundary, minimal synthetic reproduction and observed impact. Retain the originally affected revision; try current main when practical. Share sensitive details only through the private channel once available.

These are local research tools and evidence checks, not authentication or source-trust services. A valid hash or successful replay cannot authenticate external data or a coordinated replacement of all evidence. Do not use private financial records to demonstrate a vulnerability. No security response time or previous-release support guarantee is published.
