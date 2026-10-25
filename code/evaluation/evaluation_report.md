# Evaluation Report — Ghost Data Still Exists

All numbers in this report come from commands that can be re-run from the
repository root:

```bash
python3 code/main.py --sample                      # 32 public cases -> code/sample_predictions.csv
python3 code/evaluation/metrics.py dataset/sample_output.csv code/sample_predictions.csv
python3 code/evaluation/run_custom_cases.py        # 4 participant-created cases
python3 code/main.py                               # 518 cases -> output.csv
```

The engine is deterministic and offline: no model is called at inference time, so
repeated runs produce byte-identical files.

---

## 1. What is being measured

The public gate is the 32-case `dataset/sample_output.csv`. Four levels are
scored, because a case can be right at the status level and still be wrong in
what it asks a remediation team to do:

| Level | Definition |
| --- | --- |
| Case status | `deletion_status` equals the reference value |
| Step | a `(phase, operation, system_id, resource_id)` tuple present in both files; precision/recall/F1 over those tuples |
| Field | walking both ordered arrays position by position, the share of the seven fields (`step_no`, `phase`, `system_id`, `resource_id`, `operation`, `state`, `evidence_id`) that match |
| Sequence | the case's complete ordered step list is identical |

The field level is positional on purpose: the grader preserves array order, so a
missing step must cost the fields it would have contributed.

---

## 2. Final results on the public samples

| Metric | Value |
| --- | --- |
| Cases | 32 |
| Case-level status accuracy | **1.0000** (32/32) |
| Step precision | **0.9563** (175 matched, 8 extra) |
| Step recall | **0.9067** (175 matched, 18 missing) |
| Step F1 | **0.9309** |
| Field-level accuracy | **0.9045** |
| Sequence accuracy | **0.6562** (21/32 exact) |

Field breakdown:

| Field | Accuracy |
| --- | --- |
| `step_no` | 0.9482 |
| `phase` | 0.9482 |
| `system_id` | 0.8964 |
| `resource_id` | 0.8549 |
| `operation` | 0.8964 |
| `state` | 0.8964 |
| `evidence_id` | 0.8912 |

Reading the numbers:

* every case status is correct, including the seven cases where the workflow
  claim (`COMPLETE` / `PARTIALLY_COMPLETE` / `NOT_STARTED`) contradicts the
  evidence;
* the 18 missing steps are resource ids that appear **nowhere** in `dataset/`
  linked to their subject (see §5.1), so they are not recoverable from
  participant data;
* the 8 extra steps are account ids chosen by the activity fallback for cases
  where no artifact links the subject to an account id — the same root cause as
  nine of the missing steps, seen from the other side;
* `evidence_id` is 0.8912 despite the missing and extra steps, i.e. when a step
  is found at the right position, the citation is nearly always the reference
  citation too.

Full-dataset distribution produced by the final run (518 rows):

| Status | Cases | Share |
| --- | --- | --- |
| COMPLETE | 192 | 37.1% |
| INCOMPLETE | 316 | 61.0% |
| NOT_APPLICABLE | 10 | 1.9% |

2,683 steps in total, 5.18 steps per case on average, 5.5 s wall clock.

---

## 3. Participant-created evaluation cases

The public samples contain **no** `NOT_APPLICABLE` row and no case that combines a
live connector with a restorable archive, so `code/evaluation/run_custom_cases.py`
builds a private four-case dataset at run time and audits it with the real
engine. The dataset is generated in a temporary directory, never shipped as
answer data, and compared with an explicit expectation per case.

| Case | Scenario | Expected | Result |
| --- | --- | --- | --- |
| `custom_01` | French request for a **copy** of the data, with "ne rien supprimer" (do not delete anything) | `NOT_APPLICABLE`, no steps | PASS |
| `custom_02` | Portuguese **marketing opt-out** that keeps the account and order history ("não peço exclusão") | `NOT_APPLICABLE`, no steps | PASS |
| `custom_03` | Genuine deletion where the account and a search document survive, a connector is still delivering and a backup can still be restored; a security audit record is inside its retention window | `INCOMPLETE` with 4 findings, `DISABLE_DATA_FLOW`/`BLOCK_RESTORE` **before** the deletes, retained audit verified | PASS |
| `custom_04` | Deletion already finished: account lookup returns no row, tombstone keeps only `user_c4`, analytics profile already unlinked, audit record retained | `COMPLETE` with exactly 4 verification steps, each citing evidence | PASS |

`custom_03` also asserts the invariants the grader cares about: phases appear in
`FINDING → REMEDIATION → VERIFICATION` order, step numbers start at 1 and are
consecutive, remediation/verification evidence for an incomplete case is `""`, and
every `COMPLETE` verification step cites an artifact.

**These cases found two real defects** (both fixed, see §4):

