"""Tests for strict direct extraction, grounding, account resolution, duplicates, and coordinator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from pocketfinancer_sms.account_resolution import (
    DUPLICATE_WINDOW_MS,
    AccountCatalogEntry,
    DuplicateStatus,
    RecentTransactionRecord,
    ResolutionStatus,
    assess_duplicate,
    is_within_duplicate_window,
    resolve_account,
    transaction_fingerprint,
)
from pocketfinancer_sms.analyzer import ANALYSIS_CONTRACT_V2, DeterministicSmsAnalyzer
from pocketfinancer_sms.currency import CurrencyContext
from pocketfinancer_sms.extractor import (
    APPLE_FLAT_FIELDS,
    RAW_OUTPUT_UTF8_BYTE_LIMIT,
    ExtractionResult,
    ExtractionValidationError,
    ExtractorFieldEvidence,
    SourceSpan,
    build_extractor_input,
    collect_grounded_extractor_fields,
    normalize_account_reference,
    normalize_counterparty,
    parse_and_normalize_extraction,
    project_apple_fm_flat_output,
)
from pocketfinancer_sms.processing import (
    CancellationToken,
    ExtractionCoordinator,
    PersistenceContext,
    RuntimeTruncatedError,
    RuntimeUnavailableError,
    evaluate_persistence,
)
from pocketfinancer_sms.types import GateResult, TimestampProvenance


CONFIG_HASH = "a" * 64
RECEIVED_AT = 1_700_000_000_000
BASE_SOURCE = "INR 1,250.00 was debited from account XX1234."
APPLE_STRING_FIELDS = frozenset(
    key for key in APPLE_FLAT_FIELDS if not key.endswith(("_start", "_end"))
)


def _analyzer() -> DeterministicSmsAnalyzer:
    return DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india")),
        analysis_contract=ANALYSIS_CONTRACT_V2,
    )


def _analysis(source: str):
    return _analyzer().analyze(
        source,
        operation_id="synthetic-extractor",
        operation_config_hash=CONFIG_HASH,
        source_timestamp_epoch_ms=RECEIVED_AT,
        source_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
    )


def _span(source: str, text: str) -> dict[str, object]:
    start = source.index(text)
    return {"start_scalar": start, "end_scalar": start + len(text), "text": text}


def _posted_dict(
    source: str = BASE_SOURCE,
    *,
    amount_text: str = "INR 1,250.00",
    amount_value: str = "1250.00",
    currency: str = "INR",
    direction_text: str = "debited",
    direction_value: str = "debit",
    account_text: str = "XX1234",
    account_ref: str | None = None,
    counterparty: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "decision": "posted",
        "amount": {
            "value": amount_value,
            "currency": currency,
            "evidence": _span(source, amount_text),
        },
        "direction": {
            "value": direction_value,
            "evidence": _span(source, direction_text),
        },
        "account": {
            "reference": account_ref if account_ref is not None else account_text,
            "evidence": _span(source, account_text),
        },
        "counterparty": counterparty,
    }


def _posted(source: str = BASE_SOURCE, *, amount: str = "INR 1,250.00", account: str = "XX1234") -> str:
    return json.dumps(_posted_dict(source, amount_text=amount, account_text=account))


def _coordinator(model, *, catalog=(), apply_prefilter: bool = True) -> ExtractionCoordinator:
    return ExtractionCoordinator(
        _analyzer(),
        model,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
        account_catalog=tuple(catalog),
        apply_prefilter=apply_prefilter,
    )


def _process(coordinator: ExtractionCoordinator, source: str, **kwargs):
    return coordinator.process(
        source=source,
        raw_sender="AD-SYNTH1",
        operation_id="11111111-1111-4111-8111-111111111111",
        operation_config_hash=CONFIG_HASH,
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        source_event_key="synthetic-source-event",
        idempotency_key="synthetic-idempotency",
        **kwargs,
    )


def test_advisory_evidence_is_strictly_advisory_and_input_guards_validate() -> None:
    analysis = _analysis(BASE_SOURCE)
    zero_payload = build_extractor_input(
        BASE_SOURCE,
        replace(analysis, candidates=(), cues=()),
        sender_family="synth#",
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    assert zero_payload["advisory_evidence"] == []

    wrong_candidate = replace(
        analysis.candidates[0],
        value={"minor_units": 1, "currency": "USD", "currency_provenance": "explicit_code"},
    )
    conflicting = build_extractor_input(
        BASE_SOURCE,
        replace(analysis, candidates=(wrong_candidate,)),
        sender_family="synth#",
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    assert len(conflicting["advisory_evidence"]) >= 1

    result = parse_and_normalize_extraction(
        _posted(BASE_SOURCE),
        BASE_SOURCE,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    assert result.transaction is not None
    assert result.transaction.minor_units == 125_000
    assert result.transaction.currency == "INR"
    assert result.transaction.account_reference == "1234"

    with pytest.raises(ValueError, match="nonempty"):
        build_extractor_input("", analysis, sender_family="synth", primary_currency="INR", enabled_profile_ids=("core-en",))
    with pytest.raises(ValueError, match="does not match"):
        build_extractor_input("Different SMS", analysis, sender_family="synth", primary_currency="INR", enabled_profile_ids=("core-en",))
    with pytest.raises(ValueError, match="sender family"):
        build_extractor_input(BASE_SOURCE, analysis, sender_family="  ", primary_currency="INR", enabled_profile_ids=("core-en",))


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("amount", "extractor_missing_amount"),
        ("direction", "extractor_missing_direction"),
        ("account", "extractor_missing_account"),
    ],
)
def test_posted_mandatory_fields_are_required(missing: str, reason: str) -> None:
    payload = json.loads(_posted(BASE_SOURCE))
    del payload[missing]
    with pytest.raises(ExtractionValidationError, match=reason):
        parse_and_normalize_extraction(
            json.dumps(payload),
            BASE_SOURCE,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )
    outcome = _process(_coordinator(lambda _request, _token: json.dumps(payload)), BASE_SOURCE)
    assert outcome.status == "review"
    assert outcome.reason_codes == (reason,)


def test_evidence_span_mismatch_and_out_of_bounds_fail_closed() -> None:
    mismatch = json.loads(_posted(BASE_SOURCE))
    mismatch["amount"]["evidence"]["text"] = "INR 9,999"
    with pytest.raises(ExtractionValidationError, match="extractor_evidence_mismatch"):
        parse_and_normalize_extraction(
            json.dumps(mismatch),
            BASE_SOURCE,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )
    outcome = _process(_coordinator(lambda _request, _token: json.dumps(mismatch)), BASE_SOURCE)
    assert outcome.status == "review"
    assert outcome.reason_codes == ("extractor_evidence_mismatch",)

    out_of_bounds = json.loads(_posted(BASE_SOURCE))
    out_of_bounds["account"]["evidence"]["end_scalar"] = len(BASE_SOURCE) + 1
    with pytest.raises(ExtractionValidationError, match="extractor_evidence_out_of_bounds"):
        parse_and_normalize_extraction(
            json.dumps(out_of_bounds),
            BASE_SOURCE,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"decision":"none","decision":"abstain"}', "extractor_duplicate_json_key"),
        (
            '{"decision":"posted","amount":{"value":"1","value":"2"}}',
            "extractor_duplicate_json_key",
        ),
        (
            '{"decision":"posted","amount":{"value":"1","currency":"INR","evidence":{"start_scalar":0,"start_scalar":1}}}',
            "extractor_duplicate_json_key",
        ),
        ('{"decision":"none"} trailing', "extractor_extra_content"),
        ('```json\n{"decision":"none"}\n```', "extractor_malformed_json"),
        ('[{"decision":"none"}]', "extractor_output_not_object"),
        ('{"decision":NaN}', "extractor_malformed_json"),
        ('{"decision":123}', "extractor_decision_type_invalid"),
        ('{"decision":"maybe"}', "extractor_unknown_decision"),
        ('{"decision":"none","amount":null}', "extractor_non_posted_extra_fields"),
        (" " * (RAW_OUTPUT_UTF8_BYTE_LIMIT + 1), "runtime_output_truncated"),
        ("\ud800", "extractor_malformed_json"),
    ],
)
def test_strict_json_decoding_limits_and_decision_guards_fail_closed(
    raw: str, reason: str
) -> None:
    with pytest.raises(ExtractionValidationError, match=reason):
        parse_and_normalize_extraction(
            raw,
            "Synthetic source",
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )


def test_exact_utf8_byte_limit_passes() -> None:
    core = '{"decision":"none"}'
    padded = core + (" " * (RAW_OUTPUT_UTF8_BYTE_LIMIT - len(core)))
    assert len(padded.encode("utf-8")) == RAW_OUTPUT_UTF8_BYTE_LIMIT
    assert parse_and_normalize_extraction(
        padded,
        "Synthetic source",
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    ) == ExtractionResult("none")


@pytest.mark.parametrize(
    "source",
    [
        "😀 INR 1,250.00 was debited from account XX1234.",
        "e\u0301 INR 1,250.00 was debited from account XX1234.",
        "Ａ INR 1,250.00 was debited from account XX1234.",
    ],
)
def test_unicode_scalar_spans_cover_emoji_combining_marks_and_fullwidth_text(source: str) -> None:
    result = parse_and_normalize_extraction(
        _posted(source),
        source,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    assert result.transaction is not None
    span = result.transaction.amount_span
    assert source[span.start_scalar : span.end_scalar] == span.text
    assert SourceSpan.from_source(source, span.start_scalar, span.end_scalar) == span


def test_escaped_json_preserves_unicode_scalar_span_and_counterparty_normalization() -> None:
    source = "INR 10.00 was credited to account XX1234 from CAF\u00c9   MART."
    payload = _posted_dict(
        source,
        amount_text="INR 10.00",
        amount_value="10.00",
        direction_text="credited",
        direction_value="credit",
        account_text="XX1234",
        counterparty={
            "value": "CAF\u00c9   MART",
            "evidence": _span(source, "CAF\u00c9   MART"),
        },
    )
    result = parse_and_normalize_extraction(
        json.dumps(payload, ensure_ascii=True),
        source,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    assert result.transaction is not None
    assert result.transaction.counterparty == "caf\u00e9 mart"
    assert normalize_counterparty("  ") == ""
    assert normalize_counterparty("a" * 257) == ""


@pytest.mark.parametrize(
    ("amount_text", "declared", "reason"),
    [
        ("INR 1.234", "1.234", "extractor_amount_invalid"),
        ("INR 92233720368547758.08", "92233720368547758.08", "extractor_amount_invalid"),
        ("INR 1,250.00", "1251.00", "extractor_amount_value_disagreement"),
        ("INR 1,250.00", "01250.00", "extractor_amount_invalid"),
    ],
)
def test_money_precision_overflow_and_value_disagreement(
    amount_text: str, declared: str, reason: str
) -> None:
    source = f"{amount_text} was debited from account XX1234."
    payload = _posted_dict(source, amount_text=amount_text, amount_value=declared)
    with pytest.raises(ExtractionValidationError, match=reason):
        parse_and_normalize_extraction(
            json.dumps(payload),
            source,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )


def test_currency_direction_and_account_grounding_guards() -> None:
    # Mismatched currency marker vs declared currency fails closed.
    src_usd = "USD 10.00 was debited from account XX1234."
    bad_currency = _posted_dict(src_usd, amount_text="USD 10.00", amount_value="10.00", currency="INR")
    with pytest.raises(ExtractionValidationError, match="extractor_currency_invalid"):
        parse_and_normalize_extraction(
            json.dumps(bad_currency),
            src_usd,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )

    # Bare amount grounds only to primary_currency, not arbitrary foreign currency.
    src_bare = "Amount 10.00 debited from account XX1234."
    bare_ok = _posted_dict(src_bare, amount_text="10.00", amount_value="10.00", currency="INR")
    assert parse_and_normalize_extraction(
        json.dumps(bare_ok),
        src_bare,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    ).decision == "posted"
    bare_bad = _posted_dict(src_bare, amount_text="10.00", amount_value="10.00", currency="USD")
    with pytest.raises(ExtractionValidationError, match="extractor_currency_invalid"):
        parse_and_normalize_extraction(
            json.dumps(bare_bad),
            src_bare,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )

    # Ungrounded direction cue fails closed.
    bad_dir = _posted_dict(BASE_SOURCE, direction_text="account", direction_value="debit")
    with pytest.raises(ExtractionValidationError, match="extractor_direction_invalid"):
        parse_and_normalize_extraction(
            json.dumps(bad_dir),
            BASE_SOURCE,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )

    # Account reference normalization handles VPAs and single 3..8 digit masked suffixes.
    assert normalize_account_reference("User.Name@SynthBank") == "user.name@synthbank"
    assert normalize_account_reference("Card ••••7788") == "7788"
    assert normalize_account_reference("12") == ""
    assert normalize_account_reference("123456789012") == ""
    assert normalize_account_reference("XX1234 and XX5678") == ""

    bad_acct = _posted_dict(BASE_SOURCE, account_text="XX1234", account_ref="XX9999")
    with pytest.raises(ExtractionValidationError, match="extractor_account_reference_invalid"):
        parse_and_normalize_extraction(
            json.dumps(bad_acct),
            BASE_SOURCE,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )


def test_collect_grounded_extractor_fields_salvages_valid_and_grounded_only_fields() -> None:
    # Amount is valid, direction is grounded_only (wrong verb), account is grounded_only (wrong ref).
    partial = _posted_dict(
        BASE_SOURCE,
        direction_text="account",
        direction_value="debit",
        account_text="XX1234",
        account_ref="XX9999",
    )
    fields = collect_grounded_extractor_fields(
        json.dumps(partial),
        BASE_SOURCE,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    by_field = {item.field: item for item in fields}
    assert by_field["amount"].validation_state == "valid"
    assert by_field["amount"].normalized_value == {"minor_units": 125_000, "currency": "INR"}
    assert by_field["direction"].validation_state == "grounded_only"
    assert by_field["direction"].normalized_value is None
    assert by_field["account"].validation_state == "grounded_only"
    assert by_field["account"].normalized_value is None

    # Coordinator preserves salvaged fields on ReviewCase without promoting invalid output.
    outcome = _process(_coordinator(lambda *_args: json.dumps(partial)), BASE_SOURCE)
    assert outcome.status == "review"
    assert outcome.result is None
    assert outcome.review_case is not None
    slm_evidence = [item for item in outcome.review_case.field_evidence if item.origin == "slm"]
    assert {item.field for item in slm_evidence} == {"amount", "direction", "account"}

    # Malformed JSON or non-posted output yields no salvaged fields.
    assert collect_grounded_extractor_fields(
        "{not json", BASE_SOURCE, primary_currency="INR", enabled_profile_ids=("core-en",)
    ) == ()
    assert collect_grounded_extractor_fields(
        '{"decision":"none"}', BASE_SOURCE, primary_currency="INR", enabled_profile_ids=("core-en",)
    ) == ()

    # ExtractorFieldEvidence enforces provenance invariants.
    span = SourceSpan.from_source(BASE_SOURCE, 0, 3)
    with pytest.raises(ValueError, match="analyzer suggestion"):
        ExtractorFieldEvidence("amount", span, None, "suggestion", "extractor_validation", origin="slm")
    with pytest.raises(ValueError, match="labeled suggestion"):
        ExtractorFieldEvidence(
            "amount", span, None, "valid", "analysis_advisory", origin="advisory_analyzer"
        )


def test_project_apple_fm_flat_output_handles_posted_non_posted_and_rejects_malformed() -> None:
    base_flat: dict[str, object] = {
        key: ("" if key in APPLE_STRING_FIELDS else 0) for key in APPLE_FLAT_FIELDS
    }
    assert project_apple_fm_flat_output({**base_flat, "decision": "none"}) == {"decision": "none"}
    assert project_apple_fm_flat_output({**base_flat, "decision": "abstain"}) == {
        "decision": "abstain"
    }

    posted_flat = {
        **base_flat,
        "decision": "posted",
        "amount_value": "1250.00",
        "amount_currency": "INR",
        "amount_start": 0,
        "amount_end": 12,
        "amount_text": "INR 1,250.00",
        "direction_value": "debit",
        "direction_start": 17,
        "direction_end": 24,
        "direction_text": "debited",
        "account_reference": "XX1234",
        "account_start": 38,
        "account_end": 44,
        "account_text": "XX1234",
    }
    projected = project_apple_fm_flat_output(posted_flat)
    assert projected["counterparty"] is None
    assert parse_and_normalize_extraction(
        json.dumps(projected),
        BASE_SOURCE,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    ).decision == "posted"

    with pytest.raises(ValueError, match="non-posted"):
        project_apple_fm_flat_output({**base_flat, "decision": "none", "amount_value": "5.00"})
    with pytest.raises(ValueError, match="offset type"):
        project_apple_fm_flat_output({**posted_flat, "amount_start": True})
    with pytest.raises(ValueError, match="fields are invalid"):
        project_apple_fm_flat_output({"decision": "none"})


def test_account_resolution_ownership_and_five_minute_duplicate_boundaries() -> None:
    assert resolve_account(None, ()).status == ResolutionStatus.MISSING
    assert resolve_account("XX1234", ()).status == ResolutionStatus.UNRESOLVED

    unowned = AccountCatalogEntry("acct-other", "bank_account", ("XX1234",), owned=False)
    owned = AccountCatalogEntry("acct-1", "bank_account", ("A/c **1234",))
    unique = resolve_account("XX1234", (unowned, owned))
    assert unique.status == ResolutionStatus.UNIQUELY_RESOLVED
    assert unique.account_id == "acct-1"

    vpa_entry = AccountCatalogEntry("acct-vpa", "vpa", ("SynthUser@SynthBank",))
    vpa_resolved = resolve_account("synthuser@synthbank", (vpa_entry,))
    assert vpa_resolved.status == ResolutionStatus.UNIQUELY_RESOLVED
    assert vpa_resolved.account_id == "acct-vpa"

    ambiguous = resolve_account(
        "XX1234",
        (owned, AccountCatalogEntry("acct-2", "card", ("XX1234",))),
    )
    assert ambiguous.status == ResolutionStatus.AMBIGUOUS
    assert ambiguous.account_id is None

    prior = RecentTransactionRecord(
        account_id="acct-1",
        direction="debit",
        minor_units=125_000,
        currency="INR",
        received_at_epoch_ms=1_000_000,
        source_id="src-1",
        operation_id="op-1",
    )
    # Exact 300,000 ms boundary matches; 300,001 ms does not.
    assert is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id="acct-1",
        received_at_epoch_ms=1_000_000 + DUPLICATE_WINDOW_MS,
        recent_transactions=(prior,),
    )
    assert not is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id="acct-1",
        received_at_epoch_ms=1_000_000 + DUPLICATE_WINDOW_MS + 1,
        recent_transactions=(prior,),
    )
    # Opposite direction, different account, self source/operation, or None account never collide.
    assert not is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="credit",
        account_id="acct-1",
        received_at_epoch_ms=1_000_000,
        recent_transactions=(prior,),
    )
    assert not is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id="acct-2",
        received_at_epoch_ms=1_000_000,
        recent_transactions=(prior,),
    )
    assert not is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id="acct-1",
        received_at_epoch_ms=1_000_000,
        recent_transactions=(prior,),
        current_source_id="src-1",
    )
    assert not is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id="acct-1",
        received_at_epoch_ms=1_000_000,
        recent_transactions=(prior,),
        current_operation_id="op-1",
    )
    assert not is_within_duplicate_window(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id=None,
        received_at_epoch_ms=1_000_000,
        recent_transactions=(prior,),
    )

    fp = hashlib.sha256(b"synthetic").hexdigest()
    assert assess_duplicate(
        idempotency_key="k1",
        source_event_key="s1",
        transaction_fingerprint_value=fp,
        known_transaction_fingerprints=frozenset({fp}),
    ).status == DuplicateStatus.POSSIBLE_DUPLICATE
    assert assess_duplicate(
        idempotency_key="k1",
        source_event_key="s2",
        transaction_fingerprint_value=fp,
        persisted_idempotency_keys=frozenset({"k1"}),
    ).status == DuplicateStatus.ALREADY_PERSISTED
    assert assess_duplicate(
        idempotency_key="k2",
        source_event_key="s1",
        transaction_fingerprint_value=fp,
        persisted_source_event_keys=frozenset({"s1"}),
    ).status == DuplicateStatus.ALREADY_PERSISTED


def test_evaluate_persistence_enforces_all_integrity_semantic_and_mode_gates() -> None:
    parsed = parse_and_normalize_extraction(
        _posted(BASE_SOURCE),
        BASE_SOURCE,
        primary_currency="INR",
        enabled_profile_ids=("core-en", "india"),
    )
    resolution = resolve_account("XX1234", (AccountCatalogEntry("acct-1", "bank_account", ("XX1234",)),))
    fp = transaction_fingerprint(
        minor_units=125_000,
        currency="INR",
        direction="debit",
        account_id="acct-1",
        received_at_epoch_ms=RECEIVED_AT,
    )
    clear_dup = assess_duplicate(
        idempotency_key="k1", source_event_key="s1", transaction_fingerprint_value=fp
    )
    base_ctx = PersistenceContext(
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        account_resolution=resolution,
        duplicate_assessment=clear_dup,
        rollout_mode="automatic",
        extractor_mode_valid=True,
        claim_ownership_current=True,
        configuration_hash_matches=True,
        operation_integrity_valid=True,
    )
    ok = evaluate_persistence(parsed, base_ctx)
    assert ok.result == GateResult.ELIGIBLE and ok.safe_to_persist

    blocked = evaluate_persistence(parsed, replace(base_ctx, rollout_mode="shadow"))
    assert blocked.result == GateResult.BLOCKED_BY_MODE
    assert blocked.primary_reason == "persistence_blocked_by_rollout_mode"

    bad_integrity = evaluate_persistence(parsed, replace(base_ctx, operation_integrity_valid=False))
    assert bad_integrity.result == GateResult.INVALID_OPERATION
    assert bad_integrity.primary_reason == "persistence_operation_integrity_invalid"

    bad_hash = evaluate_persistence(parsed, replace(base_ctx, configuration_hash_matches=False))
    assert bad_hash.result == GateResult.INVALID_OPERATION
    assert bad_hash.primary_reason == "persistence_configuration_hash_mismatch"

    bad_claim = evaluate_persistence(parsed, replace(base_ctx, claim_ownership_current=False))
    assert bad_claim.result == GateResult.INVALID_OPERATION
    assert bad_claim.primary_reason == "persistence_claim_ownership_invalid"

    bad_mode = evaluate_persistence(parsed, replace(base_ctx, extractor_mode_valid=False))
    assert bad_mode.result == GateResult.REVIEW_REQUIRED
    assert bad_mode.primary_reason == "persistence_extractor_mode_invalid"

    not_posted = evaluate_persistence(ExtractionResult("none"), base_ctx)
    assert not_posted.result == GateResult.NOT_POSTED


def test_coordinator_tombstones_prefilter_outgoing_invalid_and_abstain() -> None:
    calls: list[dict] = []

    def model(payload, _token):
        calls.append(payload)
        return '{"decision":"none"}'

    # Prefilter excludes OTP with body purged and zero model calls.
    excluded = _process(_coordinator(model), "Your synthetic OTP is 123456.")
    assert excluded.status == "tombstoned_excluded"
    assert excluded.model_invoked is False
    assert excluded.body_retained is False
    assert excluded.review_case is None
    assert calls == []

    # Bypassing prefilter invokes the model and settles valid none as tombstoned_none.
    bypass = _process(_coordinator(model, apply_prefilter=False), "Your synthetic OTP is 123456.")
    assert bypass.status == "tombstoned_none"
    assert bypass.model_invoked is True
    assert bypass.body_retained is False
    assert len(calls) == 1

    # Invalid and outgoing inputs bypass model with tombstoned_excluded.
    outgoing = _process(_coordinator(model), "Synthetic outgoing message.", is_outgoing=True)
    invalid = _process(_coordinator(model), "", input_valid=False)
    assert outgoing.status == invalid.status == "tombstoned_excluded"
    assert outgoing.reason_codes == ("reliable_outgoing_metadata",)
    assert invalid.reason_codes == ("invalid_input",)

    # Abstain retains review.
    abstain = _process(
        _coordinator(lambda *_args: '{"decision":"abstain"}'),
        "INR 500.00 balance available in account XX1234.",
    )
    assert abstain.status == "review"
    assert abstain.review_case is not None
    assert abstain.reason_codes == ("extractor_abstained",)


def test_coordinator_receipt_timestamp_provenance_and_time_field_rejection() -> None:
    payload = json.loads(_posted(BASE_SOURCE))
    payload["transaction_time"] = "2026-01-01"
    with pytest.raises(ExtractionValidationError, match="extractor_posted_field_set_invalid"):
        parse_and_normalize_extraction(
            json.dumps(payload),
            BASE_SOURCE,
            primary_currency="INR",
            enabled_profile_ids=("core-en", "india"),
        )

    catalog = (AccountCatalogEntry("acct-1", "bank_account", ("XX1234",)),)
    coord = _coordinator(lambda *_args: _posted(BASE_SOURCE), catalog=catalog)
    platform = _process(coord, BASE_SOURCE)
    assert platform.status == "eligible"
    assert platform.received_at_epoch_ms == RECEIVED_AT
    assert platform.received_timestamp_provenance == TimestampProvenance.PLATFORM_RECEIVED

    historical = coord.process(
        source=BASE_SOURCE,
        raw_sender="AD-SYNTH1",
        operation_id="11111111-1111-4111-8111-111111111111",
        operation_config_hash=CONFIG_HASH,
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.ACQUISITION_SUPPLIED_MESSAGE_TIME,
        source_event_key="synthetic-source-event-hist",
        idempotency_key="synthetic-idempotency-hist",
    )
    assert historical.status == "eligible"

    invalid_prov = coord.process(
        source=BASE_SOURCE,
        raw_sender="AD-SYNTH1",
        operation_id="11111111-1111-4111-8111-111111111111",
        operation_config_hash=CONFIG_HASH,
        received_at_epoch_ms=RECEIVED_AT,
        received_timestamp_provenance=TimestampProvenance.ADMISSION_TIME,
        source_event_key="synthetic-source-event",
        idempotency_key="synthetic-idempotency",
    )
    assert invalid_prov.status == "review"
    assert "persistence_receipt_timestamp_provenance_invalid" in invalid_prov.reason_codes
    assert invalid_prov.review_case is not None
    assert invalid_prov.review_case.to_dict()["receipt_timestamp"]["provenance"] == "admission_time"


def test_coordinator_runtime_failures_cancellation_and_no_wall_clock_deadline() -> None:
    observed: dict[str, object] = {}

    def slow_runtime(payload, token):
        observed["has_deadline"] = "deadline" in payload
        observed["cancelled"] = token.cancelled
        return _posted(BASE_SOURCE)

    catalog = (AccountCatalogEntry("acct-1", "bank_account", ("XX1234",)),)
    assert _process(_coordinator(slow_runtime, catalog=catalog), BASE_SOURCE).status == "eligible"
    assert observed == {"has_deadline": False, "cancelled": False}

    # Pre-cancelled skips model.
    pre_token = CancellationToken()
    pre_token.cancel()
    calls: list[bool] = []
    pre = _process(
        _coordinator(lambda *_args: calls.append(True) or '{"decision":"none"}'),
        BASE_SOURCE,
        cancellation=pre_token,
    )
    assert pre.status == "interrupted" and pre.model_invoked is False
    assert pre.reason_codes == ("user_cancelled",) and calls == []

    # Post-invoke cancellation fences late output.
    late_token = CancellationToken()

    def late_runtime(_payload, token):
        token.cancel()
        return _posted(BASE_SOURCE)

    late = _process(_coordinator(late_runtime), BASE_SOURCE, cancellation=late_token)
    assert late.status == "interrupted" and late.result is None
    assert late.reason_codes == ("user_cancelled",) and late.review_case is not None

    # Truncation, unavailable, runtime failure, and KeyboardInterrupt route safely.
    for exc, expected_status, expected_reason in (
        (RuntimeTruncatedError(), "review", "runtime_output_truncated"),
        (RuntimeUnavailableError(), "review", "runtime_unavailable"),
        (RuntimeError("synthetic"), "review", "runtime_failure"),
        (KeyboardInterrupt(), "interrupted", "process_interrupted"),
    ):
        def failing(_payload, _token, error=exc):
            raise error

        outcome = _process(_coordinator(failing), "INR 100.00 notice for account XX1234.")
        assert outcome.status == expected_status
        assert outcome.result is None
        assert outcome.reason_codes == (expected_reason,)
        assert outcome.review_case is not None


def test_coordinator_account_and_duplicate_blocks_eligibility() -> None:
    ambiguous_catalog = (
        AccountCatalogEntry("acct-1", "bank_account", ("**1234",)),
        AccountCatalogEntry("acct-2", "card", ("XX1234",)),
    )
    ambiguous = _process(
        _coordinator(lambda *_args: _posted(BASE_SOURCE), catalog=ambiguous_catalog), BASE_SOURCE
    )
    assert ambiguous.status == "review"
    assert "account_resolution_ambiguous" in ambiguous.reason_codes

    unresolved = _process(_coordinator(lambda *_args: _posted(BASE_SOURCE)), BASE_SOURCE)
    assert unresolved.status == "review"
    assert unresolved.account_resolution is not None
    assert unresolved.account_resolution.status == ResolutionStatus.UNRESOLVED
    assert "account_resolution_unresolved" in unresolved.reason_codes

    persisted = _process(
        _coordinator(
            lambda *_args: _posted(BASE_SOURCE),
            catalog=(AccountCatalogEntry("acct-1", "bank_account", ("XX1234",)),),
        ),
        BASE_SOURCE,
        persisted_source_event_keys=frozenset({"synthetic-source-event"}),
    )
    assert persisted.duplicate_assessment is not None
    assert persisted.duplicate_assessment.status == DuplicateStatus.ALREADY_PERSISTED
    assert "duplicate_already_persisted" in persisted.reason_codes
