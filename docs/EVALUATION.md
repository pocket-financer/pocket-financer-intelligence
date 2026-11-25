# SMS Pipeline Evaluation Guide

`src/pocketfinancer_sms/evaluation.py` implements unified, private-safe,
checkpointed, and lock-protected evaluation over the host contracts declared in
[configs/sms_processing/evaluations/model-quality.json](../configs/sms_processing/evaluations/model-quality.json).

## Evaluation lanes

| Lane ID | Runner / Entry point | Wire transport & runtime | Notes |
|---|---|---|---|
| `android_target_gguf` | `evaluate-extractor --gguf <path.gguf>` | `direct_json` via `llama-cpp-python` (embedded GGUF chat template, greedy temp `0.0`, optional `sms-extractor.gbnf`) | Host WSL GGUF parity proxy; host latency is not Android phone latency |
| `apple_fm_python` | `evaluate-apple-fm` | `apple_fm_guided_flat` via `apple-fm-sdk` on compatible macOS Apple Silicon (`apple-fm-flat.schema.json` -> `project_apple_fm_flat_output`) | Live run requires macOS + Apple Intelligence; non-macOS hosts run fake-provider unit tests only |
| `synthetic-stateful-pipeline/1` | `SyntheticPipelineRun` (Python test suite) | `SuppliedRuntime` over isolated SQLite | Verifies admission, tombstones, 5-min duplicate window, confirmation/dismissal, concurrency, crash recovery, and 64-bit money bounds |

Every live evaluation run locks its output directory with `flock` (`run.lock`),
rejects outputs outside `PRIVATE_DATA/sms_processing/`, enforces `0700`/`0600`
permissions, checkpoints completed rows atomically, supports Ctrl+C
interruption/resume (CLI exit code `130`) when configuration and dataset
fingerprints match, and never echoes raw SMS bodies, prompts, or raw model
outputs in `report.json` or console summaries.

## Evaluation cohorts

Declared in `configs/sms_processing/evaluations/model-quality.json`:

1. **`synthetic`** (`tests/sms_processing/fixtures/extractor/synthetic-suite.jsonl`)
   - 100% invented messages covering `normal`, `zero`, `incomplete`, and
     `conflicting` advisory modes, Unicode-scalar edge cases, and expected spans.
   - Intended for CI/local parity checks; does not measure real-world accuracy.
2. **`grandfathered`** (`DATA/extraction_ds.jsonl`)
   - Preserved 203-row legacy regression fixture (legacy date labels excluded).
   - Non-regression baseline only (`spans: null`, no account-resolution or
     receipt-time truth). Never train on it or cite it as a fresh test or
     production claim.
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

Useful optional flags shared across evaluation commands:

- `--primary-currency INR` and repeatable `--profile <id>` (defaults: `core-en`, `india`)
- `--max-tokens 512` and `--no-prefilter`
- GGUF-only: `--n-ctx 4096`, `--n-gpu-layers -1`, `--seed 0`, `--no-grammar`
- Apple FM-only: `--locale en_US`

## Legacy candidate-slate GGUF benchmark

Three exploratory benchmark scripts evaluate candidate GGUF models against the
grandfathered 203-row `DATA/sms_extraction.yaml` `lm_eval` task (`DATA/utils.py`
and `DATA/llamacpp_model.py`). Keep results from this exploratory slate distinct
from the unified `pocketfinancer.sms-extractor/1` evaluation lane above, and
never cite them as unified-pipeline or production evidence.

| Command | Purpose |
|---|---|
| `bash scripts/fetch_models.sh` | Idempotently download candidate GGUFs into ignored `MODELS/` (requires native WSL2 `ext4` checkout) |
| `bash scripts/evaluate.sh [--smoke-only\|--full-only\|--auto-full]` | Sweep candidate models via `run_gguf_eval.py` (defaults to smoke 10, prompt, then full 203) |
| `python run_gguf_eval.py --model <hf_id> --gguf <path.gguf> [--grammar <path.gbnf>] [--thinking auto\|on\|off]` | Run `lm_eval` `sms_extraction` for one GGUF checkpoint |

### Subprocess run-isolation contract

`scripts/evaluate.sh` invokes `run_gguf_eval.py` in a **fresh Python subprocess**
per `(model, quantization, thinking_budget)` variant. Isolating each variant in
its own process prevents `llama.cpp` CUDA context/KV-cache state, grammar
objects, or tokenizer caches from leaking across runs and guarantees clean
per-run metrics under ignored `RESULTS/llamacpp/<model_slug>/`.
