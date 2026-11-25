"""Golden conformance and edge-case tests for the deterministic SMS prefilter."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from pocketfinancer_sms import cli
from pocketfinancer_sms.prefilter import POLICY_ID, PrefilterResult, prefilter


GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "prefilter-vectors.json"


def test_prefilter_matches_all_golden_vectors() -> None:
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert payload["policy_id"] == POLICY_ID
    cases = payload["vectors"]
    assert len(cases) >= 50
    for case in cases:
        result = prefilter(case["body"])
        assert isinstance(result, PrefilterResult)
        assert result.decision == case["decision"], case["id"]
        assert result.reason == case["reason"], case["id"]


@pytest.mark.parametrize(
    ("body", "expected_decision", "expected_reason"),
    [
        # Future/conditional phrases are stripped before _COMPLETED_GUARD, so requests exclude.
        (
            "Synth Vendor collect request for INR 450. Money will be debited from A/c XX1234 once confirmed.",
            "exclude",
            "collect_or_mandate_request",
        ),
        (
            "OTP is 448822 for transfer scheduled to be debited for INR 500.",
            "exclude",
            "otp_verification",
        ),
        (
            "Your Rs 299 plan has expired; Rs 299 has not been debited yet.",
            "exclude",
            "telecom_notice",
        ),
        # Completed movement rescues messages mentioning OTP/telecom/collect terms.
        (
            "Rs. 299.00 auto-debited from A/c XX4321 for telecom recharge now pack.",
            "pass",
            "eligible",
        ),
        (
            "Payment of INR 799.00 for OTP verified purchase was successful on Card XX5566.",
            "pass",
            "eligible",
        ),
        # Gaming promo takes precedence even if completed terms appear.
        (
            "Rs. 500 credited on WinZO game bonus for account XX1234.",
            "exclude",
            "gaming_promo",
        ),
        # Unicode currency cues and non-ASCII digits pass when financial cue exists.
        (
            "₹ ५००.०० paid at SYNTH CAFE.",
            "pass",
            "eligible",
        ),
        # Brokerage EOD excludes even with currency markers.
        (
            "SYNTH BROKER reported your fund balance Rs 9000.00 on 10-Nov-26.",
            "exclude",
            "brokerage_eod_report",
        ),
        # Multiline DOTALL OTP matching still excludes without completed verb.
        (
            "Security code\nfor login:\n662211\nfor Rs 1000 session.",
            "exclude",
            "otp_verification",
        ),
    ],
)
def test_prefilter_edge_cases_and_rule_precedence(
    body: str, expected_decision: str, expected_reason: str
) -> None:
    result = prefilter(body)
    assert result == PrefilterResult(expected_decision, expected_reason)


def test_prefilter_cli_supports_text_flag_and_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["prefilter", "--text", "Your OTP is 884422. Do not share."]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "decision": "exclude",
        "reason": "otp_verification",
    }

    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO("INR 250.00 debited from A/c XX1234 at SYNTH SHOP."),
    )
    assert cli.main(["prefilter"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "decision": "pass",
        "reason": "eligible",
    }
