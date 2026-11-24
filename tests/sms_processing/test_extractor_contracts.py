"""Schema, manifest, and synthetic vector checks for unified SMS contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import jsonschema
import pytest

from pocketfinancer_sms.account_resolution import AccountCatalogEntry
from pocketfinancer_sms.analyzer import ANALYSIS_CONTRACT_V2, DeterministicSmsAnalyzer
from pocketfinancer_sms.configuration import (
    PIPELINE_CONTRACT,
    AssetBinding,
    ExtractorRuntimeConfig,
    ProcessingConfigSnapshot,
    ProcessingTrigger,
    RolloutMode,
    WireTransport,
)
from pocketfinancer_sms.currency import CurrencyContext
from pocketfinancer_sms.extractor import (
    APPLE_FLAT_FIELDS,
    ExtractionValidationError,
    build_extractor_input,
    parse_and_normalize_extraction,
    project_apple_fm_flat_output,
)
from pocketfinancer_sms.feedback import (
    FieldRevisionProvenance,
    FieldRevisionV3,
    UserFeedbackEventV3,
)
from pocketfinancer_sms.processing import (
    ExtractionCoordinator,
    ReviewCase,
    ReviewUserRevision,
    processing_result_payload,
)
from pocketfinancer_sms.provenance import file_sha256
from pocketfinancer_sms.trace import ProcessingTraceV3, TraceEventV3
from pocketfinancer_sms.types import TimestampProvenance


ROOT = Path(__file__).resolve().parents[2]
SMS_CONFIGS = ROOT / "configs" / "sms_processing"
SCHEMAS = SMS_CONFIGS / "schemas"
PROFILES = SMS_CONFIGS / "profiles"
SHA = "a" * 64
OPERATION_ID = "11111111-1111-4111-8111-111111111111"
RECEIVED_AT = 1_700_000_000_000


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(name: str) -> dict:
    return _json(SCHEMAS / name)


def _analyzer() -> DeterministicSmsAnalyzer:
    return DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india")),
        analysis_contract=ANALYSIS_CONTRACT_V2,
    )


def test_every_active_schema_is_valid_draft_2020_12() -> None:
    schema_files = tuple(sorted(path.name for path in SCHEMAS.glob("*.schema.json")))
    assert len(schema_files) >= 12
    for name in schema_files:
        jsonschema.Draft202012Validator.check_schema(_schema(name))


def test_profiles_and_reason_codes_are_complete_and_unique() -> None:
    validation_profile = _json(PROFILES / "extractor-validation.json")
    assert validation_profile["contract"] == "pocketfinancer.extractor-validation-profile/1"
    account_profile = _json(PROFILES / "account-resolution.json")
    assert account_profile["contract"] == "pocketfinancer.account-resolution-profile/1"
    persistence_policy = _json(PROFILES / "persistence-policy.json")
    assert persistence_policy["contract"] == "pocketfinancer.persistence-policy/1"
    assert persistence_policy["duplicate_window_ms"] == 300_000
    assert persistence_policy["retain_rejected_body"] is False

    registry = _json(PROFILES / "reason-codes.json")
    codes = [item["code"] for namespace in registry["namespaces"] for item in namespace["codes"]]
    assert len(codes) == len(set(codes))
    required = {
        "extractor_malformed_json",
        "extractor_duplicate_json_key",
        "extractor_extra_content",
        "extractor_missing_amount",
        "extractor_missing_direction",
        "extractor_missing_account",
        "extractor_evidence_out_of_bounds",
        "extractor_evidence_mismatch",
        "extractor_amount_value_disagreement",
        "extractor_abstained",
        "runtime_unavailable",
        "runtime_failure",
        "runtime_output_truncated",
        "user_cancelled",
        "process_interrupted",
        "account_resolution_unresolved",
        "account_resolution_ambiguous",
        "duplicate_possible",
        "duplicate_already_persisted",
        "persistence_configuration_hash_mismatch",
        "persistence_claim_ownership_invalid",
        "persistence_receipt_timestamp_provenance_invalid",
        "persistence_blocked_by_rollout_mode",
        "extractor_none",
        "extractor_posted_valid",
        "review_retained",
        "persistence_blocked",
    }
    assert required <= set(codes)


def test_sanitized_goldens_are_grounded_and_parse_under_schema() -> None:
    golden = _json(ROOT / "tests" / "sms_processing" / "golden" / "extractor-v1" / "sanitized-vectors.json")
    schema = _schema("sms-extractor.schema.json")
    for case in golden["cases"]:
        jsonschema.validate(case["model_output"], schema)
        encoded = json.dumps(case["model_output"], ensure_ascii=False)
        if "expected_reason" in case:
            with pytest.raises(ExtractionValidationError, match=case["expected_reason"]):
                parse_and_normalize_extraction(
                    encoded,
                    case["sms_body"],
                    primary_currency="INR",
                    enabled_profile_ids=("core-en", "india"),
                )
            continue
        result = parse_and_normalize_extraction(
            encoded,
            case["sms_body"],
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )
        assert result.decision == case["expected"]["decision"]
        if result.transaction is not None:
            assert result.transaction.minor_units == case["expected"]["amount_minor_units"]
            assert result.transaction.account_reference == case["expected"]["account_reference"]


def test_executable_input_config_result_review_feedback_and_trace_match_schemas() -> None:
    source = "INR 42.50 was credited to account XX7788."
    analysis = _analyzer().analyze(
        source,
        operation_id=OPERATION_ID,
        operation_config_hash=SHA,
        source_timestamp_epoch_ms=RECEIVED_AT,
        source_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
    )
    request = build_extractor_input(
        source,
        analysis,
        sender_family="synthetic-family",
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    jsonschema.validate(request, _schema("sms-extractor-input.schema.json"))

    config = ProcessingConfigSnapshot(
        operation_id=OPERATION_ID,
        parent_operation_id=None,
        source_ref_hash="1" * 64,
        trigger=ProcessingTrigger.REALTIME,
        created_at_epoch_ms=RECEIVED_AT + 100,
        admission_epoch_ms=RECEIVED_AT,
        release_id=PIPELINE_CONTRACT,
        release_manifest_sha256="2" * 64,
        analyzer_behavior_version="pocketfinancer.structural-sms-analyzer/2",
        unicode_behavior_version="unicode-scalar-nfkc-casefold",
        currency_asset_sha256="3" * 64,
        profile_assets=(AssetBinding("core-en", "4" * 64), AssetBinding("india", "5" * 64)),
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        timezone_id="Asia/Kolkata",
        extractor=ExtractorRuntimeConfig(
            eligible=True,
            ineligibility_reason=None,
            model_identifier="synthetic-local-model",
            model_file_sha256="9" * 64,
            model_identity_kind="file_sha256",
            runtime_version="synthetic-runtime",
            os_version="synthetic-os",
            device_cohort="synthetic-device",
            prompt_sha256="6" * 64,
            grammar_sha256="7" * 64,
            grammar_enabled=True,
            wire_transport=WireTransport.DIRECT_JSON,
            validation_profile_sha256="8" * 64,
        ),
        rollout_mode=RolloutMode.AUTOMATIC,
    )
    jsonschema.validate(config.to_dict(), _schema("processing-config.schema.json"))

    coordinator = ExtractionCoordinator(
        _analyzer(),
        lambda _request, _token: '{"decision":"none"}',
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    excluded = coordinator.process(
        source="Your synthetic OTP is 123456.",
        raw_sender="AD-SYNTH",
        operation_id=OPERATION_ID,
        operation_config_hash=SHA,
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        source_event_key="synthetic-event-excl",
        idempotency_key="synthetic-idempotency-excl",
    )
    assert excluded.status == "tombstoned_excluded"
    assert excluded.body_retained is False
    jsonschema.validate(
        processing_result_payload(excluded), _schema("processing-result.schema.json")
    )

    none_outcome = coordinator.process(
        source="INR 42.50 available balance in account XX7788.",
        raw_sender="AD-SYNTH",
        operation_id=OPERATION_ID,
        operation_config_hash=SHA,
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        source_event_key="synthetic-event-none",
        idempotency_key="synthetic-idempotency-none",
    )
    assert none_outcome.status == "tombstoned_none"
    assert none_outcome.body_retained is False
    jsonschema.validate(
        processing_result_payload(none_outcome), _schema("processing-result.schema.json")
    )

    posted_output = {
        "decision": "posted",
        "amount": {
            "value": "42.50",
            "currency": "INR",
            "evidence": {"start_scalar": 0, "end_scalar": 9, "text": "INR 42.50"},
        },
        "direction": {
            "value": "credit",
            "evidence": {
                "start_scalar": source.index("credited"),
                "end_scalar": source.index("credited") + len("credited"),
                "text": "credited",
            },
        },
        "account": {
            "reference": "XX7788",
            "evidence": {
                "start_scalar": source.index("XX7788"),
                "end_scalar": source.index("XX7788") + len("XX7788"),
                "text": "XX7788",
            },
        },
        "counterparty": None,
    }
    posted_coordinator = ExtractionCoordinator(
        _analyzer(),
        lambda _request, _token: json.dumps(posted_output),
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
        account_catalog=(AccountCatalogEntry("opaque-account-1", "bank_account", ("XX7788",)),),
    )
    posted_outcome = posted_coordinator.process(
        source=source,
        raw_sender="AD-SYNTH",
        operation_id=OPERATION_ID,
        operation_config_hash=SHA,
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        source_event_key="synthetic-posted-event",
        idempotency_key="synthetic-posted-idempotency",
    )
    posted_payload = processing_result_payload(posted_outcome)
    assert posted_payload["status"] == "eligible"
    assert posted_payload["account_resolution"]["status"] == "uniquely_resolved"
    assert posted_payload["duplicate_assessment"]["status"] == "clear"
    assert posted_payload["automatic_persistence"]["result"] == "eligible"
    jsonschema.validate(posted_payload, _schema("processing-result.schema.json"))

    review = ReviewCase(
        review_case_id="synthetic-review",
        operation_id_hash=hashlib.sha256(OPERATION_ID.encode()).hexdigest(),
        raw_sender="AD-SYNTH",
        source=source,
        received_at_epoch_ms=RECEIVED_AT,
        primary_reason="extractor_abstained",
        reason_codes=("extractor_abstained",),
        furthest_stage="extractor_validation",
        analyzer_suggestions=(),
        extractor_suggestion=None,
        account_resolution=None,
        user_revisions=(
            ReviewUserRevision(
                revision=1,
                action="correct",
                actor_id_hash="c" * 64,
                field_revisions=(
                    FieldRevisionV3(
                        "direction",
                        "credit",
                        FieldRevisionProvenance.USER_DIRECTION_CONTROL,
                    ),
                ),
                created_at_epoch_ms=RECEIVED_AT,
            ),
        ),
        revision=2,
        previous_revision_hash="e" * 64,
    )
    jsonschema.validate(json.loads(json.dumps(review.to_dict())), _schema("review-case.schema.json"))

    feedback = UserFeedbackEventV3.create(
        action_id="22222222-2222-4222-8222-222222222222",
        operation_id=OPERATION_ID,
        review_case=ReviewCase(
            review_case_id="synthetic-feedback-review",
            operation_id_hash=hashlib.sha256(OPERATION_ID.encode()).hexdigest(),
            raw_sender="AD-SYNTH",
            source=source,
            received_at_epoch_ms=RECEIVED_AT,
            primary_reason="extractor_abstained",
            reason_codes=("extractor_abstained",),
            furthest_stage="extractor_validation",
            analyzer_suggestions=(),
            extractor_suggestion=None,
            account_resolution=None,
        ),
        expected_review_revision=0,
        resulting_review_revision=1,
        action="correct",
        actor_id="synthetic-user",
        field_revisions=(
            FieldRevisionV3("direction", "credit", FieldRevisionProvenance.USER_DIRECTION_CONTROL),
        ),
        created_at_epoch_ms=RECEIVED_AT,
    )
    jsonschema.validate(feedback.to_dict(), _schema("user-feedback.schema.json"))

    trace = ProcessingTraceV3.create(
        operation_id=OPERATION_ID,
        config_hash=SHA,
        owner_generation=1,
        events=(
            TraceEventV3(
                sequence=0,
                event_id="33333333-3333-4333-8333-333333333333",
                occurred_at_epoch_ms=RECEIVED_AT,
                stage="prefilter",
                status="completed",
            ),
        ),
    )
    jsonschema.validate(json.loads(json.dumps(asdict(trace))), _schema("processing-trace.schema.json"))


def test_apple_flat_schema_matches_python_projection_fields() -> None:
    flat_schema = _schema("apple-fm-flat.schema.json")
    assert tuple(flat_schema["required"]) == APPLE_FLAT_FIELDS
    assert tuple(flat_schema["properties"]) == APPLE_FLAT_FIELDS
    projected = project_apple_fm_flat_output(
        {key: ("none" if key == "decision" else ("" if not key.endswith(("_start", "_end")) else 0))
         for key in APPLE_FLAT_FIELDS}
    )
    jsonschema.validate(projected, _schema("sms-extractor.schema.json"))


def test_canonical_label_v2_retains_taxonomy_and_requires_grounded_posted_fields() -> None:
    source = "INR 42.50 was credited to account XX7788."

    def span(text: str) -> dict:
        start = source.index(text)
        return {"start_scalar": start, "end_scalar": start + len(text), "text": text}

    payload = {
        "contract": "pocketfinancer.canonical-label/2",
        "label_id": "synthetic-label",
        "source_id": "synthetic-source",
        "revision": 1,
        "status": "submitted",
        "decision": "posted",
        "operational_class": "posted_candidate",
        "event_state": "posted",
        "financial_family": "bank_transfer",
        "payment_rail": "bank_internal",
        "event": {
            "amount_value": "42.50",
            "currency": "INR",
            "amount_span": span("INR 42.50"),
            "direction": "credit",
            "direction_span": span("credited"),
            "account_reference": "XX7788",
            "account_span": span("XX7788"),
            "existing_account_id": "opaque-account-1",
            "counterparty": None,
            "counterparty_span": None,
        },
        "uncertain": False,
        "notes": "",
        "reviewer_id": "synthetic-reviewer",
        "created_at_epoch_ms": RECEIVED_AT,
        "supersedes_revision": None,
    }
    schema = _schema("canonical-label.schema.json")
    jsonschema.validate(payload, schema)
    payload["event"]["counterparty"] = "SYNTHETIC MERCHANT"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
    payload["event"]["counterparty"] = None
    del payload["event"]["direction_span"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_release_manifest_validates_against_schema_and_file_hashes() -> None:
    manifest = _json(SMS_CONFIGS / "release-manifest.json")
    jsonschema.validate(manifest, _schema("release-manifest.schema.json"))
    assert manifest["release_id"] == PIPELINE_CONTRACT
    assert manifest["duplicate_window_ms"] == 300_000
    for artifact in manifest["artifacts"]:
        assert file_sha256(SMS_CONFIGS / artifact["path"]) == artifact["sha256"]


def test_output_contract_rejects_time_fields_and_utf_offsets() -> None:
    schema = _schema("sms-extractor.schema.json")
    payload = {
        "decision": "posted",
        "amount": {
            "value": "1.00",
            "currency": "INR",
            "evidence": {"start_scalar": 0, "end_scalar": 8, "text": "INR 1.00"},
        },
        "direction": {
            "value": "debit",
            "evidence": {"start_scalar": 9, "end_scalar": 16, "text": "debited"},
        },
        "account": {
            "reference": "XX1234",
            "evidence": {"start_scalar": 22, "end_scalar": 28, "text": "XX1234"},
        },
        "counterparty": None,
        "message_time": 0,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
    del payload["message_time"]
    payload["account"]["evidence"]["start_utf16"] = 22
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