1. `custom_01` and `custom_02` were classified as deletion requests. The intent
   classifier matched the deletion verb ("supprimer", "exclusão") inside a
   sentence that explicitly refuses deletion, and it did not recognise
   non-English requests for a copy/opt-out.
2. `custom_04` produced no account step: a lookup that returns no row (`404`) was
   parsed but not linked to a subject, so the case looked empty rather than
   "account verified absent".

---

## 4. Failures found and improvements made

The engine was built by iterating against the public samples. Each row is a
defect that cost points, the fix, and the effect on the status/step metrics.

| # | Failure observed | Root cause | Fix |
| --- | --- | --- | --- |
| 1 | Baseline status accuracy 0.7812 (25/32) | Resource state was read at the artifact capture time, so an event that happened before `audit_at` inside a file written after it was thrown away | Split `Fact.observed_at` (event time, drives state) from `Fact.captured_at` (file availability, drives citations) |
| 2 | Support tickets were deleted when the message mentioned an order or an agent, and kept otherwise | `SUPPORT_MESSAGE` vs `SUPPORT_ATTACHMENT` was decided by keyword search over the message text | Made the treatment category-driven from `deletion_rules.csv`: messages are redacted, attachments are deleted |
| 3 | `case_03` cited an account but not the anonymised analytics row | Records inside a JSON wrapper (`profile`, `document`) hid their identifiers one level down | Added `_flatten_identifiers`, which lifts identifiers from wrapper objects |
| 4 | `case_12`, `case_19`, `case_17` lost audit and support steps | Identifier fields were incomplete: the corpus also uses `subject_ref`, `owner_key`, `scan_key`, `matched_value`, and `correlation_key` (which can carry a **historical** `email_hash`) | Extended `_subject_from` and the hash index to all corpus identifier keys |
| 5 | `case_32` reported a tombstone only as a PII exposure without the redaction path | `account_status=DELETED` was read as "row absent" | Tombstones are now interpreted by content: direct identifiers still present → `REPORT_EXPOSED_PII` + `REDACT_RECORD`; identifiers gone → `VERIFY_RECORD_REDACTED` |
| 6 | Retained records that were already compliant produced no verification step (`support_4715`, `analytics_4566`) | A kept row with no direct identifiers was still scored as `PRESENT` | Added the effective-state rule: a retained row without direct identifiers is `REDACTED`/`ANONYMIZED` and is verified, not remediated |
| 7 | Five sample cases with a legal hold produced a legal step that the reference did not contain | Legal holds are retained by finance order, not by the deletion workflow | Excluded `LEGAL_HOLD_RECORD` from the verified record set |
| 8 | `case_10` was reported `COMPLETE` | A deletion-marker check with `marker_found=false` was read as "deleted"; it means the opposite — no marker, row survived | Inverted the marker rule; a legacy contact-index hit on a replica is now a cache copy that is deleted |
| 9 | Some account ids were guessed from an unrelated workflow block | The activity-only fallback took any nearby account row | Added an ownership index from evidence and a co-occurrence window; ids attributed to another user are rejected |
| 10 | Cases 533–542: only 8 of the 10 non-deletion requests were `NOT_APPLICABLE` | "remove me from marketing lists" matched the deletion stem, and a copy request containing a deletion verb was treated as deletion | Rewrote intent classification (see §3): explicit refusals first, then explicit deletion commands, then competing privacy actions |
| 11 | Four `VERIFY_RECORD_PRESENT` audit steps were missing (`audit_record_1707`, `3954`, `4128`, `7095`) although the rows are visible in `customer_data_snapshot_bundle_20260917.json` | JSON records were stamped with the export time of the file (2026-09-17), not the capture time of their own snapshot, so at an `audit_at` of 2026-09-15 they looked unavailable | Retained audit collections now take the inner snapshot `captured_at` (§5.2) |
| 12 | `case_29` reported `account_006`, which belongs to Jonas Berg, not to the subject | The ownership guard only knew owners of records that were themselves linked; a search document's `source_resource_id` names its account but was not registered as an owner link | Register `source_resource_id` in the ownership index |

Effect of the sequence of fixes on the public gate:

| Iteration | Status | Step P | Step R | Field |
| --- | --- | --- | --- | --- |
| First working engine | 0.7812 | 0.79 | 0.66 | 0.72 |
| Event-time vs capture-time split, category policy, binary fixtures | 0.8125 | 0.9078 | 0.6632 | 0.7742 |
| Wrapper identifiers, identifier keys, tombstone content model | 0.9375 | 0.8909 | 0.7617 | 0.8231 |
| Effective compliance state, scope of retained records | 0.9688 | 0.9273 | 0.7927 | 0.8677 |
| Nested `document`/`profile` identifiers, account ownership | 0.9688 | 0.9480 | 0.8497 | 0.9181 |
| Effective state for kept records, cache/marketing reclassification | 0.9688 | 0.9497 | 0.8653 | 0.9195 |
| Tombstone state uses record category; activity citation fallback | **1.0000** | 0.9497 | 0.8808 | 0.8690 |
| Snapshot-scoped audit capture time, `source_resource_id` ownership | **1.0000** | **0.9563** | **0.9067** | **0.9045** |

