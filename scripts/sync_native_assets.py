#!/usr/bin/env python3
"""Build the release manifest and sync/verify clean SMS assets across repos."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


MANIFEST_CONTRACT = "pocketfinancer.contract-release-manifest/1"
RELEASE_ID = "pocketfinancer.sms-pipeline/1"
DUPLICATE_WINDOW_MS = 300_000

COMMON_SCHEMAS = (
    "schemas/sms-extractor.schema.json",
    "schemas/sms-extractor-input.schema.json",
    "schemas/sms-analysis.schema.json",
    "schemas/processing-config.schema.json",
    "schemas/processing-result.schema.json",
    "schemas/processing-trace.schema.json",
    "schemas/review-case.schema.json",
    "schemas/user-feedback.schema.json",
)
COMMON_PROFILES = (
    "profiles/currency.json",
    "profiles/account-resolution.json",
    "profiles/extractor-validation.json",
    "profiles/reason-codes.json",
    "profiles/core-en.json",
    "profiles/india.json",
    "profiles/persistence-policy.json",
)
ANDROID_ASSETS = (
    "prompts/sms-extractor.txt",
    "grammars/sms-extractor.gbnf",
    *COMMON_SCHEMAS,
    *COMMON_PROFILES,
)
IOS_ASSETS = (
    "prompts/apple-fm-sms-extractor.txt",
    "schemas/apple-fm-flat.schema.json",
    *COMMON_SCHEMAS,
    *COMMON_PROFILES,
)
MANIFEST_ARTIFACTS = tuple(dict.fromkeys((*ANDROID_ASSETS, *IOS_ASSETS)))
NON_JSON_CONTRACTS = {
    "prompts/sms-extractor.txt": "pocketfinancer.sms-extractor-prompt/1",
    "prompts/apple-fm-sms-extractor.txt": "pocketfinancer.apple-fm-sms-extractor-prompt/1",
    "grammars/sms-extractor.gbnf": "pocketfinancer.sms-extractor-grammar/1",
}


def _lf_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_contract(rel_path: str, data: bytes) -> str:
    if rel_path in NON_JSON_CONTRACTS:
        return NON_JSON_CONTRACTS[rel_path]
    payload = json.loads(data.decode("utf-8"))
    if rel_path == "schemas/apple-fm-flat.schema.json":
        return str(payload.get("$id") or "pocketfinancer.apple-fm-flat/1")
    contract = payload.get("contract") or payload.get("$id")
    if not isinstance(contract, str) or not contract:
        raise ValueError(f"missing contract identifier in {rel_path}")
    return contract


def build_release_manifest(repo_root: Path) -> dict[str, Any]:
    """Build the single release-manifest document from configs/sms_processing."""
    sms_root = repo_root / "configs" / "sms_processing"
    artifacts = []
    for rel_path in sorted(MANIFEST_ARTIFACTS):
        data = _lf_bytes(sms_root / rel_path)
        artifacts.append(
            {
                "path": rel_path,
                "contract": _artifact_contract(rel_path, data),
                "sha256": _sha256_bytes(data),
            }
        )
    return {
        "contract": MANIFEST_CONTRACT,
        "release_id": RELEASE_ID,
        "duplicate_window_ms": DUPLICATE_WINDOW_MS,
        "routing_policy": {
            "policy_contract": "pocketfinancer.persistence-policy/1",
            "processing_config_contract": RELEASE_ID,
            "review_contract": "pocketfinancer.review-case/2",
            "processing_result_contract": "pocketfinancer.processing-result/3",
            "complete_posted": "transactions",
            "exception": "review",
            "duplicate": "review",
            "valid_none": "tombstoned_none",
            "prefilter_excluded": "tombstoned_excluded",
            "transaction_write": "atomic_with_operation_settlement",
            "retry": "new_operation_with_parent_operation_id",
        },
        "runtime_policy": {
            "generation_mode": "DIRECT_NON_THINKING",
            "decoding": "greedy",
            "answer_token_limit": 512,
            "raw_output_utf8_byte_limit": 16384,
            "parser_deadline_ms": 0,
            "claim_lease_ms": 120000,
            "claim_heartbeat_ms": 15000,
            "automatic_retry_limit": 3,
        },
        "artifacts": artifacts,
    }


def _manifest_bytes(repo_root: Path) -> bytes:
    manifest = build_release_manifest(repo_root)
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _sync_tree(
    expected_files: dict[str, bytes],
    dest_dir: Path,
    *,
    check: bool,
    errors: list[str],
) -> None:
    if check:
        if not dest_dir.is_dir():
            errors.append(f"missing asset directory: {dest_dir}")
            return
        actual_paths = {
            path.relative_to(dest_dir).as_posix()
            for path in dest_dir.rglob("*")
            if path.is_file()
        }
        missing = sorted(set(expected_files) - actual_paths)
        extra = sorted(actual_paths - set(expected_files))
        if missing:
            errors.append(f"{dest_dir}: missing files: {', '.join(missing)}")
        if extra:
            errors.append(f"{dest_dir}: stale/extra files: {', '.join(extra)}")
        for rel_path, expected in sorted(expected_files.items()):
            target = dest_dir / rel_path
            if target.is_file() and _lf_bytes(target) != expected:
                errors.append(f"{target}: content differs from canonical intelligence asset")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    for existing in list(dest_dir.rglob("*")):
        if existing.is_file():
            existing.unlink()
    for existing_dir in sorted(
        (path for path in dest_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if not any(existing_dir.iterdir()):
            existing_dir.rmdir()
    for rel_path, content in sorted(expected_files.items()):
        target = dest_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(content)


def _sync_single_file(
    expected: bytes,
    target: Path,
    *,
    check: bool,
    errors: list[str],
) -> None:
    if check:
        if not target.is_file():
            errors.append(f"missing fixture file: {target}")
        elif _lf_bytes(target) != expected:
            errors.append(f"{target}: fixture differs from canonical prefilter-vectors.json")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(expected)


def sync(
    repo_root: Path,
    *,
    check: bool = False,
    manifest_only: bool = False,
    target: str = "all",
) -> int:
    """Build/check release-manifest.json and optionally sync/check Android & iOS copies."""
    if target not in {"all", "android", "ios"}:
        raise ValueError(f"unsupported target: {target!r}")
    resolved_root = Path(repo_root).resolve()
    sms_root = resolved_root / "configs" / "sms_processing"
    manifest_path = sms_root / "release-manifest.json"
    manifest_raw = _manifest_bytes(resolved_root)
    errors: list[str] = []

    if check:
        if not manifest_path.is_file():
            errors.append(f"missing release manifest: {manifest_path}")
        elif _lf_bytes(manifest_path) != manifest_raw:
            errors.append(f"{manifest_path}: out of date with configs/sms_processing artifacts")
    elif target == "all":
        with manifest_path.open("wb") as handle:
            handle.write(manifest_raw)

    if not manifest_only:
        workspace_root = resolved_root.parent
        prefilter_bytes = _lf_bytes(
            resolved_root / "tests" / "sms_processing" / "golden" / "prefilter-vectors.json"
        )
        if target in {"all", "android"}:
            android_files = {"release-manifest.json": manifest_raw}
            for rel_path in ANDROID_ASSETS:
                android_files[rel_path] = _lf_bytes(sms_root / rel_path)
            android_repo = workspace_root / "pocket-financer-android"
            _sync_tree(
                android_files,
                android_repo / "pipeline" / "src" / "main" / "assets" / "sms_processing",
                check=check,
                errors=errors,
            )
            _sync_single_file(
                prefilter_bytes,
                android_repo / "pipeline" / "src" / "test" / "resources" / "prefilter-vectors.json",
                check=check,
                errors=errors,
            )
        if target in {"all", "ios"}:
            ios_files = {"release-manifest.json": manifest_raw}
            for rel_path in IOS_ASSETS:
                ios_files[rel_path] = _lf_bytes(sms_root / rel_path)
            ios_repo = workspace_root / "pocket-financer-ios"
            _sync_tree(
                ios_files,
                ios_repo / "PocketFinancer" / "Resources" / "SmsProcessing",
                check=check,
                errors=errors,
            )
            _sync_single_file(
                prefilter_bytes,
                ios_repo / "PocketFinancerTests" / "Fixtures" / "prefilter-vectors.json",
                check=check,
                errors=errors,
            )

    if errors:
        for message in errors:
            print(f"sync_native_assets error: {message}", file=sys.stderr)
        return 1
    mode = "verified" if check else "synced"
    scope = "manifest" if manifest_only else f"manifest + {target} assets"
    print(f"sync_native_assets: {mode} ({scope}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build/verify release-manifest.json and sync assets across Android & iOS."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--target", choices=("all", "android", "ios"), default="all")
    args = parser.parse_args(argv)
    return sync(
        args.repo_root,
        check=args.check,
        manifest_only=args.manifest_only,
        target=args.target,
    )


if __name__ == "__main__":
    raise SystemExit(main())
