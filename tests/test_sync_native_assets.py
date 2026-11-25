"""Tests for release-manifest generation and cross-repo native asset synchronization."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts import sync_native_assets


ROOT = Path(__file__).resolve().parents[1]


def _make_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    intel = workspace / "pocket-financer-intelligence"
    android = workspace / "pocket-financer-android"
    ios = workspace / "pocket-financer-ios"
    shutil.copytree(ROOT / "configs" / "sms_processing", intel / "configs" / "sms_processing")
    golden_dest = intel / "tests" / "sms_processing" / "golden"
    golden_dest.mkdir(parents=True)
    shutil.copy2(
        ROOT / "tests" / "sms_processing" / "golden" / "prefilter-vectors.json",
        golden_dest / "prefilter-vectors.json",
    )
    android.mkdir(parents=True)
    ios.mkdir(parents=True)
    return intel, android, ios


def test_canonical_manifest_check_passes_for_repository() -> None:
    assert sync_native_assets.sync(ROOT, check=True, manifest_only=True) == 0
    assert (
        sync_native_assets.main(["--repo-root", str(ROOT), "--check", "--manifest-only"]) == 0
    )


def test_sync_all_targets_writes_clean_trees_and_verifies_check_mode(tmp_path: Path) -> None:
    intel, android, ios = _make_workspace(tmp_path)
    android_assets = android / "pipeline" / "src" / "main" / "assets" / "sms_processing"
    android_vectors = android / "pipeline" / "src" / "test" / "resources" / "prefilter-vectors.json"
    ios_assets = ios / "PocketFinancer" / "Resources" / "SmsProcessing"
    ios_vectors = ios / "PocketFinancerTests" / "Fixtures" / "prefilter-vectors.json"

    # Missing destination trees fail closed under --check.
    assert sync_native_assets.sync(intel, check=True, target="all") == 1

    # Sync writes manifest + Android + iOS trees + prefilter vectors.
    assert sync_native_assets.sync(intel, check=False, target="all") == 0
    assert sync_native_assets.sync(intel, check=True, target="all") == 0
    assert (android_assets / "release-manifest.json").is_file()
    assert (android_assets / "grammars" / "sms-extractor.gbnf").is_file()
    assert not (android_assets / "schemas" / "apple-fm-flat.schema.json").exists()
    assert (ios_assets / "release-manifest.json").is_file()
    assert (ios_assets / "schemas" / "apple-fm-flat.schema.json").is_file()
    assert not (ios_assets / "grammars" / "sms-extractor.gbnf").exists()
    assert android_vectors.is_file()
    assert ios_vectors.is_file()


def test_sync_detects_manifest_drift_stale_files_content_diff_and_vector_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    intel, android, ios = _make_workspace(tmp_path)
    assert sync_native_assets.sync(intel, check=False, target="all") == 0

    android_assets = android / "pipeline" / "src" / "main" / "assets" / "sms_processing"
    ios_vectors = ios / "PocketFinancerTests" / "Fixtures" / "prefilter-vectors.json"

    # Stale extra file in Android tree fails check, and re-sync removes it.
    stale_file = android_assets / "stale-legacy.json"
    stale_file.write_text("{}\n", encoding="utf-8")
    assert sync_native_assets.sync(intel, check=True, target="android") == 1
    assert "stale/extra files" in capsys.readouterr().err
    assert sync_native_assets.sync(intel, check=False, target="android") == 0
    assert not stale_file.exists()

    # Modified asset content fails check.
    prompt_copy = android_assets / "prompts" / "sms-extractor.txt"
    prompt_copy.write_text("tampered\n", encoding="utf-8")
    assert sync_native_assets.sync(intel, check=True, target="android") == 1
    assert "content differs" in capsys.readouterr().err
    assert sync_native_assets.sync(intel, check=False, target="android") == 0

    # Modified prefilter vectors fail check.
    ios_vectors.write_text("{}\n", encoding="utf-8")
    assert sync_native_assets.sync(intel, check=True, target="ios") == 1
    assert "fixture differs" in capsys.readouterr().err
    assert sync_native_assets.sync(intel, check=False, target="ios") == 0

    # Out-of-date manifest fails --manifest-only --check and is rebuilt cleanly.
    manifest_path = intel / "configs" / "sms_processing" / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["duplicate_window_ms"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert sync_native_assets.sync(intel, check=True, manifest_only=True) == 1
    assert "out of date" in capsys.readouterr().err
    assert sync_native_assets.sync(intel, check=False, manifest_only=True) == 0
    assert sync_native_assets.sync(intel, check=True, manifest_only=True) == 0


def test_sync_normalizes_crlf_bytes_and_rejects_unsupported_target(tmp_path: Path) -> None:
    intel, _android, _ios = _make_workspace(tmp_path)
    prompt = intel / "configs" / "sms_processing" / "prompts" / "sms-extractor.txt"
    lf_data = prompt.read_bytes().replace(b"\r\n", b"\n")
    prompt.write_bytes(lf_data.replace(b"\n", b"\r\n"))

    # Manifest hashing normalizes CRLF -> LF, so hashes remain stable.
    assert sync_native_assets.sync(intel, check=True, manifest_only=True) == 0
    with pytest.raises(ValueError, match="unsupported target"):
        sync_native_assets.sync(intel, target="web")
