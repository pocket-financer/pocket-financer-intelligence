# Repository Instructions

This is the single canonical instruction file for humans and coding agents
working in `pocket-financer-intelligence`. Read it before modifying code,
schemas, tests, scripts, workflows, or documentation.

## Authority and scope

1. Executable Python code in `src/pocketfinancer_sms/`, schemas and profiles in
   `configs/sms_processing/`, and conformance vectors in
   `tests/sms_processing/golden/` are the authoritative specification. Prose
   must follow executable code and schemas.
2. `pocket-financer-intelligence` owns the shared SMS processing contracts,
   Python reference pipeline, corpus builder, encrypted annotation workbench,
   and Python evaluators.
3. Do not modify sibling repositories (`pocket-financer-android` or
   `pocket-financer-ios`) during intelligence-scoped tasks; inspect them
   read-only if verifying contract alignment and propagate changes explicitly
   via `scripts/sync_native_assets.py`.

## Working environment (WSL Ubuntu & Python 3.11)

- Run lightweight verification (safety, manifest, Ruff, ShellCheck, pytest, and
  aggregate checks) in Python 3.11 using either a `requirements-ci.txt` virtual
  environment or the workstation WSL Ubuntu environment at
  `/home/tojinotzenin/pF_slm_selection/.venv/bin/python` against
  `/mnt/d/Personal_Projects/pocket-financer/pocket-financer-intelligence`.
- CUDA/PyTorch/GGUF runtime shell scripts (`scripts/setup_wsl.sh`,
  `scripts/activate_wsl.sh`, `scripts/evaluate.sh`, `scripts/fetch_models.sh`)
  require a native WSL2 `ext4` checkout and intentionally fail closed under
  `/mnt/*`, containers, WSL1, or non-Linux hosts.
- Live Apple Foundation Models inference (`evaluate-apple-fm`) runs only on
  compatible macOS Apple Silicon hosts (`apple-fm-sdk`); non-macOS hosts run
  fake-provider unit tests only.
- Enforce Unix `LF` line endings across all text files and shell scripts
  (`.editorconfig` and `.gitattributes`). When editing from Windows, run
  `git add --renormalize .` before committing.

## Strict privacy and data-handling invariants

The following files and top-level directories are strictly local, ignored, and
must NEVER be committed, inspected, printed, logged, quoted, or modified
casually:

- `all_sms.json`, `all_sms.csv`, `sms.db`
- `PRIVATE_DATA/` (including `PRIVATE_DATA/sms_processing/` and any WSL-side
  `PRIVATE_DATA/` copy)
- `MODELS/` (including any WSL-side `MODELS/` copy)
- `RESULTS/`, `TRAINING_ARTIFACTS/`, `PUBLIC_CANDIDATE/`, `UPSTREAM/`

Mandatory rules at all times:

1. Never read, print, log, quote, or surface raw SMS bodies, sender IDs, phone
   numbers, account digits, notes, or per-row predictions from private files in
   terminal output, code, tests, docs, or PRs.
2. Aggregate counts and hashes only: when verifying local corpus regressions,
   emit integer totals and sanitized reason counts only—never message content.
3. Never send private messages or identifiers to hosted APIs, telemetry, cloud
   inference, or external trackers.
4. All unit tests, golden vectors, examples, and documentation must use 100%
   invented synthetic data.
5. `DATA/extraction_ds.jsonl` is a grandfathered 203-row regression fixture
   only. Never train on it, weaken safety rules around it, or cite it as a
   fresh test or production claim.
6. Local corpus, workbench, and evaluation tools never authorize publishing
   datasets, model weights, or app builds.

## Engineering and fine-tuning rules

- Keep pipeline, evaluation, and future fine-tuning logic strictly
  model-agnostic: model IDs, chat templates, quantization, and runtime limits
  belong in configuration rather than model-specific code paths.
- Future fine-tuning pipelines must consume committed (`submitted` or
  `adjudicated`) `CanonicalLabelV2` labels from the SQLCipher workbench via
  `project_extractor_target(...)` (excluding `uncertain=True` and
  `invalid_outgoing`), strictly preserving template-component-disjoint pool
  isolation.
- Never weaken repository safety guards (`scripts/check_repo_safety.py`),
  schema validation, Unicode-scalar grounding checks, 64-bit integer money
  bounds, or deterministic persistence/privacy gates.
- Whenever any asset under `configs/sms_processing/` referenced by
  `scripts/sync_native_assets.py` changes, rebuild
  `configs/sms_processing/release-manifest.json` with
  `python scripts/sync_native_assets.py --manifest-only` and coordinate
  downstream native sync with Android and iOS.

## Required verification gate

Run all checks via WSL Python 3.11 (and verify clean `git status` / `git diff --check`) before committing:

```bash
python scripts/check_repo_safety.py
python scripts/sync_native_assets.py --check --manifest-only
python -m ruff check .
python -m ruff check --select E4,E7,E9,F src scripts tests
shellcheck --external-sources --exclude=SC1090,SC1091,SC2086,SC2162,SC2317 -- $(git ls-files '*.sh')
python -m pytest -v
git diff --check
```

## Version control

- Work on short-lived branches off `main` named `<type>/<short-description>`
  (never commit or push directly to `main`).
- Use Conventional Commit subjects and PR titles (`feat:`, `fix:`, `docs:`,
  `test:`, `ci:`, `refactor:`, `chore:`).
- Never bypass failing safety, manifest, lint, ShellCheck, LF, or unit tests.
