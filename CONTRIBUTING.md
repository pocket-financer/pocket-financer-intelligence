# Contributing to Pocket Financer Intelligence

Start by reading [AGENTS.md](AGENTS.md), [docs/README.md](docs/README.md), and
[scripts/README.md](scripts/README.md). Executable Python code and frozen JSON
schemas under `configs/sms_processing/` take precedence over prose.

## Development environments

### Lightweight CI-parity checks

Pure-Python contracts, prefilter logic, corpus tests, workbench tests, and
static checks require only `requirements-ci.txt` (Python 3.11) or the WSL
environment at `/home/tojinotzenin/pF_slm_selection/.venv/bin/python`:

```bash
python3.11 -m venv /tmp/pf-ci
source /tmp/pf-ci/bin/activate
python -m pip install -r requirements-ci.txt
python scripts/check_repo_safety.py
python scripts/sync_native_assets.py --check --manifest-only
python -m ruff check .
python -m ruff check --select E4,E7,E9,F src scripts tests
python -m pytest -q
```

### WSL2 GPU and local GGUF environment

GPU inference and GGUF evaluation use the native WSL2 `ext4` environment
bootstrapped by `scripts/setup_wsl.sh`. See
[docs/WSL_AND_LOCAL_SETUP.md](docs/WSL_AND_LOCAL_SETUP.md) for exact CUDA,
PyTorch, `llama-cpp-python`, and macOS Apple Foundation Models details.

## Making a change

1. Create a focused branch off `main` (or the active consolidation branch).
2. Inspect all affected contracts, schemas, tests, and native asset outputs
   before editing.
3. If you edit any file under `configs/sms_processing/` referenced by
   [scripts/sync_native_assets.py](scripts/sync_native_assets.py), run:
   ```bash
   python scripts/sync_native_assets.py --manifest-only
   python scripts/sync_native_assets.py --check --manifest-only
   ```
   and coordinate downstream native sync (`--target android` / `--target ios`)
   with the Android and iOS repositories.
4. Keep line endings strictly `LF` (`.editorconfig` and `.gitattributes`). Run
   `git add --renormalize .` before committing if editing from Windows.
5. Run the full verification gate in WSL:
   - `python scripts/check_repo_safety.py`
   - `python scripts/sync_native_assets.py --check --manifest-only`
   - `python -m ruff check .`
   - `python -m ruff check --select E4,E7,E9,F src scripts tests`
   - `shellcheck` on `git ls-files '*.sh'`
   - `python -m pytest -q`
   - `git diff --check`
6. Commit with a Conventional Commit message (`feat:`, `fix:`, `docs:`,
   `refactor:`, `test:`, `ci:`, `chore:`). Do not push directly to `main`.

## Data, privacy, and UI rules

- Never include real SMS text, sender addresses, account suffixes, reviewer
  notes, or row-level evaluation results in commits, tests, docs, issues, or
  pull requests.
- Tests and docs must use 100% synthetic/invented examples only.
- Run the local workbench strictly via `python scripts/run_sms_processing.py serve-workbench`,
  which binds to `127.0.0.1` with per-process token auth and SQLCipher
  encryption (`workbench-v2.sqlite3`). Never tunnel or screen-share private rows.
- For UI development, verify behavior through synthetic HTTP tests in
  [tests/sms_processing/test_workbench_web.py](tests/sms_processing/test_workbench_web.py)
  and asset tests in
  [tests/test_sms_workbench_assets.py](tests/test_sms_workbench_assets.py).

## Pull requests

Follow [.github/pull_request_template.md](.github/pull_request_template.md).
Report exact commands run, observed test/lint counts, contract impact, and any
unverified runtime or device lanes honestly.
