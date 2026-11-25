"""Repository safety guard tests (tracked pathname policy only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_repo_safety import (
    GRANDFATHERED_PATHS,
    VERSIONED_PUBLIC_ARTIFACT_PATHS,
    Violation,
    find_violations,
    tracked_paths,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("private/messages.sqlite3", "database"),
        ("private/messages.db-wal", "database"),
        ("all_sms.csv", "tabular export"),
        ("exports/messages.json", "raw export"),
        ("DATA/another_dataset.jsonl", "raw export"),
        ("MODELS/demo.gguf", "model weight"),
        ("checkpoints/run-1/state.json", "checkpoint"),
        ("checkpoint-42/adapter.json", "checkpoint"),
        ("RESULTS/demo/samples_sms.jsonl", "result artifact"),
        ("logs/evaluation.log", "result artifact"),
        ("archive/error_analysis.txt", "result artifact"),
        ("archive/results_analysis.ipynb", "result artifact"),
        ("candidate.jsonl", "raw export"),
        ("scripts/example.py.orig", "patch backup"),
        ("scripts/example.py~", "patch backup"),
        ("pocketfinancer_v2_datasets.zip", "archive"),
        ("private/archive.tar.gz", "archive"),
        ("docs/.DS_Store", "operating-system metadata"),
        ("docs/Thumbs.db", "operating-system metadata"),
    ],
)
def test_private_and_generated_paths_are_rejected(path: str, category: str) -> None:
    assert find_violations([path]) == [Violation(path, category)]


@pytest.mark.parametrize(
    ("path", "tree"),
    [
        ("PRIVATE_DATA/sms_processing/split_manifest.jsonl", "PRIVATE_DATA"),
        ("PUBLIC_CANDIDATE/sms_processing/safe_preview.jsonl", "PUBLIC_CANDIDATE"),
        ("TRAINING_ARTIFACTS/sms_processing/seed-29/README.md", "TRAINING_ARTIFACTS"),
        ("MODELS/example-slm/tokenizer_config.json", "MODELS"),
        ("RESULTS/sms_processing/benchmarks/notes.md", "RESULTS"),
        ("UPSTREAM/llama.cpp/examples/example.py", "UPSTREAM"),
    ],
)
def test_innocuous_names_cannot_bypass_protected_top_level_trees(path: str, tree: str) -> None:
    assert find_violations([path]) == [Violation(path, f"protected output tree: {tree}")]


def test_protected_tree_names_are_exact_and_top_level_only() -> None:
    paths = ["models/runtime.py", "private_data/schema.py", "src/PRIVATE_DATA/schema.py"]
    assert find_violations(paths) == []


def test_safe_tracked_paths_and_grandfathered_exception_are_allowed() -> None:
    paths = [
        "DATA/extraction_ds.jsonl",
        "DATA/sms_extraction.yaml",
        "export_sms.py",
        "models/runtime.py",
    ]
    assert find_violations(paths) == []


def test_versioned_public_artifact_allowlists_are_exact_and_reject_near_misses() -> None:
    assert find_violations(sorted(VERSIONED_PUBLIC_ARTIFACT_PATHS)) == []

    near_misses = [
        "tests/sms_processing/golden/sms-vectors-copy.json",
        "tests/sms_processing/fixtures/extractor/synthetic-sms-copy.jsonl",
        "configs/sms_processing/schemas/sms-extractor-copy.json",
    ]
    assert find_violations(near_misses) == [
        Violation(near_misses[0], "raw export"),
        Violation(near_misses[1], "raw export"),
        Violation(near_misses[2], "raw export"),
    ]


def test_grandfather_exception_is_exact_and_case_sensitive() -> None:
    assert GRANDFATHERED_PATHS == frozenset({"DATA/extraction_ds.jsonl"})
    assert find_violations(["data/extraction_ds.jsonl"]) == [
        Violation("data/extraction_ds.jsonl", "raw export")
    ]


def test_removed_analysis_artifacts_are_rejected_at_root() -> None:
    assert find_violations(["error_analysis.txt", "results_analysis.ipynb"]) == [
        Violation("error_analysis.txt", "result artifact"),
        Violation("results_analysis.ipynb", "result artifact"),
    ]


def test_windows_separators_and_dot_prefixes_cannot_bypass_policy() -> None:
    assert find_violations([r".\RESULTS\demo\metrics.json"]) == [
        Violation("RESULTS/demo/metrics.json", "result artifact")
    ]


def test_live_tracked_repository_has_zero_safety_violations() -> None:
    paths = tracked_paths(ROOT)
    assert paths
    assert find_violations(paths) == []
    for allowed_path in VERSIONED_PUBLIC_ARTIFACT_PATHS:
        assert (ROOT / allowed_path).is_file(), allowed_path
