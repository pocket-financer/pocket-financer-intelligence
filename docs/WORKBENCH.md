# Local Corpus & Encrypted Annotation Workbench

The local workbench manages canonical corpus preparation, leakage-safe review
pools, blind human annotation (`CanonicalLabelV2`), adjudication, encrypted
native trace inspection, verified backups, and consent-gated encrypted exports.
All state remains inside ignored `PRIVATE_DATA/sms_processing/` with strict
`0700`/`0600` filesystem permissions.

## 1. Canonical corpus construction (`build-corpus`)

`python scripts/run_sms_processing.py build-corpus` reads
[configs/sms_processing/profiles/archive-india-inr.json](../configs/sms_processing/profiles/archive-india-inr.json)
(override with `--config <path>`) and executes `build_private_corpus`
(`src/pocketfinancer_sms/corpus/`):

- **Opaque HMAC identities**: Generates an ignored 32-byte secret at
  `PRIVATE_DATA/sms_processing/.source-id-key` and maps every source row 1:1 to
  a `src_<hex>` ID without dropping or mutating rows.
- **Privacy-bound grouping** (`corpus/grouping.py`): Computes keyed HMAC digests
  for exact normalized body, template (`<URL>`, `<ADDRESS>`, `<AMOUNT>`,
  `<DATE>`, `<TIME>`, `<IDENTIFIER>`, `<NUMBER>`), sender, sender family,
  sender-template pair, and `YYYY-MM` `time_group`.
- **Template-component-disjoint pool assignment** (`corpus/pools.py`): Assigns
  entire `normalized_template_hash` components atomically to:
  - `regression_only` (templates overlapping `DATA/extraction_ds.jsonl`)
  - `legacy_review` (templates overlapping optional legacy review JSONL)
  - `later_time_holdout` (all component messages >= `later_time_cutoff`)
  - `annotation_training` (80% deterministic bucket)
  - `annotation_development` (10% deterministic bucket)
  - `protected_test` (10% deterministic bucket)
- **Leakage & completeness audit**: Refuses to commit a run if
  `leakage_audit(...)` detects any `source_id`, `exact_body`,
  `normalized_template`, or `sender_template_group` overlap across pools, or if
  `row_count != expected_source_rows`.
- **Outputs**: Writes immutable run artifacts (`canonical_manifest.jsonl`,
  `annotation_queues/`, `reports/`, `provenance.json`, `artifact_hashes.json`)
  under `PRIVATE_DATA/sms_processing/runs/<run_id>/` and updates `CURRENT.json`.

## 2. Encrypted SQLCipher storage (`SecureWorkbenchStore`)

CLI workbench commands enforce `SecureWorkbenchStore`
(`src/pocketfinancer_sms/workbench/secure_store.py`):

- Stores corpus rows, append-only annotation revisions, weak-facet corrections,
  resume state, and imported native traces in
  `PRIVATE_DATA/sms_processing/workbench/workbench-v2.sqlite3`.
- Requires a SQLCipher Python driver (`sqlcipher3` or `pysqlcipher3.dbapi2`) and
  a 32-byte OS credential-vault key via `KeyringKeyProvider` (`keyring`).
- If a legacy plaintext `workbench.sqlite3` exists, commands fail closed until
  explicit migration completes:
  ```bash
  python scripts/run_sms_processing.py migrate-secure-workbench
  ```
- Initialize or refresh the encrypted workbench from `CURRENT.json`:
  ```bash
  python scripts/run_sms_processing.py init-workbench
  ```

## 3. Loopback web UI (`serve-workbench`)

```bash
python scripts/run_sms_processing.py serve-workbench --port 8765
```

Security invariants enforced by `WorkbenchWebServer` (`workbench/web.py`):

- Refuses any bind address other than `127.0.0.1`.
- Generates a fresh per-process `secrets.token_urlsafe(32)` token and requires
  matching `?token=` on `/` plus `X-Workbench-Token` and same-`Origin` header on
  all `/api/*` requests.
- Sets `Content-Security-Policy` (`default-src 'self'`),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, `Cache-Control: no-store`, and suppresses request-path
  logging so search queries never hit logs.

## 4. Annotation policy, blindness & `CanonicalLabelV2`

- **Blind-first protected review**: Rows in `protected_test` and
  `later_time_holdout` hide `analysis`, `weak_facets`, `grouping`,
  `disagreements`, `target_preview`, and suggestion/group filters until the
  reviewer submits an annotation and explicitly calls `/api/reveal`.
- **CanonicalLabelV2** (`pocketfinancer.canonical-label/2`): Validates decision
  (`posted`, `none`, `abstain`), orthogonal `(operational_class, event_state)`,
  optional `financial_family`/`payment_rail`, `uncertain` flag, and zero-based
  half-open Unicode-scalar `SourceSpan` evidence for posted `amount`,
  `direction`, `account`, and optional `counterparty`.
- **Append-only revisions & adjudication**: Drafts, submissions, and
  adjudications (`adjudication_of` revision hashes when multiple reviewers
  semantically disagree) are append-only and hash-verified via
  `verify_revision_chains()`.
- **Target projection (`project_extractor_target`)**: `target_preview` projects
  committed non-uncertain, non-`invalid_outgoing` `CanonicalLabelV2` labels
  directly to `pocketfinancer.sms-extractor/1` JSON (legacy `canonical-label/1`
  revisions remain readable in history and return
  `historical_selector_contract_retired`).
- **Named source sets & native trace import**: `SourceSetCatalog` loads
  hash-verified non-protected review slices from
  `PRIVATE_DATA/sms_processing/evaluations/*/manifest.json`;
  `import-native-trace` decrypts AES-256-GCM Android/iOS trace bundles using an
  explicitly provisioned keyring secret while rejecting any bundle matching
  blind pools before decryption.

## 5. Backups, trace import & consent-gated exports

```bash
# Verified encrypted SQLCipher backup + SHA-256 manifest
python scripts/run_sms_processing.py backup-workbench

# Import an encrypted Android or iOS trace bundle
python scripts/run_sms_processing.py import-native-trace \
  --bundle PRIVATE_DATA/sms_processing/traces/<bundle>.json \
  --key-id native-trace-import-v1

# Export explicitly selected (source_id, reviewer_id, revision, revision_hash) revisions
python scripts/run_sms_processing.py export-workbench \
  --selection-manifest PRIVATE_DATA/sms_processing/workbench/export-selection.json \
  --consent-local-encrypted-export
```

Never tunnel, screen-share, or copy `PRIVATE_DATA/sms_processing/` outside the
host owning the local archive.
