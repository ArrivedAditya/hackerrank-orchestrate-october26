# Usage Report — Final Full-Dataset Run

## 1. Summary

| Item | Value |
| --- | --- |
| Command | `python3 code/main.py` |
| Input | `dataset/deletion_requests.csv` (518 cases) |
| Output | `output.csv` (518 data rows, root of the repository) |
| Model providers | **none** |
| Model names | **none** |
| Model calls | **0** |
| Input tokens | **0** |
| Output tokens | **0** |
| Total tokens | **0** |
| Tokens per request | **0** |
| Estimated total cost | **$0.00** |
| Estimated cost per request | **$0.00** |
| Wall clock | ≈ 5.5 s for 518 cases on one CPU core |
| Peak memory | < 200 MB (all 2,579 evidence facts held in memory) |
| Network access | none |

There is no per-model table to report because no model is used: the table would
have a single row of zeros.

## 2. Why there is no token usage

The agent is a deterministic evidence engine, not a prompt chain. Every decision
is a pure function of participant-facing files:

1. **Parsing** (`code/ir.py`) turns all 94 artifacts into 2,579 `Fact` records and
   253 table snapshots: SQL snapshot exports (four topologies: primary, regional,
   reporting, failover), JSON collection bundles, five log dialects plus
   JSON-lines logs, one plain-text state table, and the transcribed binary
   artifacts.
2. **Identity** (`code/identity.py`) resolves current and historical emails,
   phone numbers and email hashes from `user_identities.csv` and
   `user_audit_log.csv`; matching is token-exact so similar-but-different
   addresses do not merge two people.
3. **Policy** (`code/policy.py`) reads `deletion_rules.csv` and maps each data
   category to its required action.
4. **Planning** (`code/plan.py`) reconstructs the state of every record at the
   case's `audit_at`, applies the policy, and emits findings, remediation and
   verification in the required order.

Because the same inputs always produce the same output, the submission can be
re-run by a reviewer and diffed byte for byte, and it cannot fail on a rate
limit, an expired key, or a provider outage.

## 3. Where model-like judgement was needed

A few artifacts carry information only as pixels or PDF layout. Those were read
**once, offline, during development**, and recorded as reviewable fixture data in
`code/fixtures/binary_facts.json` (18 facts from 11 screenshots and 4 PDF
documents). Each fact stores the resource id, system, category, state, subject
identifiers, and the manifest capture time of the file it came from.

This is a build-time transcription, not a runtime model call:

* the fixture is committed and diff-able;
* every fact is cross-checked against a second artifact where one exists (for
  example the data-catalog row `activity_4010` links `audit_record_003` to
  `user_03`, which is why the six rows on the audit console collapse to the one
  record the reference expects);
* the run itself only reads JSON.

`pdftotext` was used offline to read the four PDFs while building the fixture. It
is **not** a runtime dependency: the submitted solution imports only the Python
standard library (`csv`, `json`, `re`, `sqlite3`, `dataclasses`, `argparse`).

## 4. Per-request compute

| Measure | Per request | Total (518) |
| --- | --- | --- |
| Model calls | 0 | 0 |
| Tokens | 0 | 0 |
| Facts considered | ≈ 5 facts after subject resolution | 2,579 parsed once per run |
| Steps emitted | 5.18 | 2,683 |

The evidence corpus is parsed once per process and reused for all 518 cases; the
per-case work is an in-memory filter over the subject's facts plus the policy
table.

## 5. Cost

| Component | Cost |
| --- | --- |
| Model input tokens | $0.00 |
| Model output tokens | $0.00 |
| External APIs | $0.00 |
| Storage / hosting | $0.00 (local files only) |
| **Total** | **$0.00** |
| **Per request** | **$0.00** |

No API keys, credentials, or provider configuration are read at run time, and
none are stored in the repository.
