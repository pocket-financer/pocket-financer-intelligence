# Repository Development Instructions

This is the canonical guidance for humans and coding agents working in
`pocket-financer-intelligence`. Read this file before modifying code, schemas,
evaluation runners, workbench tools, or documentation.

## Authority and scope

1. Executable Python code in `src/pocketfinancer_sms/`, schemas and profiles in
   `configs/sms_processing/`, and conformance vectors in
   `tests/sms_processing/golden/` are the authoritative specification. Prose
   must follow executable code and schemas.
2. `pocket-financer-intelligence` owns the shared contracts, Python reference
   pipeline, corpus builder, encrypted annotation workbench, and Python
   evaluators.
3. Do not modify sibling repositories (`pocket-financer-android` or
   `pocket-financer-ios`) from intelligence-scoped tasks; propagate shared
   assets explicitly with `scripts/sync_native_assets.py`.

## Working environment (WSL Ubuntu)

- The shared WSL Ubuntu Python 3.11 environment lives at
  `/home/tojinotzenin/pF_slm_selection/.venv/bin/python`.
- Run repo safety checks, manifest checks, Ruff, ShellCheck, pytest, and
  aggregate prefilter/corpus checks from WSL against
  `/mnt/d/Personal_Projects/pocket-financer/pocket-financer-intelligence`:
  ```bash
  /home/tojinotzenin/pF_slm_selection/.venv/bin/python scripts/check_repo_safety.py
  /home/tojinotzenin/pF_slm_selection/.venv/bin/python scripts/sync_native_assets.py --check --manifest-only
  /home/tojinotzenin/pF_slm_selection/.venv/bin/python -m ruff check .
  /home/tojinotzenin/pF_slm_selection/.venv/bin/python -m ruff check --select E4,E7,E9,F src scripts tests
  /home/tojinotzenin/pF_slm_selection/.venv/bin/python -m pytest -q
  ```
- CUDA/PyTorch/GGUF runtime scripts (`scripts/setup_wsl.sh`,
  `scripts/activate_wsl.sh`, `scripts/evaluate.sh`, `scripts/fetch_models.sh`)
  require a native WSL2 `ext4` checkout (such as `/home/tojinotzenin/pF_slm_selection`)
  and intentionally fail closed under `/mnt/*`, Docker, WSL1, or non-Linux hosts.
- Apple Foundation Models live inference (`evaluate-apple-fm`) runs only on
  compatible macOS Apple Silicon hosts (`apple-fm-sdk`); non-macOS hosts run
  fake-provider unit tests only.
- Enforce Unix `LF` line endings across all text files and shell scripts
  (`.editorconfig` and `.gitattributes`). Run `git add --renormalize .` before
  committing when working across Windows and WSL.

## Strict privacy and data-handling rules

The following files and roots are strictly local and must NEVER be committed,
read into prompts/logs, or modified casually:

- `all_sms.json`, `all_sms.csv`, `sms.db`
- `PRIVATE_DATA/` (including `PRIVATE_DATA/sms_processing/` and WSL
  `/home/tojinotzenin/pF_slm_selection/PRIVATE_DATA/`)
- `MODELS/` (including WSL `/home/tojinotzenin/pF_slm_selection/MODELS/`)
- `RESULTS/`, `TRAINING_ARTIFACTS/`, `PUBLIC_CANDIDATE/`, `UPSTREAM/`

Rules that apply at all times:

1. Never print, log, quote, or inspect raw SMS bodies, sender IDs, phone
   numbers, account digits, notes, or per-row predictions from `all_sms.json`,
   `sms.db`, or `PRIVATE_DATA/` in terminal output, code, tests, docs, or PRs.
2. Aggregate counts and hashes only: when verifying corpus regressions (such as
   prefilter pass/exclude counts on `all_sms.json`), print integer totals and
   reason counts only—never message content or examples.
3. Never send private messages or identifiers to hosted APIs, telemetry, cloud
   inference, or remote experiment trackers.
4. All test fixtures, golden vectors, examples, and documentation must use 100%
   invented synthetic data.
5. `DATA/extraction_ds.jsonl` is a grandfathered 203-row regression fixture
   only. Never train on it or present it as a fresh test or production claim.
6. Local corpus, workbench, and evaluation commands never authorize publishing
   data, weights, or application builds.

## Engineering & future fine-tuning rules

- Keep pipeline, evaluation, and future fine-tuning logic strictly
  model-agnostic: model IDs, chat templates, quantization, and runtime limits
  belong in configuration rather than model-specific code sprawl.
- Legacy LFM2.5 fine-tuning, LoRA probe, and candidate-selector modules have
  been retired and removed. Any future fine-tuning pipeline must build cleanly
  from scratch on top of the annotated SQLCipher workbench corpus using
  `CanonicalLabelV2` -> `project_extractor_target(...)` (committed `submitted`
  or `adjudicated` labels, excluding `uncertain=True` and `invalid_outgoing`),
  strictly preserving template-component-disjoint pool isolation.
- Never silently weaken privacy guards, schema validation, Unicode-scalar
  grounding checks, 64-bit integer money bounds, or deterministic safety gates.
- Keep `configs/sms_processing/release-manifest.json` synchronized with
  `scripts/sync_native_assets.py --manifest-only` whenever any prompt, grammar,
  profile, or schema under `configs/sms_processing/` changes.

## Required checks before committing

Run all checks in WSL and verify clean git status:

```bash
python scripts/check_repo_safety.py
python scripts/sync_native_assets.py --check --manifest-only
python -m ruff check .
python -m ruff check --select E4,E7,E9,F src scripts tests
shellcheck --external-sources --exclude=SC1090,SC1091,SC2086,SC2162,SC2317 -- $(git ls-files '*.sh')
python -m pytest -q
git diff --check
```

## Version control

- Work on short-lived feature/consolidation branches (never commit or push
  directly to `main`).
- Use Conventional Commit subjects (`feat:`, `fix:`, `docs:`, `test:`, `ci:`,
  `refactor:`, `chore:`).
- Never bypass failing safety, lint, manifest, ShellCheck, or unit tests.
