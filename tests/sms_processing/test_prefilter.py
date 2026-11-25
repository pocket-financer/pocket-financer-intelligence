"""Golden conformance tests for the deterministic SMS prefilter."""

from __future__ import annotations

import json
from pathlib import Path

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
