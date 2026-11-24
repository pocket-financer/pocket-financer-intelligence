import pytest

from scripts.check_repo_safety import Violation, find_violations


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
        ("RESULTS/demo/samples_sms.jsonl", "result artifact"),
        ("logs/evaluation.log", "result artifact"),
        ("archive/error_analysis.txt", "result artifact"),
        ("archive/results_analysis.ipynb", "result artifact"),
        ("candidate.jsonl", "raw export"),
        ("scripts/example.py.orig", "patch backup"),
        ("pocketfinancer_v2_datasets.zip", "archive"),
        ("private/archive.tar.gz", "archive"),
        ("docs/.DS_Store", "operating-system metadata"),
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


def test_safe_tracked_paths_and_fixture_exception_are_allowed() -> None:
    paths = [
        "DATA/extraction_ds.jsonl",
        "DATA/sms_extraction.yaml",
        "export_sms.py",
        "models/runtime.py",
    ]

    assert find_violations(paths) == []


def test_versioned_candidate_protocol_artifacts_have_exact_exceptions() -> None:
    paths = [
        "tests/sms_processing/fixtures/extractor/synthetic-suite.jsonl",
        "tests/sms_processing/golden/admission-v1.json",
        "tests/sms_processing/golden/extractor-v1/sanitized-vectors.json",
        "tests/sms_processing/golden/prefilter-vectors.json",
    ]

    assert find_violations(paths) == []

    near_misses = [
        "tests/sms_processing/golden/sms-vectors-copy.json",
        "tests/sms_processing/fixtures/extractor/synthetic-sms-copy.jsonl",
    ]

    assert find_violations(near_misses) == [
        Violation(near_misses[0], "raw export"),
        Violation(near_misses[1], "raw export"),
    ]


def test_sms_processing_source_contracts_have_exact_exceptions() -> None:
    paths = [
        "configs/sms_processing/evaluations/model-quality.json",
        "configs/sms_processing/profiles/account-resolution.json",
        "configs/sms_processing/profiles/archive-india-inr.json",
        "configs/sms_processing/profiles/core-en.json",
        "configs/sms_processing/profiles/currency.json",
        "configs/sms_processing/profiles/extractor-validation.json",
        "configs/sms_processing/profiles/india.json",
        "configs/sms_processing/profiles/persistence-policy.json",
        "configs/sms_processing/profiles/reason-codes.json",
        "configs/sms_processing/release-manifest.json",
        "configs/sms_processing/schemas/apple-fm-flat.schema.json",
        "configs/sms_processing/schemas/canonical-label.schema.json",
        "configs/sms_processing/schemas/corpus-record.schema.json",
        "configs/sms_processing/schemas/native-trace-bundle.schema.json",
        "configs/sms_processing/schemas/processing-config.schema.json",
        "configs/sms_processing/schemas/processing-result.schema.json",
        "configs/sms_processing/schemas/processing-trace.schema.json",
        "configs/sms_processing/schemas/release-manifest.schema.json",
        "configs/sms_processing/schemas/review-case.schema.json",
        "configs/sms_processing/schemas/sms-analysis.schema.json",
        "configs/sms_processing/schemas/sms-extractor-input.schema.json",
        "configs/sms_processing/schemas/sms-extractor.schema.json",
        "configs/sms_processing/schemas/user-feedback.schema.json",
    ]

    assert find_violations(paths) == []
    near_miss = "configs/sms_processing/schemas/sms-extractor-copy.json"
    assert find_violations([near_miss]) == [Violation(near_miss, "raw export")]


def test_grandfather_exception_is_exact() -> None:
    assert find_violations(["data/extraction_ds.jsonl"]) == [
        Violation("data/extraction_ds.jsonl", "raw export")
    ]


def test_removed_analysis_artifacts_are_rejected_at_root() -> None:
    assert find_violations(["error_analysis.txt", "results_analysis.ipynb"]) == [
        Violation("error_analysis.txt", "result artifact"),
        Violation("results_analysis.ipynb", "result artifact"),
    ]


def test_windows_separators_cannot_bypass_policy() -> None:
    assert find_violations([r"RESULTS\demo\metrics.json"]) == [
        Violation("RESULTS/demo/metrics.json", "result artifact")
    ]