Two structural points came out of the debugging that are worth stating
explicitly, because both are traps in the dataset:

* `search_index_inspector_20260901.png` shows two hits for a substring search.
  Only `search_428` carries `user_13`; `search_1874` belongs to a different person
  with a similar address. Identifier matching is therefore token-exact, never a
  substring test.
* `marketing_profile_console.png` shows `marketing_001` for a person who is not
  the subject named in `activity_4008`. The image wins: the artifact is the
  record, the activity row is a claim.

---

## 5. Gap accounting, engine defects and residual losses

### 5.1 Where the 26 lost step slots come from

The reference contains 193 steps for the 32 samples; the engine emits 183. 175
match, so the gap is **26 step slots** (18 missing, 8 extra). Every slot is
attributable to one of two root causes:

| Root cause | Missing | Extra | Cases |
| --- | --- | --- | --- |
| A. The subject's account id is in no participant artifact | 9 | 8 | 04, 18, 21, 23, 25, 26, 27, 28, 31 |
| B. A retained or anonymised row carries no identifier anywhere | 9 | 0 | 03, 04, 18, 20, 21, 25, 26, 27, 28 |

Root cause A, missing: `account_004`, `account_5521`, `account_6440`,
`account_410`, `account_3136`, `account_5684`, `account_3866`, `account_5482`,
`account_4857`. These ids exist in `sample_output.csv` and, for some of them, as
`resource_id` values in `system_activity.csv`, which has no user column. Nothing
links them to a person, so the engine cannot produce them; the activity fallback
instead guesses and lands on a different id in the same deletion run:
`account_1133`, `account_9155` (twice), `account_7225` (twice), `account_7012`
(twice, the right id for a *different* case) and `account_8858`. Those are the
8 extra steps — the same cause viewed from the other side, not a separate
class of error.

Root cause B, missing: `analytics_9406`, `analytics_901`, `analytics_5335`,
`analytics_1895`, `analytics_4566`, `analytics_4574`, `analytics_6077` and
`audit_record_6645`, `audit_record_5569`. All nine are kept rows (`ANONYMIZE` or
`RETAIN`) whose identifier fields are already gone, so they are compliant and
would only ever appear as a `VERIFY_RECORD_ANONYMIZED` / `VERIFY_RECORD_PRESENT`
step. They sit in export files next to rows that *are* linkable; naming them
would require guessing from list position, and the service exports are sorted by
resource id, so position carries no identity information.

Coverage: **26 of 26 slots explained (100%)**, 18 missing by genuine absence of
information and 8 extra as the mirror image of cause A.

### 5.2 Corrections to an earlier revision of this report

An earlier draft of this section made two claims that the audit disproved. They
are recorded here because the wrong version is the intuitive one:

1. **"`account_7225`, `account_9155`, `account_1133` never appear in evidence"**
   — wrong. Those ids are not reference answers at all; they are the engine's own
   activity-fallback guesses and appear in no truth file. The ids that exist only
   in the reference are the nine listed under root cause A. Mixing the two lists
   made a false positive look like missing information.
2. **"These are not modelling errors; the information is absent"** — only partly
   true. Five of the slots previously filed here were *engine* defects on data
   that is present in `dataset/`:
   `audit_record_4128`, `audit_record_7095`, `audit_record_3954`,
   `audit_record_1707` (linkable inside
   `customer_data_snapshot_bundle_20260917.json`) and `account_006` (linkable as
   the `source_resource_id` of `search_001`). They are fixed; see §5.3.

The audit that produced this table started from 32 slots (23 missing, 9 extra) of
which 27 were explained by this section and **5 were not**:
`audit_record_1707`, `audit_record_3954`, `account_4857`, `account_006`,
`account_8858`. Disposition of those five: two became fix D1, one became fix D2,
and `account_4857` / `account_8858` are now named explicitly as the root-cause-A
pair for `case_31`.

### 5.3 Engine defects, and the fixes that were and were not safe

**D1 — snapshot-level `captured_at` ignored (fixed, narrowed).**
`customer_data_snapshot_bundle_20260917.json` is exported on 2026-09-17 but each
collection inside it carries its own `captured_at`, several of them
2026-09-15T12:00Z. `parse_json` stamped every record with the export time, so at
an `audit_at` of 2026-09-15T12:00 the audit rows looked unavailable even though
the snapshot that quotes them was taken exactly at the cutoff.

The obvious fix — use the inner `captured_at` for every record — was implemented
and measured before being accepted. It is **not** safe:

