"""Unit tests for currency parsing, analyzer profiles, structural text, and DeterministicSmsAnalyzer."""

from __future__ import annotations

from decimal import localcontext

import pytest

from pocketfinancer_sms.analyzer import (
    ANALYSIS_CONTRACT_V1,
    ANALYSIS_CONTRACT_V2,
    DeterministicSmsAnalyzer,
)
from pocketfinancer_sms.currency import (
    MAX_SIGNED_64,
    CurrencyContext,
    parse_money,
)
from pocketfinancer_sms.profiles import CORE_EN, INDIA, resolve_profiles
from pocketfinancer_sms.structural_text import (
    build_structural_view,
    clause_for_span,
    split_clauses,
)
from pocketfinancer_sms.types import (
    Analysis,
    CandidateKind,
    CurrencyProvenance,
    TimestampProvenance,
)


CONFIG_HASH = "a" * 64


def test_currency_context_normalizes_case_and_hashes_profiles() -> None:
    ctx_india = CurrencyContext("inr", ("core-en", "india"))
    assert ctx_india.primary_currency == "INR"
    assert ctx_india.config_hash == CurrencyContext("INR", ("core-en", "india")).config_hash
    assert ctx_india.config_hash != CurrencyContext("USD", ("core-en",)).config_hash

    with pytest.raises(ValueError, match="supported ISO-4217"):
        CurrencyContext("ZZZ")
    with pytest.raises(ValueError, match="unknown analyzer profile"):
        resolve_profiles(("core-en", "nonexistent"))
    assert resolve_profiles(("core-en", "india")) == (CORE_EN, INDIA)


def test_parse_money_handles_zero_decimal_currencies_formatting_and_64bit_bounds() -> None:
    inr = parse_money("1,25,000.50", currency="inr", provenance=CurrencyProvenance.EXPLICIT_CODE)
    assert inr.minor_units == 12_500_050
    assert inr.currency == "INR"

    jpy = parse_money("1,200", currency="JPY", provenance=CurrencyProvenance.EXPLICIT_CODE)
    assert jpy.minor_units == 1200

    with pytest.raises(ValueError, match="precision"):
        parse_money("1200.5", currency="JPY", provenance=CurrencyProvenance.EXPLICIT_CODE)
    with pytest.raises(ValueError, match="unsupported"):
        parse_money("10.00", currency="BRL", provenance=CurrencyProvenance.EXPLICIT_CODE)
    for invalid in ("0", "-5.00", "NaN", "Infinity", "not-a-number"):
        with pytest.raises(ValueError):
            parse_money(invalid, currency="INR", provenance=CurrencyProvenance.EXPLICIT_CODE)

    # Caller decimal precision must never round or overflow valid 64-bit money.
    with localcontext() as ctx:
        ctx.prec = 2
        max_usd = parse_money(
            "92233720368547758.07", currency="USD", provenance=CurrencyProvenance.EXPLICIT_CODE
        )
        assert max_usd.minor_units == MAX_SIGNED_64
        with pytest.raises(ValueError, match="signed 64-bit"):
            parse_money(
                "92233720368547758.08",
                currency="USD",
                provenance=CurrencyProvenance.EXPLICIT_CODE,
            )


def test_structural_view_maps_nfkc_casefold_ligatures_and_clauses_back_to_source() -> None:
    source = "ﬁnal\t\tINR 50.00   debited; INR 10.00 pending."
    view = build_structural_view(source)
    assert view.normalized == "final inr 50.00 debited; inr 10.00 pending."
    # Ligature 'ﬁ' (index 0) expands to 'fi' in normalized text and maps back to index 0..1.
    assert view.source_span(0, 2) == (0, 1)
    assert source[view.source_span(0, 5)[0] : view.source_span(0, 5)[1]] == "ﬁnal"

    with pytest.raises(ValueError, match="outside the structural view"):
        view.source_span(0, 0)
    with pytest.raises(ValueError, match="outside the structural view"):
        view.source_span(0, len(view.normalized) + 1)

    clauses = split_clauses(source)
    assert len(clauses) == 2
    assert clauses[0].evidence.text == "ﬁnal\t\tINR 50.00   debited;"
    assert clauses[1].evidence.text == "INR 10.00 pending."
    assert len(split_clauses("INR 50 debited but INR 10 pending")) == 2
    assert clause_for_span(clauses, 0, 4) == "cl0"
    assert clause_for_span(clauses, 0, len(source)) is None
    assert split_clauses("") == ()
    assert len(split_clauses("   ")) == 1


