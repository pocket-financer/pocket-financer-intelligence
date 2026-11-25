# Documentation Map

`pocket-financer-intelligence` owns PocketFinancer's shared SMS processing
contracts, Python reference pipeline, evaluation harness, encrypted local
annotation workbench, and cross-platform asset sync. Native app implementations
live in sibling repositories (`pocket-financer-android` and
`pocket-financer-ios`).

## Core documents

Read in order:

1. [Architecture](ARCHITECTURE.md) — unified `pocketfinancer.sms-pipeline/1`
   architecture, deterministic safety boundaries, cross-platform asset sync,
   and clean future model-agnostic fine-tuning path (`CanonicalLabelV2` ->
   `project_extractor_target`).
2. [Evaluation](EVALUATION.md) — `android_target_gguf`, `apple_fm_python`, and
   stateful mechanics evaluation lanes, cohort boundaries, and metrics.
3. [Workbench](WORKBENCH.md) — canonical corpus construction, blind-pool
   isolation, encrypted SQLCipher `workbench-v2.sqlite3`, loopback UI, and
   `CanonicalLabelV2` revision/export flow.
4. [WSL and local setup](WSL_AND_LOCAL_SETUP.md) — WSL Ubuntu `.venv` workflow,
   native WSL2 `ext4` CUDA environment, CI parity checks, and macOS Apple FM
   notes.

## Repository governance & command references

- [Repository overview](../README.md)
- [Agent instructions & privacy rules](../AGENTS.md)
- [Contribution workflow](../CONTRIBUTING.md)
- [Scripts & CLI command map](../scripts/README.md)
- [Release manifest](../configs/sms_processing/release-manifest.json)

## Status vocabulary

- **Implemented**: Code, schemas, and automated checks exist in this repository
  and pass in the stated verification lane.
- **Planned**: Designed contract or downstream work not yet implemented or not
  yet trained (such as future model-agnostic fine-tuning on `CanonicalLabelV2`).
- **Grandfathered**: Preserved regression fixture (`DATA/extraction_ds.jsonl`,
  203 rows) retained solely for non-production regression checks.
- **Unverified**: A runtime lane (for example physical Android/iOS device or
  Apple Silicon `apple-fm-sdk`) was not executed in the current check run.
