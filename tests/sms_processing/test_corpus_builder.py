"""Synthetic-only corpus grouping, template-component pool isolation, leakage, and build tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pocketfinancer_sms import cli
from pocketfinancer_sms.corpus.grouping import (
    build_grouping,
    canonical_exact_body,
    normalized_template,
    sender_family,
)
from pocketfinancer_sms.corpus.manifest import build_private_corpus
from pocketfinancer_sms.corpus.pools import PoolInput, assign_pools, leakage_audit
from pocketfinancer_sms.provenance import PrivateArtifactError


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_jsonl(path: Path, values) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def _synthetic_repo(tmp_path: Path) -> tuple[Path, dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("PRIVATE_DATA/\n*.json\n", encoding="utf-8")
    source = [
        {
            "id": 1,
            "date": "2024-01-01T10:00:00Z",
            "sender": "SYNTH-BANK",
            "text": "INR 10 was debited from account **1111 at STORE.",
            "is_from_me": False,
            "service": "SMS",
        },
        {
            "id": 2,
            "date": "2024-01-02T10:00:00Z",
            "sender": "SYNTH-BANK",
            "text": "123456 is your OTP for login.",
            "is_from_me": False,
            "service": "SMS",
        },
        {
            "id": 3,
            "date": "2026-01-02T10:00:00Z",
            "sender": "SYNTH-BANK",
            "text": "EUR 4 was credited to account **2222 from FRIEND.",
            "is_from_me": False,
            "service": "SMS",
        },
        {
            "id": 4,
            "date": "2024-01-03T10:00:00Z",
            "sender": "SYNTH-SELF",
            "text": "INR 2 was paid at STORE.",
            "is_from_me": True,
            "service": "SMS",
        },
    ]
    _write_json(repo / "synthetic_source.json", source)
    _write_jsonl(repo / "fixture.jsonl", [{"sms": source[0]["text"]}])
    _write_jsonl(
        repo / "PRIVATE_DATA" / "legacy.jsonl",
        [
            {
                "sms": source[1]["text"],
                "sender": source[1]["sender"],
                "decision": "not_transaction",
            },
            {
                "sms": "Unmatched synthetic message not in archive.",
                "sender": "SYNTH-OTHER",
                "decision": "not_transaction",
            },
        ],
    )
    config = {
        "contract": "pocketfinancer.corpus-run/1",
        "source_path": "synthetic_source.json",
        "expected_source_rows": 4,
        "output_root": "PRIVATE_DATA/sms_processing",
        "primary_currency": "INR",
        "profile_ids": ["core-en", "india"],
        "later_time_cutoff": "2025-09-01T00:00:00Z",
        "pool_percentages": {
            "annotation_training": 80,
            "annotation_development": 10,
            "protected_test": 10,
        },
        "source_id_key_path": "PRIVATE_DATA/sms_processing/.source-id-key",
        "regression_fixture_path": "fixture.jsonl",
        "legacy_review_path": "PRIVATE_DATA/legacy.jsonl",
        "offline_retention": "retain_every_source_row",
        "build_sft_targets": False,
        "permissions": {"directory": "0700", "file": "0600"},
    }
    _write_json(repo / "config.json", config)
    return repo, config


def test_grouping_normalizes_placeholders_sender_family_and_hmac_keys() -> None:
    body = (
        "Alert Rs. 1,250.00 debited from A/c XX4321 on 12-03-2025 at 09:30 am "
        "to user@synthbank ref 998877 https://example.invalid/info"
    )
    assert canonical_exact_body("  INR\t 10.00  ") == "inr 10.00"
    template = normalized_template(body)
    for placeholder in ("<AMOUNT>", "<IDENTIFIER>", "<DATE>", "<TIME>", "<ADDRESS>", "<URL>"):
        assert placeholder in template
    assert sender_family("AD-SYNTH99") == "synth#"

    g1 = build_grouping(b"k" * 32, body, "AD-SYNTH99", "2025-03-12T09:30:00Z")
    g2 = build_grouping(b"m" * 32, body, "AD-SYNTH99", "invalid-timestamp")
    assert g1.time_group == "2025-03"
    assert g2.time_group == "unknown"
    assert g1.normalized_template_hash != g2.normalized_template_hash


def test_template_components_never_cross_pool_boundaries_and_leakage_audit_detects_violations() -> None:
    rows = [
        # Same template t1 straddles later_time_cutoff: entire component stays out of holdout.
        PoolInput("s1", "2024-01-01T00:00:00Z", "e1", "t1", "st1"),
        PoolInput("s2", "2026-02-01T00:00:00Z", "e2", "t1", "st2"),
        # Entire template t2 >= cutoff goes to later_time_holdout.
        PoolInput("s3", "2026-01-01T00:00:00Z", "e3", "t2", "st3"),
        # Template overlapping regression fixture goes to regression_only (even if also legacy).
        PoolInput("s4", "2024-01-01T00:00:00Z", "e4", "t3", "st4"),
        # Template overlapping legacy review goes to legacy_review.
        PoolInput("s5", "2024-01-01T00:00:00Z", "e5", "t4", "st5"),
    ]
    assignments = assign_pools(
        rows,
        regression_template_hashes={"t3"},
        legacy_template_hashes={"t3", "t4"},
        later_time_cutoff="2025-09-01T00:00:00Z",
        pool_percentages={
            "annotation_training": 80,
            "annotation_development": 10,
            "protected_test": 10,
        },
    )

    assert assignments["s1"] == assignments["s2"]
    assert assignments["s1"] in {"annotation_training", "annotation_development", "protected_test"}
    assert assignments["s3"] == "later_time_holdout"
    assert assignments["s4"] == "regression_only"
    assert assignments["s5"] == "legacy_review"
    assert leakage_audit(rows, assignments)["passed"] is True

    leaked = {**assignments, "s2": "protected_test" if assignments["s1"] != "protected_test" else "annotation_training"}
    audit = leakage_audit(rows, leaked)
    assert audit["passed"] is False
    assert audit["maximum_overlap"]["normalized_template"] == 1


def test_synthetic_corpus_build_retains_every_row_writes_reports_and_cli_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _config = _synthetic_repo(tmp_path)

    summary = build_private_corpus(repo, Path("config.json"))
    current = json.loads(
        (repo / "PRIVATE_DATA" / "sms_processing" / "CURRENT.json").read_text(encoding="utf-8")
    )
    run = repo / "PRIVATE_DATA" / "sms_processing" / "runs" / current["run_id"]
    manifest = [
        json.loads(line)
        for line in (run / "canonical_manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert summary["row_count"] == 4
    assert len(manifest) == 4
    assert len({row["source_id"] for row in manifest}) == 4
    assert sum(summary["pool_counts"].values()) == 4
    assert (run / "reports" / "leakage_audit.json").is_file()
    assert (run / "reports" / "artifact_hashes.json").is_file()
    legacy_report = json.loads(
        (run / "reports" / "legacy_review_migration.json").read_text(encoding="utf-8")
    )
    assert legacy_report == {"total": 2, "unique_match": 1, "ambiguous_match": 0, "missing": 1}
    assert os.stat(run).st_mode & 0o777 == 0o700
    assert os.stat(run / "canonical_manifest.jsonl").st_mode & 0o777 == 0o600

    # Idempotent reuse returns the identical aggregate summary.
    assert build_private_corpus(repo, Path("config.json")) == summary

    # CLI build-corpus prints aggregate JSON only.
    assert cli.main(["--repo-root", str(repo), "build-corpus", "--config", "config.json"]) == 0
    cli_summary = json.loads(capsys.readouterr().out)
    assert cli_summary == summary


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"build_sft_targets": True}, "must not create SFT"),
        ({"offline_retention": "drop_some"}, "retain every source row"),
        ({"pool_percentages": {"annotation_training": 50, "annotation_development": 10, "protected_test": 10}}, "percentages"),
        ({"permissions": {"directory": "0755", "file": "0644"}}, "permission policy"),
        ({"expected_source_rows": 99}, "does not match"),
        ({"source_path": "../escape.json"}, "outside the repository"),
    ],
)
def test_corpus_config_validation_fails_closed(
    tmp_path: Path, overrides: dict, match: str
) -> None:
    repo, config = _synthetic_repo(tmp_path)
    bad = {**config, **overrides}
    _write_json(repo / "bad_config.json", bad)
    with pytest.raises(PrivateArtifactError, match=match):
        build_private_corpus(repo, Path("bad_config.json"))
