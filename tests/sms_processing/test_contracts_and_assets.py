"""Contract bundle, release manifest, and sync script verification."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from scripts import sync_native_assets


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs" / "sms_processing"


def test_release_manifest_and_sync_check_match_repository_assets() -> None:
    manifest = json.loads((CONFIGS / "release-manifest.json").read_text(encoding="utf-8"))
    schema = json.loads((CONFIGS / "schemas" / "release-manifest.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(manifest, schema)

    assert sync_native_assets.sync(ROOT, check=True, manifest_only=True) == 0


def test_all_manifest_artifacts_exist_and_have_nonempty_contents() -> None:
    manifest = json.loads((CONFIGS / "release-manifest.json").read_text(encoding="utf-8"))
    paths = {item["path"] for item in manifest["artifacts"]}
    assert "prompts/sms-extractor.txt" in paths
    assert "prompts/apple-fm-sms-extractor.txt" in paths
    assert "grammars/sms-extractor.gbnf" in paths
    assert "schemas/apple-fm-flat.schema.json" in paths
    assert "profiles/persistence-policy.json" in paths
    for rel_path in paths:
        target = CONFIGS / rel_path
        assert target.is_file(), rel_path
        assert target.read_bytes().strip(), rel_path