def test_analyzer_v2_extracts_amounts_cues_rails_and_blocks_non_completed_clauses() -> None:
    analyzer = DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india")),
        analysis_contract=ANALYSIS_CONTRACT_V2,
    )
    source = (
        "Rs. 1,25,000.50 debited from A/c XX4321 at SYNTH MART via UPI; "
        "refund of INR 50.00 is expected tomorrow; fee $10 and BRL 20."
    )
    analysis = analyzer.analyze(
        source,
        operation_id="op-synth-1",
        operation_config_hash=CONFIG_HASH,
        source_timestamp_epoch_ms=1_700_000_000_000,
        source_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
    )

    assert analysis.contract == ANALYSIS_CONTRACT_V2
    amounts = [c for c in analysis.candidates_of(CandidateKind.AMOUNT) if not c.explicit_absence]
    assert {c.value["minor_units"] for c in amounts} == {12_500_050, 5_000}

    directions = analysis.candidates_of(CandidateKind.DIRECTION)
    assert len(directions) == 1
    assert directions[0].value == {"direction": "debit"}

    cue_kinds = {cue.kind for cue in analysis.cues}
    assert {
        "payment_rail",
        "expectation",
        "currency_ambiguous",
        "currency_unsupported",
    } <= cue_kinds
    assert "rail_upi" in analysis.reason_codes
    assert "ambiguous_currency_marker" in analysis.reason_codes
    assert "unsupported_currency_code" in analysis.reason_codes

    # Round-trip rehydration verifies fingerprint, lengths, clauses, and spans.
    rehydrated = Analysis.from_dict(analysis.to_dict(), source=source)
    assert rehydrated == analysis

    redacted = analysis.to_dict(include_source_evidence=False)
    assert all("text" not in clause["evidence"] for clause in redacted["clauses"])
    assert all("text" not in cue["evidence"] for cue in redacted["cues"])


def test_analyzer_validation_guards_bare_amounts_and_from_dict_tampering() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        DeterministicSmsAnalyzer(CurrencyContext("INR"), analysis_contract="invalid/0")

    v2 = DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india")),
        analysis_contract=ANALYSIS_CONTRACT_V2,
    )
    with pytest.raises(ValueError, match="configuration hash"):
        v2.analyze("INR 10 debited.", operation_id="op-1")
    with pytest.raises(ValueError, match="requires a timestamp"):
        v2.analyze(
            "INR 10 debited.",
            operation_id="op-1",
            operation_config_hash=CONFIG_HASH,
            source_timestamp_epoch_ms=None,
            source_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
        )

    # Account identifier digits are excluded from bare amount candidates.
    bare_source = "Amount 250 paid from account 987654 at SYNTH SHOP."
    bare = v2.analyze(
        bare_source,
        operation_id="op-bare",
        operation_config_hash=CONFIG_HASH,
    )
    amounts = [c.value["minor_units"] for c in bare.candidates_of(CandidateKind.AMOUNT)]
    assert amounts == [25_000]

    # Authorization hold blocks direction candidate in the same clause.
    hold = v2.analyze(
        "Authorization hold of INR 500.00 charged on Card XX1234.",
        operation_id="op-hold",
        operation_config_hash=CONFIG_HASH,
    )
    assert hold.candidates_of(CandidateKind.DIRECTION) == ()
    assert "authorization_or_hold_not_posted" in hold.reason_codes

    # Tampered stored analysis fails closed on rehydration.
    serialized = bare.to_dict()
    with pytest.raises(ValueError, match="fingerprint"):
        Analysis.from_dict(serialized, source=bare_source + " ")
    bad_contract = {**serialized, "contract": "pocketfinancer.sms-analysis/99"}
    with pytest.raises(ValueError, match="unsupported"):
        Analysis.from_dict(bad_contract, source=bare_source)

    # V1 analysis remains supported for historical corpus records.
    v1 = DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india")),
        analysis_contract=ANALYSIS_CONTRACT_V1,
    )
    v1_analysis = v1.analyze("INR 20 debited from A/c XX1234.", operation_id="op-v1")
    assert v1_analysis.contract == ANALYSIS_CONTRACT_V1
    assert Analysis.from_dict(v1_analysis.to_dict(), source="INR 20 debited from A/c XX1234.") == v1_analysis
