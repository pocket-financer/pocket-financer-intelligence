"""Synthetic canonical-label, trace, and feedback tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from pocketfinancer_sms.analyzer import DeterministicSmsAnalyzer
from pocketfinancer_sms.currency import CurrencyContext
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
from pocketfinancer_sms.processing import ReviewCase
from pocketfinancer_sms.trace import ProcessingTraceV3, TraceEventV3
from pocketfinancer_sms.types import Direction


def _posted_fixture():
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
        existing_account_id="acct-7788",
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
        notes="",
        reviewer_id="synthetic-reviewer",
        created_at_epoch_ms=1_700_000_000_000,
    )
    analysis = DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india"))
    ).analyze(source, operation_id="synthetic-label")
    return source, analysis, label


def test_valid_canonical_label_projects_to_extractor_target() -> None:
    source, _analysis, label = _posted_fixture()
    validate_canonical_label_v2(label, source)
    target = project_extractor_target(label, source)

    assert target["decision"] == "posted"
    assert target["amount"]["value"] == "42.50"
    assert target["amount"]["currency"] == "INR"
    assert target["direction"]["value"] == "credit"
    assert target["account"]["reference"] == "**7788"
    assert target["counterparty"]["value"] == "FRIEND"


def test_non_financial_outgoing_or_uncertain_posted_blocks_target_projection() -> None:
    source, _analysis, label = _posted_fixture()
    uncertain = replace(label, uncertain=True)
    with pytest.raises(LabelValidationError, match="projection_uncertain"):
        project_extractor_target(uncertain, source)

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


def test_amount_evidence_must_contain_exact_money_value() -> None:
    source, _analysis, label = _posted_fixture()
    assert label.event is not None
    start = source.index("credited")
    invalid_event = replace(
        label.event,
        amount_span=SourceSpan.from_source(source, start, start + len("credited")),
    )
    with pytest.raises(LabelValidationError, match="label_extractor_event_invalid"):
        validate_canonical_label_v2(replace(label, event=invalid_event), source)


def test_canonical_decision_enum_remains_complete() -> None:
    assert {item.value for item in CanonicalDecision} == {
        "posted",
        "not_posted",
        "non_financial",
        "ambiguous",
        "multiple_event",
    }


def test_processing_trace_includes_prefilter_and_hash_chains() -> None:
    first = TraceEventV3(
        sequence=0,
        event_id="11111111-1111-4111-8111-111111111111",
        occurred_at_epoch_ms=1_700_000_000_000,
        stage="prefilter",
        status="completed",
        reason_codes=("prefilter_eligible",),
    )
    second = TraceEventV3(
        sequence=1,
        event_id="22222222-2222-4222-8222-222222222222",
        occurred_at_epoch_ms=1_700_000_000_100,
        stage="analysis_advisory",
        status="completed",
        previous_event_hash=first.event_hash,
    )
    trace = ProcessingTraceV3.create(
        operation_id="33333333-3333-4333-8333-333333333333",
        config_hash="a" * 64,
        owner_generation=1,
        events=(first, second),
    )
    changed = ProcessingTraceV3.create(
        operation_id="33333333-3333-4333-8333-333333333333",
        config_hash="a" * 64,
        owner_generation=1,
        events=(first,),
    )
    assert len(trace.trace_hash) == 64
    assert trace.trace_hash != changed.trace_hash


def test_feedback_is_hash_chained_and_validates_spans() -> None:
    source, _analysis, _label = _posted_fixture()
    review = ReviewCase(
        review_case_id="synthetic-review",
        operation_id_hash=hashlib.sha256(
            "33333333-3333-4333-8333-333333333333".encode()
        ).hexdigest(),
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
    event = UserFeedbackEventV3.create(
        action_id="44444444-4444-4444-8444-444444444444",
        operation_id="33333333-3333-4333-8333-333333333333",
        review_case=review,
        expected_review_revision=0,
        resulting_review_revision=1,
        action="correct",
        actor_id="synthetic-reviewer",
        field_revisions=(
            FieldRevisionV3(
                "direction",
                "credit",
                FieldRevisionProvenance.USER_DIRECTION_CONTROL,
            ),
        ),
        created_at_epoch_ms=1_700_000_000_500,
    )
    assert len(event.event_hash) == 64