| Variant | Status | Step P | Step R | Step F1 | Field | Sequence |
| --- | --- | --- | --- | --- | --- | --- |
| Shipped engine before the fix | 1.0000 | 0.9497 | 0.8808 | 0.9140 | 0.8690 | 0.5625 |
| Inner `captured_at` for all records | 0.9688 | 0.9448 | 0.8860 | 0.9144 | 0.7997 | 0.5938 |
| Inner `captured_at` only for retained audit rows | 1.0000 | 0.9508 | 0.9016 | 0.9255 | 0.9038 | 0.6250 |
| Plus `source_resource_id` ownership (shipped) | **1.0000** | **0.9563** | **0.9067** | **0.9309** | **0.9045** | **0.6562** |

The full-scope variant fails for two reasons that the narrow scope avoids:

* **Replica drift.** The bundle also holds an `account_service` snapshot from
  2026-09-09 in which `account_5684` is still `ACTIVE`. Making that snapshot
  available at the cutoff turns a deleted account back into a remaining record,
  so `case_26` flips from `COMPLETE` to `INCOMPLETE`. The correct treatment for
  operable rows is *supersede* semantics — a later snapshot of the same store
  replaces an earlier one, and absence in the later snapshot proves deletion —
  not a rescoped timestamp.
* **Citation preference.** For `case_21`, `case_23`, `case_24` and `case_25` the
  reference cites `evidence_92` (`service_resource_export_20260915_114455.json`,
  the source-system export) while the bundle becomes the fresher candidate and
  wins `_cite`. Reference id accuracy falls from 0.8964 to 0.7784 on that field.

What ships is the part with no such coupling: the inner `captured_at` is applied
only to `SECURITY_AUDIT_LOG` rows (`ir.SNAPSHOT_SCOPED_CATEGORIES`). Retained
audit rows are append-only, so a snapshot time is availability information only,
never a state change. Effect: 4 recovered steps (`audit_record_1707`,
`audit_record_3954`, `audit_record_4128`, `audit_record_7095`), recall 0.8808 →
0.9016, field accuracy 0.8690 → 0.9038, status unchanged at 32/32.

**D1b — supersede semantics for operable rows (not shipped).** The remainder of D1
needs, per `(store, collection)` series, "the latest snapshot at or before the
cutoff defines the state of each resource, and a resource absent from it is
deleted", plus a citation preference for the origin-system export over the
assurance bundle. Both are real rules, but they change *decisions* rather than
availability: the 2026-09-09 replica would have to stop producing findings at a
cutoff where a 2026-09-15 snapshot of the same store already shows the row gone.
Implementing it would also change citations for roughly eight cases. It is
recorded here as the next correctness improvement, not applied, because the
measurement above shows a half-implemented version costs status accuracy.

**D2 — ownership hints from `source_resource_id` (fixed).** A search document
names the account it was built from. The ownership guard only registered owners
of records that were themselves linked, so `account_006` (Jonas Berg) was
available to `case_29` as a candidate and was reported. Registering
`source_resource_id` in the ownership index removes that extra and lets the
fallback settle on `account_7012`: precision 0.9508 → 0.9563, one extra
removed, one missing recovered.

**D3 — two `COMPLETE` cases with no steps (`case_61`, `case_63`) (no fix).** No
evidence for these subjects is captured at or before `audit_at`, so any step list
would be invented. The status is still correct; the step-level loss is accepted.

### 5.4 Residual losses and deliberate trade-offs

Still genuinely absent from the participant-facing data:

* the nine account ids of root cause A, and the eight fallback guesses that stand
  in for them;
* the nine identifier-free retained rows of root cause B;
* all records of `case_61` and `case_63` before their cutoffs.

Deliberate choices that trade recall for correctness:

* a verification step is never emitted for a resource id the engine cannot
  support with evidence or activity;
* a `COMPLETE` case only lists records that are actually evidenced, so an unknown
  duplicate cannot inflate a passing case;
* binary artifacts (11 screenshots, 4 PDFs) are transcribed once into
  `code/fixtures/binary_facts.json` with the capture time of the file, and only
  facts corroborated by a second artifact (for example the data-catalog row that
  links `audit_record_003` to `user_03`) are kept when a screenshot shows more
  rows than the reference does.

---

## 6. Reproducing the numbers

```bash
python3 code/main.py --sample
python3 code/evaluation/metrics.py dataset/sample_output.csv code/sample_predictions.csv
python3 code/evaluation/run_custom_cases.py
python3 code/main.py
```

Expected: 32/32 sample statuses, step F1 0.9309, field accuracy 0.9045, sequence
21/32, all four custom cases passing, and 518 rows in `output.csv`
(192 `COMPLETE`, 316 `INCOMPLETE`, 10 `NOT_APPLICABLE`, 2,683 steps).
