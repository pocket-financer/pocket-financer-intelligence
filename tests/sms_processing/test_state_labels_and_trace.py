"""Tests for immutable config snapshots, canonical labels, review revisions, feedback, and traces."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from pocketfinancer_sms.configuration import (
    APPLE_PROMPT_VERSION,
    PIPELINE_CONTRACT,
    AssetBinding,
    ExtractorRuntimeConfig,
    ProcessingConfigSnapshot,
    ProcessingTrigger,
    RolloutMode,
    WireTransport,
)
from pocketfinancer_sms.extractor import SourceSpan
from pocketfinancer_sms.feedback import (
    FieldRevisionProvenance,
    FieldRevisionV3,
    UserFeedbackEventV3,
)
from pocketfinancer_sms.labels import (
    CanonicalDecision,
    CanonicalLabelV2,
    EventState,
    ExtractorCanonicalEvent,
    LabelValidationError,
    OperationalClass,
    ReviewStatus,
    project_extractor_target,
    validate_canonical_label_v2,
)
from pocketfinancer_sms.processing import ReviewCase, ReviewUserRevision
from pocketfinancer_sms.trace import ProcessingTraceV3, TraceEventV3
from pocketfinancer_sms.types import Direction, TimestampProvenance


OP_ID = "11111111-1111-4111-8111-111111111111"
OP_ID_2 = "22222222-2222-4222-8222-222222222222"


def _source_and_label() -> tuple[str, CanonicalLabelV2]:
    source = "INR 42.50 was credited to account **7788 from FRIEND."

    def span(text: str) -> SourceSpan:
        start = source.index(text)
        return SourceSpan.from_source(source, start, start + len(text))

    event = ExtractorCanonicalEvent(
        amount_value="42.50",
        currency="INR",
        amount_span=span("INR 42.50"),
        direction=Direction.CREDIT,
        direction_span=span("credited"),
        account_reference="**7788",
        account_span=span("**7788"),
        existing_account_id="account-opaque-1",
        counterparty="FRIEND",
        counterparty_span=span("FRIEND"),
    )
    label = CanonicalLabelV2(
        label_id="synthetic-label-1",
        source_id="synthetic-source",
        revision=1,
        status=ReviewStatus.SUBMITTED,
        decision="posted",
        operational_class=OperationalClass.POSTED_CANDIDATE,
        event_state=EventState.POSTED,
        financial_family="bank_transfer",
        payment_rail="bank_internal",
        event=event,
        uncertain=False,
        notes="synthetic",
        reviewer_id="synthetic-reviewer",
        created_at_epoch_ms=1_700_000_000_000,
    )
    return source, label


def _config() -> ProcessingConfigSnapshot:
    return ProcessingConfigSnapshot(
        operation_id=OP_ID,
        parent_operation_id=None,
        source_ref_hash="1" * 64,
        trigger=ProcessingTrigger.REALTIME,
        created_at_epoch_ms=1_700_000_000_100,
        admission_epoch_ms=1_700_000_000_000,
        release_id=PIPELINE_CONTRACT,
        release_manifest_sha256="2" * 64,
        analyzer_behavior_version="pocketfinancer.structural-sms-analyzer/2",
        unicode_behavior_version="unicode-scalar-nfkc-casefold",
        currency_asset_sha256="3" * 64,
        profile_assets=(
            AssetBinding("core-en", "4" * 64),
            AssetBinding("india", "5" * 64),
        ),
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
        received_at_epoch_ms=1_699_999_999_000,
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


def _review_case(source: str) -> ReviewCase:
    return ReviewCase(
        review_case_id="review-synthetic",
        operation_id_hash=hashlib.sha256(OP_ID.encode()).hexdigest(),
        raw_sender="SYNTH-BANK",
        source=source,
        received_at_epoch_ms=1_700_000_000_000,
        primary_reason="extractor_abstained",
        reason_codes=("extractor_abstained",),
        furthest_stage="extractor_validation",
        analyzer_suggestions=(),
        extractor_suggestion=None,
        account_resolution=None,
    )


def test_processing_config_hashing_immutability_and_runtime_guards() -> None:
    config = _config()
    payload = config.to_dict()
    assert payload["contract"] == PIPELINE_CONTRACT
    assert payload["extractor"]["parser_deadline_ms"] == 0
    assert payload["received_timestamp"] == {
        "epoch_ms": 1_699_999_999_000,
        "provenance": "platform_received",
        "timezone_id": "Asia/Kolkata",
        "read_only": True,
    }
    assert payload["config_hash"] == config.config_hash
    assert replace(config, primary_currency="USD").config_hash != config.config_hash

    # System-managed Apple FM runtime requires no GGUF hash and allows apple_fm_guided_flat.
    apple_runtime = ExtractorRuntimeConfig(
        eligible=True,
        ineligibility_reason=None,
        model_identifier="SystemLanguageModel.general",
        model_file_sha256=None,
        model_identity_kind="system_managed_runtime",
        runtime_version="synthetic-ios",
        os_version="synthetic-os",
        device_cohort="synthetic-device",
        prompt_sha256="6" * 64,
        validation_profile_sha256="8" * 64,
        wire_transport=WireTransport.APPLE_FM_GUIDED_FLAT,
        prompt_version=APPLE_PROMPT_VERSION,
    )
    assert replace(config, extractor=apple_runtime).config_hash != config.config_hash

    with pytest.raises(ValueError, match="release provenance"):
        replace(config, release_id="native-integration-v2")
    with pytest.raises(ValueError, match="analyzer provenance"):
        replace(config, analyzer_behavior_version="pocketfinancer.structural-sms-analyzer/1")
    with pytest.raises(ValueError, match="parent operation"):
        replace(config, trigger=ProcessingTrigger.RETRY, parent_operation_id=None)
    with pytest.raises(ValueError, match="own parent"):
        replace(config, trigger=ProcessingTrigger.RETRY, parent_operation_id=OP_ID)
    with pytest.raises(ValueError, match="GGUF hash"):
        replace(apple_runtime, model_file_sha256="a" * 64)


def test_canonical_label_v2_validation_and_direct_extractor_projection() -> None:
    source, label = _source_and_label()
    validate_canonical_label_v2(label, source)
    target = project_extractor_target(label, source)
    assert target["decision"] == "posted"
    assert target["amount"]["value"] == "42.50"
    assert target["amount"]["currency"] == "INR"
    assert target["direction"]["value"] == "credit"
    assert target["account"]["reference"] == "**7788"
    assert target["account"]["evidence"]["text"] == "**7788"
    assert target["counterparty"]["value"] == "FRIEND"
    assert "candidate" not in repr(target)

    # Draft, uncertain, and invalid_outgoing labels cannot project as training targets.
    with pytest.raises(LabelValidationError, match="projection_label_not_committed"):
        project_extractor_target(replace(label, status=ReviewStatus.DRAFT), source)
    with pytest.raises(LabelValidationError, match="projection_uncertain"):
        project_extractor_target(replace(label, uncertain=True), source)

    invalid_outgoing = replace(
        label,
        decision="none",
        operational_class=OperationalClass.INVALID_OUTGOING,
        event_state=EventState.NO_EVENT,
        financial_family=None,
        payment_rail=None,
        event=None,
    )
    validate_canonical_label_v2(invalid_outgoing, source)
    with pytest.raises(LabelValidationError, match="projection_invalid_outgoing_excluded"):
        project_extractor_target(invalid_outgoing, source)

    # Non-posted committed labels project cleanly without an event payload.
    non_financial = replace(
        invalid_outgoing, operational_class=OperationalClass.NON_FINANCIAL
    )
    assert project_extractor_target(non_financial, source) == {"decision": "none"}

    # Misgrounded amount or account spans fail validation.
    assert label.event is not None
    with pytest.raises(LabelValidationError, match="label_extractor_event_invalid"):
        validate_canonical_label_v2(
            replace(label, event=replace(label.event, amount_span=label.event.direction_span)),
            source,
        )
    with pytest.raises(LabelValidationError, match="label_extractor_event_invalid"):
        project_extractor_target(
            replace(label, event=replace(label.event, account_span=label.event.direction_span)),
            source,
        )

    assert {item.value for item in CanonicalDecision} == {
        "posted",
        "not_posted",
        "non_financial",
        "ambiguous",
        "multiple_event",
    }


def test_review_case_revision_chain_and_read_only_receipt() -> None:
    review = _review_case("Synthetic body")
    payload = review.to_dict()
    assert payload["receipt_timestamp"]["read_only"] is True
    assert payload["source_sha256"]
    assert payload["revision_hash"] == review.content_hash
    with pytest.raises(ValueError, match="predecessor"):
        replace(review, revision=2)

    direction = FieldRevisionV3(
        "direction",
        "credit",
        FieldRevisionProvenance.USER_DIRECTION_CONTROL,
    )
    first_user_revision = ReviewUserRevision(
        revision=1,
        action="correct",
        actor_id_hash="b" * 64,
        field_revisions=(direction,),
        created_at_epoch_ms=1_700_000_000_100,
    )
    revised = replace(
        review,
        revision=2,
        user_revisions=(first_user_revision,),
        previous_revision_hash=review.content_hash,
    )
    revised_payload = revised.to_dict()
    assert revised_payload["user_revisions"][0]["revision_hash"] == (
        first_user_revision.revision_hash
    )
    assert revised_payload["previous_revision_hash"] == review.content_hash

    broken_second = ReviewUserRevision(
        revision=2,
        action="confirm",
        actor_id_hash="c" * 64,
        field_revisions=(),
        created_at_epoch_ms=1_700_000_000_200,
        previous_revision_hash="d" * 64,
    )
    with pytest.raises(ValueError, match="hash chain"):
        replace(
            review,
            revision=3,
            user_revisions=(first_user_revision, broken_second),
            previous_revision_hash=revised.content_hash,
        )


def test_feedback_v3_validates_source_spans_operation_and_forbids_time_edits() -> None:
    source, label = _source_and_label()
    assert label.event is not None
    event = label.event
    amount = FieldRevisionV3(
        "amount",
        "42.50",
        FieldRevisionProvenance.SOURCE_SPAN_SELECTION,
        event.amount_span,
    )
    direction = FieldRevisionV3(
        "direction",
        "credit",
        FieldRevisionProvenance.USER_DIRECTION_CONTROL,
    )
    account = FieldRevisionV3(
        "account",
        "7788",
        FieldRevisionProvenance.SOURCE_SPAN_SELECTION,
        event.account_span,
        "account-opaque-1",
    )
    common = {
        "action_id": OP_ID_2,
        "operation_id": OP_ID,
        "review_case": _review_case(source),
        "expected_review_revision": 0,
        "resulting_review_revision": 1,
        "action": "correct",
        "actor_id": "synthetic-user",
        "created_at_epoch_ms": 1_700_000_000_100,
    }
    feedback = UserFeedbackEventV3.create(
        field_revisions=(amount, direction, account),
        **common,
    )
    assert feedback.to_dict()["field_revisions"][1]["span"] is None
    assert len(feedback.event_hash) == 64

    with pytest.raises(ValueError, match="unsupported"):
        FieldRevisionV3(
            "transaction_time",
            0,
            FieldRevisionProvenance.SOURCE_SPAN_SELECTION,
            SourceSpan.from_source(source, 0, 3),
        )

    substituted_source = "USD 99.99 was credited to account XX7788."
    fabricated = FieldRevisionV3(
        "amount",
        "99.99",
        FieldRevisionProvenance.SOURCE_SPAN_SELECTION,
        SourceSpan.from_source(substituted_source, 0, len("USD 99.99")),
    )
    with pytest.raises(ValueError, match="does not match source"):
        UserFeedbackEventV3.create(field_revisions=(fabricated,), **common)

    out_of_bounds = FieldRevisionV3(
        "amount",
        "42.50",
        FieldRevisionProvenance.SOURCE_SPAN_SELECTION,
        SourceSpan(0, len(source) + 1, source),
    )
    with pytest.raises(ValueError, match="span is invalid"):
        UserFeedbackEventV3.create(field_revisions=(out_of_bounds,), **common)

    with pytest.raises(ValueError, match="operation does not match"):
        UserFeedbackEventV3.create(
            field_revisions=(amount,),
            **{**common, "operation_id": "33333333-3333-4333-8333-333333333333"},
        )


def test_processing_trace_v3_hash_chain_and_stage_validation() -> None:
    first = TraceEventV3(
        sequence=0,
        event_id="33333333-3333-4333-8333-333333333333",
        occurred_at_epoch_ms=1_700_000_000_000,
        stage="prefilter",
        status="completed",
        reason_codes=("prefilter_eligible",),
    )
    second = TraceEventV3(
        sequence=1,
        event_id="44444444-4444-4444-8444-444444444444",
        occurred_at_epoch_ms=1_700_000_000_100,
        stage="extractor_execution",
        status="completed",
        previous_event_hash=first.event_hash,
    )
    trace = ProcessingTraceV3.create(
        operation_id=OP_ID,
        config_hash="a" * 64,
        owner_generation=1,
        events=(first, second),
    )
    shorter = ProcessingTraceV3.create(
        operation_id=OP_ID,
        config_hash="a" * 64,
        owner_generation=1,
        events=(first,),
    )
    assert trace.contract == "pocketfinancer.processing-trace/3"
    assert len(trace.trace_hash) == 64
    assert trace.trace_hash != shorter.trace_hash

    with pytest.raises(ValueError, match="hash chain"):
        ProcessingTraceV3.create(
            operation_id=OP_ID,
            config_hash="a" * 64,
            owner_generation=1,
            events=(first, replace(second, previous_event_hash="b" * 64)),
        )
    with pytest.raises(ValueError, match="contiguous"):
        ProcessingTraceV3.create(
            operation_id=OP_ID,
            config_hash="a" * 64,
            owner_generation=1,
            events=(second,),
        )
    with pytest.raises(ValueError, match="stage or status"):
        TraceEventV3(
            sequence=0,
            event_id="55555555-5555-4555-8555-555555555555",
            occurred_at_epoch_ms=1_700_000_000_000,
            stage="nonexistent_stage",
            status="completed",
        )
