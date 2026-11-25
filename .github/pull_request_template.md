## Summary

<!-- What engineering or research outcome does this PR deliver? -->

## Scope and end-to-end behavior

<!-- Identify affected contract, data, evaluation, workbench, CI, or runtime
paths. State what is deliberately unchanged. -->

## Cross-repo and contract alignment

<!-- Note any impact on `release-manifest.json`, `pocket-financer-android`,
`pocket-financer-ios`, `prefilter-vectors.json`, dataset roles, or runtime
parity. Write "Not applicable" when unchanged. -->

## Verification

<!-- List exact commands and observed results. Distinguish lightweight CI/WSL,
CUDA/GGUF, macOS Apple FM, and physical Android/iOS device checks. -->

- [ ] `python scripts/check_repo_safety.py` passes
- [ ] `python scripts/sync_native_assets.py --check --manifest-only` passes
- [ ] `python -m ruff check .` and `python -m ruff check --select E4,E7,E9,F src scripts tests` pass
- [ ] ShellCheck passes on tracked `*.sh` files
- [ ] `python -m pytest -v` passes
- [ ] `git diff --check` is clean and all edited files use Unix `LF` endings
- [ ] Runtime/device verification is complete, or its gap is documented honestly

## Privacy, reproducibility, and compatibility

<!-- Cover private data boundaries, synthetic-only fixtures, seeds/hashes/provenance,
schema/output compatibility, and rollback implications. -->

## Evidence

<!-- Aggregate metrics, non-sensitive logs, or links to reports. Never paste raw
SMS, identifiers, private per-row output, credentials, or model weights. -->

## Checklist

- [ ] I read `AGENTS.md` and verified all prose against executable code and schemas
- [ ] This change is focused and includes zero generated or private artifacts
- [ ] The PR title uses a Conventional Commit prefix
- [ ] The grandfathered 203-row fixture (`DATA/extraction_ds.jsonl`) is not presented as a fresh test
- [ ] Host GGUF, Apple FM, Android-device, and iOS-device evidence are labeled truthfully
- [ ] No model, dataset, or application publication is implied by this PR
