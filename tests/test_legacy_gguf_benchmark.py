"""Synthetic unit tests for the legacy lm-eval GGUF benchmark helpers (DATA/utils.py)."""

from __future__ import annotations

import json

from DATA import utils


def test_prompt_builder_places_synthetic_query_after_few_shot_block() -> None:
    document = {
        "sender": "ZZ-TESTBK",
        "sms": "Synthetic notice: Rs.12.50 debited from Card XX0000 at DEMO STORE.",
    }

    prompt = utils.doc_to_text(document)
    task = prompt.split("### YOUR TASK", maxsplit=1)[1]

    assert "### EXAMPLES" in prompt
    assert "Sender: ZZ-TESTBK" in task
    assert document["sms"] in task
    assert prompt.endswith("Output: ")


def test_response_filters_strip_thinking_and_reject_missing_required_fields() -> None:
    expected = {
        "amount": 12.5,
        "counterparty": "DEMO STORE",
        "type": "debit",
        "account": "Card XX0000",
    }
    response = f"<think>synthetic reasoning</think>\nResult: {json.dumps(expected)}"

    filtered = utils.extract_json_filter([[response]], [{}])
    assert json.loads(filtered[0][0]) == expected

    null_filtered = utils.extract_json_filter([["<think>not a txn</think>\nnull"]], [{}])
    assert null_filtered == [["null"]]

    incomplete = {**expected, "account": None}
    nonnull = utils.extract_json_nonnull_filter([[json.dumps(incomplete)]], [{}])
    assert nonnull == [["null"]]

    string_null = {**expected, "type": "null"}
    assert utils.extract_json_nonnull_filter([[json.dumps(string_null)]], [{}]) == [["null"]]


def test_account_and_counterparty_normalizers_and_metrics() -> None:
    assert utils._normalize_account("A/c XXXXXXXX6254") == ("account", "6254")
    assert utils._normalize_account("Credit Card ending 3782") == ("card", "3782")
    assert utils._normalize_account("no digits") is None
    assert utils._counterparty_match("DEMO SHOP", "demo   shop") is True
    assert utils._counterparty_match("VPA user@synthbank", "user@synthbank") is True
    assert utils._counterparty_match("A", "ABC Corp") is False
    assert utils._counterparty_match(None, "DEMO SHOP") is False

    reference = json.dumps(
        {
            "amount": 12.5,
            "counterparty": "DEMO STORE",
            "type": "debit",
            "account": "Card XX0000",
        }
    )
    prediction = json.dumps(
        {
            "amount": "12.50",
            "counterparty": "demo store",
            "type": "DEBIT",
            "account": "Credit Card ending 0000",
        }
    )
    assert utils.full_match_accuracy([reference], [prediction]) == 1.0
    assert utils.ghost_transaction_rate(["null"], [prediction]) == 1.0
    assert utils.ghost_transaction_rate(["null"], ["null"]) == 0.0
    assert utils.missed_transaction_rate([reference], ["null"]) == 1.0
    assert utils.missed_transaction_rate([reference], [prediction]) == 0.0

    leaked = json.dumps(
        {
            "amount": 500.0,
            "counterparty": "demouser000@examplebank",
            "type": "credit",
            "account": "a/c XXXXXX0000",
        }
    )
    assert utils.few_shot_leakage_rate(["null"], [leaked]) == 1.0
    assert utils.few_shot_leakage_rate(["null"], [prediction]) == 0.0
    assert utils.few_shot_leakage_rate([reference], [leaked]) == 0.0
