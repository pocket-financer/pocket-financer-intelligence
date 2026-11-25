# WSL and Local Setup Guide

## 1. WSL Ubuntu Python 3.11 environment (Primary verification & local ML)

The repository uses a prepared WSL2 Ubuntu Python 3.11 environment at:

```text
/home/tojinotzenin/pF_slm_selection/.venv/bin/python
```

### Running checks against the Windows workspace checkout

From WSL Ubuntu, run static checks, unit tests, manifest verification, and
aggregate prefilter/corpus scripts directly on
`/mnt/d/Personal_Projects/pocket-financer/pocket-financer-intelligence`:

```bash
cd /mnt/d/Personal_Projects/pocket-financer/pocket-financer-intelligence
PY=/home/tojinotzenin/pF_slm_selection/.venv/bin/python

"$PY" scripts/check_repo_safety.py
"$PY" scripts/sync_native_assets.py --check --manifest-only
"$PY" -m ruff check .
"$PY" -m ruff check --select E4,E7,E9,F src scripts tests
/home/tojinotzenin/pF_slm_selection/.venv/bin/shellcheck \
  --external-sources --exclude=SC1090,SC1091,SC2086,SC2162,SC2317 \
  -- $(git ls-files '*.sh')
"$PY" -m pytest -q
```

Note: When running `pytest` under `/mnt/d/...`, `test_activation_can_be_sourced_from_native_ext4_checkout`
in `tests/test_wsl_guards.py` skips cleanly because `.venv` lives in the
native WSL2 `ext4` checkout rather than `/mnt/d/`.

### Native WSL2 `ext4` setup (`setup_wsl.sh` & `activate_wsl.sh`)

CUDA PyTorch, `llama-cpp-python` (cu124), model caches (`HF_HOME`), and heavy
GGUF evaluations require a native WSL2 `ext4` checkout (for example
`/home/tojinotzenin/pF_slm_selection`):

1. `scripts/setup_wsl.sh` and `scripts/activate_wsl.sh` enforce
   `require_native_wsl2_ext4`: they check `/proc/sys/kernel/osrelease` for
   `WSL2`, reject `/.dockerenv` / `/run/.containerenv` containers, reject
   `/mnt/*` paths, and verify `findmnt` reports `ext4`.
2. `scripts/setup_wsl.sh` installs pinned dependencies via `uv`:
   - `torch==2.6.0` from PyTorch's `cu124` index
   - `llama-cpp-python==0.3.20` from the `cu124` wheel index
   - `requirements.txt` (`transformers`, `accelerate`, `huggingface_hub`, `lm_eval[gguf]`)
   - `requirements-training.txt` (`datasets`, `peft`, `trl`, `bitsandbytes`, `sentencepiece`, `rapidfuzz`)
3. `scripts/activate_wsl.sh` exports `CUDA_HOME`, sets `HF_HOME` to
   `$REPO_ROOT/.hf_cache`, and activates `.venv`.
4. Verify GPU offload with:
   ```bash
   source scripts/activate_wsl.sh
   python scripts/verify_gpu.py
   ```

## 2. Lightweight CI environment

GitHub Actions (`.github/workflows/ci.yml`) runs on `ubuntu-latest` with Python
3.11 and `requirements-ci.txt` (`pytest`, `ruff`, `shellcheck-py`, `rapidfuzz`,
`jsonschema`)—intentionally excluding PyTorch, CUDA, model downloads, and
private files.

## 3. Optional macOS Apple Foundation Models lane

On Apple Silicon macOS hosts running compatible Apple Intelligence / Xcode
releases, install `requirements-apple-fm.txt` (`apple-fm-sdk==0.2.1`) to enable
live `python scripts/run_sms_processing.py evaluate-apple-fm` runs. On WSL or
Linux, `AppleFMProvider` refuses live execution cleanly and tests use fake
providers.

## 4. Line-ending hygiene across Windows and WSL

`.editorconfig` and `.gitattributes` enforce Unix `LF` (`eol=lf`) across `.sh`,
`.py`, `.json`, `.jsonl`, `.txt`, `.gbnf`, `.md`, `.yml`, and `.yaml` files.
WSL Bash and `tests/test_wsl_guards.py` fail if `.sh` files have `CRLF`. Before
committing from Windows, run:

```bash
git config core.autocrlf false
git add --renormalize .
git diff --check
```
