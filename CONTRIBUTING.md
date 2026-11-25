# Contributing to Pocket Financer Intelligence

Read [AGENTS.md](AGENTS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
before making changes. Executable Python code in `src/pocketfinancer_sms/`,
schemas and profiles in `configs/sms_processing/`, and golden vectors in
`tests/sms_processing/golden/` are the authoritative specification.

## Development environments

### Lightweight CI-parity checks

Pure-Python contracts, prefilter logic, analyzer, corpus, evaluation, workbench,
and static checks require only Python 3.11 and `requirements-ci.txt` (or the
local WSL environment documented in
[docs/WSL_AND_LOCAL_SETUP.md](docs/WSL_AND_LOCAL_SETUP.md)):

```bash
python3.11 -m venv /tmp/pf-ci
source /tmp/pf-ci/bin/activate
python -m pip install -r requirements-ci.txt
```

### WSL2 GPU and local GGUF environment

GPU inference and GGUF evaluation require a native WSL2 `ext4` checkout
bootstrapped by `scripts/setup_wsl.sh`. See
[docs/WSL_AND_LOCAL_SETUP.md](docs/WSL_AND_LOCAL_SETUP.md) for CUDA PyTorch,
`llama-cpp-python`, and optional macOS Apple Foundation Models setup.

## Making a change

1. Create a focused branch off `main` using `<type>/<short-description>`. Never
   commit or push directly to `main`.
2. Inspect affected contracts, schemas, tests, and native asset outputs before
   editing.
3. If you edit any file under `configs/sms_processing/` tracked by
   [scripts/sync_native_assets.py](scripts/sync_native_assets.py), regenerate
   and verify the release manifest:
   ```bash
   python scripts/sync_native_assets.py --manifest-only
   python scripts/sync_native_assets.py --check --manifest-only
   ```
   Then coordinate downstream native sync (`--target android` / `--target ios`)
   with [pocket-financer-android](https://github.com/pocket-financer/pocket-financer-android)
   and [pocket-financer-ios](https://github.com/pocket-financer/pocket-financer-ios).
4. Keep line endings strictly Unix `LF` (`.editorconfig` and `.gitattributes`).
   Run `git add --renormalize .` before committing when editing from Windows.
5. Run the full required verification gate listed in
   [AGENTS.md](AGENTS.md#required-verification-gate) and confirm every check
   passes.
6. Use Conventional Commit subjects (`feat:`, `fix:`, `docs:`, `refactor:`,
   `test:`, `ci:`, `chore:`).

## Data, privacy, and UI rules

- Never include real SMS text, sender addresses, account suffixes, reviewer
  notes, or per-row evaluation results in commits, tests, docs, issues, or PRs.
- All unit tests, fixtures, and documentation examples must use 100% synthetic
  invented data.
- Run the local workbench strictly through
  `python scripts/run_sms_processing.py serve-workbench`, which binds only to
  `127.0.0.1` with per-process token auth and SQLCipher storage
  (`workbench-v2.sqlite3`). Never tunnel or screen-share private rows.
- Verify workbench HTTP endpoints, security headers, and static UI asset
  contracts via
  [tests/sms_processing/test_workbench_web.py](tests/sms_processing/test_workbench_web.py).

## Pull requests

Follow [.github/pull_request_template.md](.github/pull_request_template.md).
Report exact commands run, observed test/lint counts, contract impact, and any
unverified runtime or device lanes honestly.
