# HackerRank Orchestrate - October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.


## Submitted Solution

This repository contains a complete, working solution: a deterministic
evidence-audit engine written in Python 3 using **only the standard library**.

### Requirements

- Python 3.8 or newer
- No third-party packages, no network access, no API keys, no model calls

### Run

From the repository root:

```bash
python3 code/main.py
```

This reads `dataset/deletion_requests.csv` and writes `output.csv` (518 rows) to
the repository root. Two other entry points are useful for review:

```bash
python3 code/main.py --sample        # 32 public samples -> code/sample_predictions.csv
python3 code/evaluation/metrics.py dataset/sample_output.csv code/sample_predictions.csv
python3 code/evaluation/run_custom_cases.py   # 4 participant-created cases
```

`--dataset DIR`, `--input FILE` and `--output FILE` override the defaults, so the
same code can be pointed at another copy of the corpus.

### Layout

| Path | Role |
| --- | --- |
| `code/main.py` | CLI entry point; writes `output.csv` |
| `code/ir.py` | Parses all 94 artifacts (SQL snapshots, JSON bundles, five log dialects, plain-text state table, transcribed binary facts) into `Fact` records and `Snapshot` captures |
| `code/identity.py` | Current and historical identifier index (email, phone, email hash, stable `user_id`); token-exact matching |
| `code/policy.py` | The supplied rule table and the operation/phase ordering |
| `code/plan.py` | Reconstructs record state at `audit_at`, applies policy, emits findings, remediation and verification in the required order |
| `code/fixtures/binary_facts.json` | Facts transcribed once, offline, from 11 screenshots and 4 PDFs, with the capture time of each file |
| `code/validate.py` | Sample comparison helper |
| `code/evaluation/` | Evaluation and usage reports, the metrics script, and the custom-case runner |

### How a case is decided

1. **Intent.** The request text is classified first. Explicit refusals
   ("not asking for deletion", "não peço exclusão", "ne rien supprimer") and
   competing privacy actions (access, copy, correction, opt-out, login help) make
   the case `NOT_APPLICABLE` with `steps_json=[]`.
2. **Subject.** Current and historical identifiers are resolved from
   `user_identities.csv` and `user_audit_log.csv`, so an old email address in an
   artifact still links to the person.
3. **State at `audit_at`.** Evidence facts carry two times: `observed_at` (when
   the event happened) decides the state, `captured_at` (when the file became
   available) decides whether the file may be cited. Anything captured after
   `audit_at` is never used as proof. Workflow statuses, `reported_result` values
   and activity claims are treated as claims; content wins.
4. **Policy.** Each record is mapped to its category and given the action from
   `deletion_rules.csv`: delete, redact, anonymise, retain, or keep a tombstone
   with only the stable `user_id`. Account tombstones are judged by what they
   still contain.
5. **Steps.** Findings cite the strongest artifact available at the cutoff.
   Remediation stops data flows and blocks unsafe restores before dependent
   copies are changed, and every remediated record gets a post-remediation
   verification. For `INCOMPLETE` cases those future steps use `evidence_id=""`.
   Steps are ordered by phase, then by the operation order in the problem
   statement, then by `system_id` and `resource_id`, and numbered from 1.

### Validation

- Public samples: 32/32 correct `deletion_status`, step F1 0.9309, field accuracy
  0.9045, exact sequence on 21/32 cases.
- Four additional cases (French copy request, Portuguese opt-out, live flow with
  restorable backup, already-complete deletion) pass in
  `code/evaluation/run_custom_cases.py`.
- Full run: 518/518 rows, valid JSON in every row, consecutive step numbers,
  phases in order, `NOT_APPLICABLE` rows empty, `COMPLETE` verification always
  cites evidence.

Details, including the failures found while building the engine and the residual
information that is not present in the participant data, are in
[`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md);
compute and token usage are in
[`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md).


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
