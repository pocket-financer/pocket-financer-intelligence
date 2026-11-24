# Scripts and Command Map

## Active SMS processing and workbench CLI

`scripts/run_sms_processing.py` is the single CLI entry point for
`pocketfinancer_sms`. Every command prints aggregate-safe JSON only and keeps
private artifacts inside ignored `PRIVATE_DATA/sms_processing/`.

| Command | Purpose |
|---|---|
| `python scripts/run_sms_processing.py build-corpus` | Build or reuse the immutable canonical corpus run from `configs/sms_processing/profiles/archive-india-inr.json` |
| `python scripts/run_sms_processing.py prefilter --text "<sms>"` | Run `sms-prefilter/1` on a single string (or stdin) and print `{"decision","reason"}` |
| `python scripts/run_sms_processing.py init-workbench` | Import the `CURRENT.json` canonical manifest into encrypted SQLCipher `workbench-v2.sqlite3` |
| `python scripts/run_sms_processing.py serve-workbench [--port 8765]` | Serve the token-authenticated local workbench on `127.0.0.1` only |
| `python scripts/run_sms_processing.py backup-workbench` | Create and integrity-verify a mode-`0600` encrypted SQLCipher backup + `.manifest.json` |
| `python scripts/run_sms_processing.py export-workbench --selection-manifest <path> --consent-local-encrypted-export` | Export selected hash-bound submitted/adjudicated revisions inside `PRIVATE_DATA/sms_processing/` |
| `python scripts/run_sms_processing.py migrate-secure-workbench` | Migrate a legacy plaintext `workbench.sqlite3` to SQLCipher `workbench-v2.sqlite3` with encrypted recovery |
| `python scripts/run_sms_processing.py import-native-trace --bundle <path> [--key-id native-trace-import-v1]` | Import an AES-GCM encrypted Android or iOS trace bundle while rejecting blind-pool matches |
| `python scripts/run_sms_processing.py evaluate-extractor --gguf <local.gguf> --suite {synthetic,grandfathered,private-canonical} --output-dir PRIVATE_DATA/sms_processing/evaluations/<run>` | Evaluate the direct extractor (`android_target_gguf`) using GGUF + embedded chat template + optional GBNF |
| `python scripts/run_sms_processing.py evaluate-apple-fm --suite {synthetic,grandfathered,private-canonical} --output-dir PRIVATE_DATA/sms_processing/evaluations/<run>` | Evaluate the Apple Foundation Models guided flat lane (`apple_fm_python`) on compatible macOS |
| `python scripts/run_sms_processing.py sync-native-assets [--manifest-only] [--check]` | Wrapper around `scripts/sync_native_assets.py` |

## Contract manifest and native asset sync

`scripts/sync_native_assets.py` builds
`configs/sms_processing/release-manifest.json` with SHA-256 checksums across all
shared contracts, prompts, grammar, schemas, and profiles, and copies/verifies
them into sibling native repositories:

```bash
# Regenerate release-manifest.json after editing configs/sms_processing/
python scripts/sync_native_assets.py --manifest-only

# Verify release-manifest.json matches configs/sms_processing/ (CI check)
python scripts/sync_native_assets.py --check --manifest-only

# Sync assets + tests/sms_processing/golden/prefilter-vectors.json to Android or iOS
python scripts/sync_native_assets.py --target android
python scripts/sync_native_assets.py --target ios
python scripts/sync_native_assets.py --check
```

## Environment, GPU, and repository safety

| Script | Purpose |
|---|---|
| `python scripts/check_repo_safety.py` | Inspect `git ls-files -z` pathnames (without reading contents) and reject private/model/result artifacts |
| `bash scripts/setup_wsl.sh` | Bootstrap `.venv` on a native WSL2 `ext4` checkout with CUDA PyTorch, `llama-cpp-python`, and deps |
| `source scripts/activate_wsl.sh` | Activate the native WSL2 `ext4` environment and set `CUDA_HOME` and `HF_HOME` |
| `python scripts/verify_gpu.py` | Verify CUDA availability in PyTorch and test-load the smallest `.gguf` in `MODELS/` via `llama_cpp` |

## Legacy candidate-slate GGUF benchmark

These exploratory scripts benchmark candidate GGUF models against the
grandfathered 203-row `DATA/sms_extraction.yaml` task (`DATA/extraction_ds.jsonl`,
`DATA/utils.py`, `DATA/llamacpp_model.py`). They are separate from the unified
`evaluate-extractor` (`sms-extractor/1`) pipeline and must not be cited as
unified-pipeline or production evidence.

| Command | Purpose |
|---|---|
| `bash scripts/fetch_models.sh` | Idempotently download candidate GGUFs into ignored `MODELS/` |
| `bash scripts/evaluate.sh [--smoke-only\|--full-only\|--auto-full]` | Run the candidate GGUF benchmark slate via `run_gguf_eval.py` |
| `python run_gguf_eval.py --model <hf_id> --gguf <path.gguf> [--grammar <path.gbnf>] [--thinking auto\|on\|off]` | Run `lm_eval` `sms_extraction` for one GGUF checkpoint |

### Pipeline and run-isolation contract

`scripts/evaluate.sh` invokes `run_gguf_eval.py` in a **fresh Python subprocess**
per `(model, quantization, thinking_budget)` configuration. Isolating each run
in its own subprocess prevents `llama.cpp` CUDA context/KV-cache state, global
grammar objects, or tokenizer caches from leaking across candidate variants and
ensures each output directory under `RESULTS/llamacpp/<model_slug>/` captures
clean per-run metrics and sample artifacts.

## Local raw SMS archive utilities

Two root utilities handle local-only iOS backup extraction outside the active
SMS pipeline:

| Script | Purpose |
|---|---|
| `python find_sms_db.py` | Inspect the newest local backup subdirectory under `./` and copy its `sms.db` to `./sms.db` |
| `python export_sms.py [path/to/sms.db]` | Export `text` and `attributedBody` messages from `sms.db` to ignored `all_sms.csv` and `all_sms.json` |

Do not run `find_sms_db.py` in an uninspected directory, and never commit or
share `sms.db`, `all_sms.csv`, or `all_sms.json`.
