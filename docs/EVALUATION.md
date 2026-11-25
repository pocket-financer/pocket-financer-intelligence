# SMS Pipeline Evaluation Guide

`src/pocketfinancer_sms/evaluation.py` implements unified, private-safe,
checkpointed, and lock-protected evaluation over the exact host contracts in
`configs/sms_processing/evaluations/model-quality.json`.

## Evaluation lanes

| Lane ID | Runner / Entry point | Wire transport & runtime | Notes |
|---|---|---|---|
| `android_target_gguf` | `evaluate-extractor --gguf <path.gguf>` | `direct_json` via `llama-cpp-python` (embedded GGUF chat template, greedy temp `0.0`, optional `sms-extractor.gbnf`) | Host WSL GGUF parity proxy; host latency is not Android phone latency |
| `apple_fm_python` | `evaluate-apple-fm` | `apple_fm_guided_flat` via `apple-fm-sdk` on compatible macOS Apple Silicon (`apple-fm-flat.schema.json` -> `project_apple_fm_flat_output`) | Live run requires macOS + Apple Intelligence; WSL/Linux runs fake-provider unit tests only |
| `synthetic-stateful-pipeline/1` | `SyntheticPipelineRun` (Python test suite) | `SuppliedRuntime` over isolated SQLite | Verifies admission, tombstones, 5-min duplicate window, confirmation/dismissal, concurrency, crash recovery, and 64-bit money bounds |

Every live evaluation run locks its output directory with `flock` (`run.lock`),
rejects outputs outside `PRIVATE_DATA/sms_processing/`, enforces `0700`/`0600`
permissions, checkpoints completed rows atomically, supports Ctrl+C
interruption/resume when configuration and dataset fingerprints match, and
never echoes raw SMS bodies, prompts, or raw model outputs in `report.json` or
console JSON summaries.

## Evaluation cohorts

Declared in `configs/sms_processing/evaluations/model-quality.json`:

1. **`synthetic`** (`tests/sms_processing/fixtures/extractor/synthetic-suite.jsonl`)
   - 100% invented messages covering normal, zero, incomplete, and conflicting
     advisory modes, Unicode scalar edge cases, and expected spans.
   - Intended for CI/local parity checks; does not measure real-world accuracy.
2. **`grandfathered`** (`DATA/extraction_ds.jsonl`)
   - Preserved 203-row legacy regression fixture (114 transactions, 89
     non-transactions; legacy date labels excluded).
   - Useful strictly as a non-regression baseline (`spans: null`, no
     account-resolution or receipt-time truth). Never train on it or cite it as a
     fresh test or production claim.
3. **`private-canonical`** (`PRIVATE_DATA/sms_processing`)
   - Reads `CURRENT.json` and encrypted `workbench-v2.sqlite3` via
     `SecureWorkbenchStore` + `KeyringKeyProvider`.
   - Accuracy metrics score only `submitted` / `adjudicated` human-gold labels;
     unlabeled rows contribute outcome and latency rates only.
   - Group metrics (`sender_family`, `template_family`) suppress private groups
     with fewer than `_SMALL_GROUP_MINIMUM = 5` rows.

## Scored metrics

`report.json` reports:

- **Prefilter**: `excluded_count`, `excluded_rate`, `avoided_model_calls`,
  `false_exclusions` (excluded rows whose gold decision is `posted`).
- **Classification**: `accuracy`, `confusion` matrix across `none`/`abstain`/
  `posted` gold vs `none`/`abstain`/`posted`/`malformed`/`runtime_failure`
  outcomes, `false_transactions`, `missed_transactions`.
- **Transaction**: `precision`, `recall`, `f1` for `posted` detection.
- **Strict posted exact success**: normalized `minor_units`, `currency`,
  `direction`, `account_reference`, `counterparty`, plus exact half-open
  Unicode-scalar spans when gold spans exist.
- **Per-field & span accuracy**: `amount_accuracy`, `direction_accuracy`,
  `account_reference_accuracy`, `counterparty_accuracy`, `exact_source_spans`.
- **Source grounding & failures**: `source_grounding` rejection counts
  (`extractor_evidence_*`, `extractor_amount_value_disagreement`,
  `extractor_account_reference_invalid`), `abstention` rates, `failures`
  (`malformed_count`, `runtime_failure_count`), and sanitized `reason_code_counts`.
- **Latency**: host `mean`, `p50`, `p95`, `p99`, `max` milliseconds per row.

## Running evaluations

```bash
# Synthetic GGUF parity smoke run
python scripts/run_sms_processing.py evaluate-extractor \
  --gguf MODELS/<model>.gguf \
  --suite synthetic \
  --output-dir PRIVATE_DATA/sms_processing/evaluations/synth-check

# Grandfathered 203-row regression check
python scripts/run_sms_processing.py evaluate-extractor \
  --gguf MODELS/<model>.gguf \
  --suite grandfathered \
  --output-dir PRIVATE_DATA/sms_processing/evaluations/grandfathered-check

# Private human-gold workbench check (on local host owning workbench-v2.sqlite3)
python scripts/run_sms_processing.py evaluate-extractor \
  --gguf MODELS/<model>.gguf \
  --suite private-canonical \
  --output-dir PRIVATE_DATA/sms_processing/evaluations/private-check \
  --account-catalog PRIVATE_DATA/sms_processing/account-catalog.json

# macOS Apple Foundation Models SDK lane (macOS Apple Silicon only)
python scripts/run_sms_processing.py evaluate-apple-fm \
  --suite synthetic \
  --output-dir PRIVATE_DATA/sms_processing/evaluations/apple-synth-check
```

## Legacy broad GGUF benchmark (`run_gguf_eval.py`)

`scripts/evaluate.sh` and `run_gguf_eval.py` run `lm_eval` against the legacy
`DATA/sms_extraction.yaml` prompt/filter task (`DATA/utils.py` +
`DATA/llamacpp_model.py`). Keep results from that exploratory slate distinct from
the unified `pocketfinancer.sms-extractor/1` evaluation lane above.
