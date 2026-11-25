"""Tests for GGUF & Apple FM evaluation lanes, scoring/suppression, and stateful pipeline mechanics."""

from __future__ import annotations

import asyncio
import fcntl
import json
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from pocketfinancer_sms import cli, evaluation
from pocketfinancer_sms.account_resolution import DUPLICATE_WINDOW_MS
from pocketfinancer_sms.admission import admission_action
from pocketfinancer_sms.evaluation import (
    AppleFMProvider,
    EvaluationInterrupted,
    ExtractorEvaluationError,
    InventedMessage,
    SuppliedRuntime,
    SyntheticPipelineRun,
    evaluate_apple_foundation_models,
    evaluate_extractor,
)
from pocketfinancer_sms.extractor import APPLE_FLAT_FIELDS
from pocketfinancer_sms.prefilter import prefilter
from pocketfinancer_sms.provenance import PrivateArtifactError


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/sms_processing/fixtures/extractor/synthetic-suite.jsonl"
APPLE_SCHEMA = ROOT / "configs/sms_processing/schemas/apple-fm-flat.schema.json"
STRING_FLAT_FIELDS = frozenset(
    key for key in APPLE_FLAT_FIELDS if not key.endswith(("_start", "_end"))
)


def _cases() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def _flat_gold(case: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        key: "" if key in STRING_FLAT_FIELDS else 0 for key in APPLE_FLAT_FIELDS
    }
    gold = case["gold"]
    value["decision"] = gold["decision"]
    if gold["decision"] != "posted":
        return value
    for field in ("amount", "direction", "account", "counterparty"):
        content = gold.get(field)
        if content is None:
            continue
        if field == "amount":
            value["amount_value"] = content["value"]
            value["amount_currency"] = content["currency"]
        elif field == "direction":
            value["direction_value"] = content["value"]
        elif field == "account":
            value["account_reference"] = content["reference"]
        else:
            value["counterparty_value"] = content["value"]
        span = content["evidence"]
        value[f"{field}_start"] = span["start_scalar"]
        value[f"{field}_end"] = span["end_scalar"]
        value[f"{field}_text"] = span["text"]
    return value


class FakeModel:
    metadata = {"tokenizer.chat_template": "{{ messages }}"}

    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        if isinstance(output, dict):
            return output
        return {
            "choices": [{"message": {"content": output}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }


class FakeFactory:
    def __init__(self, outputs: list[Any], *, metadata: dict[str, str] | None = None) -> None:
        self.outputs = outputs
        self.metadata = metadata
        self.instances: list[FakeModel] = []

    def __call__(self, **kwargs: Any) -> FakeModel:
        model = FakeModel(list(self.outputs))
        if self.metadata is not None:
            model.metadata = self.metadata
        self.instances.append(model)
        return model


class FakeAppleProvider:
    provider_id = "fake-apple-provider"
    runtime_version = "fake-sdk-0"

    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def infer(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _allow_tmp_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation, "require_private_output", lambda _root, path: path.resolve())


def _fake_gguf(tmp_path: Path) -> Path:
    gguf = tmp_path / "synthetic.gguf"
    gguf.write_bytes(b"GGUF-SYNTHETIC")
    return gguf


# ---------------------------------------------------------------------------
# GGUF evaluation lane
# ---------------------------------------------------------------------------


def test_gguf_evaluation_uses_chat_template_greedy_grammar_account_catalog_and_private_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _allow_tmp_private(monkeypatch)
    cases = _cases()
    outputs = [json.dumps(case["gold"], ensure_ascii=False, separators=(",", ":")) for case in cases]
    factory = FakeFactory(outputs)
    grammars: list[Path] = []
    gguf = _fake_gguf(tmp_path)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {"account_id": "acct-1234", "account_type": "bank_account", "aliases": ["XX1234"]},
                    {"account_id": "acct-7788", "account_type": "card", "aliases": ["••••7788"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "gguf_run"
    summary = evaluate_extractor(
        ROOT,
        gguf=gguf,
        suite="synthetic",
        output_dir=output_dir,
        account_catalog=catalog_path,
        apply_prefilter=False,
        model_factory=factory,
        grammar_factory=lambda path: grammars.append(path) or f"grammar:{path.name}",
        runtime_version="fake-llama-cpp",
    )
    assert summary["lane"] == "android_target_gguf"
    assert summary["status"] == "complete"
    assert summary["metrics"]["classification"]["accuracy"] == 1.0
    assert summary["metrics"]["transaction"]["f1"] == 1.0
    assert summary["metrics"]["strict_posted_exact_success"]["rate"] == 1.0
    assert summary["metrics"]["account_resolution"]["eligible_count"] > 0
    assert grammars == [ROOT / "configs/sms_processing/grammars/sms-extractor.gbnf"]
    call = factory.instances[0].calls[0]
    assert call["temperature"] == 0.0
    assert call["grammar"] == "grammar:sms-extractor.gbnf"
    report_path = output_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert cases[0]["source"] not in report_path.read_text(encoding="utf-8")
    assert report["provenance"]["inference"]["per_row_deadline_ms"] == 0
    assert report["capabilities"]["account_resolution"] is True
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600


def test_gguf_prefilter_interruption_resume_and_truncated_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _allow_tmp_private(monkeypatch)
    cases = _cases()
    eligible_outputs = [
        json.dumps(case["gold"], ensure_ascii=False, separators=(",", ":"))
        for case in cases
        if prefilter(case["source"]).decision != "exclude"
    ]
    gguf = _fake_gguf(tmp_path)
    output_dir = tmp_path / "resume_run"
    with pytest.raises(EvaluationInterrupted):
        evaluate_extractor(
            ROOT,
            gguf=gguf,
            suite="synthetic",
            output_dir=output_dir,
            apply_prefilter=True,
            model_factory=FakeFactory([eligible_outputs[0], KeyboardInterrupt()]),
            grammar_factory=lambda _path: None,
            runtime_version="fake-llama-cpp",
        )
    resumed = evaluate_extractor(
        ROOT,
        gguf=gguf,
        suite="synthetic",
        output_dir=output_dir,
        apply_prefilter=True,
        model_factory=FakeFactory(eligible_outputs[1:]),
        grammar_factory=lambda _path: None,
        runtime_version="fake-llama-cpp",
    )
    assert resumed["status"] == "complete"
    assert resumed["metrics"]["prefilter"]["excluded_count"] == len(cases) - len(eligible_outputs)

    # Re-running with mismatched seed against an existing checkpoint fails closed.
    with pytest.raises(ExtractorEvaluationError, match="checkpoint provenance"):
        evaluate_extractor(
            ROOT,
            gguf=gguf,
            suite="synthetic",
            output_dir=output_dir,
            seed=99,
            apply_prefilter=True,
            model_factory=FakeFactory([]),
            grammar_factory=lambda _path: None,
            runtime_version="fake-llama-cpp",
        )

    length_factory = FakeFactory(
        [{"choices": [{"message": {"content": "PRIVATE_PARTIAL"}, "finish_reason": "length"}]}]
        + [json.dumps(case["gold"]) for case in cases[1:]]
    )
    truncated = evaluate_extractor(
        ROOT,
        gguf=gguf,
        suite="synthetic",
        output_dir=tmp_path / "length_run",
        apply_prefilter=False,
        model_factory=length_factory,
        grammar_factory=lambda _path: None,
        runtime_version="fake-llama-cpp",
    )
    assert truncated["metrics"]["failures"]["runtime_failure_count"] == 1
    assert "PRIVATE_PARTIAL" not in (tmp_path / "length_run/report.json").read_text(encoding="utf-8")


def test_gguf_boundary_missing_chat_template_nonempty_dir_and_cli_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(PrivateArtifactError):
        evaluate_extractor(
            ROOT,
            gguf=_fake_gguf(tmp_path),
            suite="synthetic",
            output_dir=Path("configs/sms_processing/bad-output"),
            model_factory=FakeFactory([]),
            grammar_factory=lambda _path: None,
            runtime_version="fake-llama-cpp",
        )

    _allow_tmp_private(monkeypatch)
    with pytest.raises(ExtractorEvaluationError, match="unavailable"):
        evaluate_extractor(
            ROOT,
            gguf=tmp_path / "missing.gguf",
            suite="synthetic",
            output_dir=tmp_path / "missing_run",
            model_factory=FakeFactory([]),
            grammar_factory=lambda _path: None,
            runtime_version="fake-llama-cpp",
        )

    with pytest.raises(ExtractorEvaluationError, match="embedded chat template"):
        evaluate_extractor(
            ROOT,
            gguf=_fake_gguf(tmp_path),
            suite="synthetic",
            output_dir=tmp_path / "no_template_run",
            model_factory=FakeFactory([], metadata={}),
            grammar_factory=lambda _path: None,
            runtime_version="fake-llama-cpp",
        )

    dirty_dir = tmp_path / "dirty_run"
    dirty_dir.mkdir()
    (dirty_dir / "stray.txt").write_text("stray", encoding="utf-8")
    with pytest.raises(ExtractorEvaluationError, match="cannot be reused"):
        evaluate_extractor(
            ROOT,
            gguf=_fake_gguf(tmp_path),
            suite="synthetic",
            output_dir=dirty_dir,
            model_factory=FakeFactory([]),
            grammar_factory=lambda _path: None,
            runtime_version="fake-llama-cpp",
        )

    monkeypatch.setattr(
        cli, "evaluate_extractor", lambda *_args, **_kwargs: (_ for _ in ()).throw(EvaluationInterrupted(2, 9))
    )
    code = cli.main(
        [
            "--repo-root",
            str(ROOT),
            "evaluate-extractor",
            "--gguf",
            str(_fake_gguf(tmp_path)),
            "--suite",
            "synthetic",
            "--output-dir",
            str(tmp_path / "cli_int"),
        ]
    )
    assert code == 130
    assert json.loads(capsys.readouterr().out) == {
        "completed_rows": 2,
        "status": "interrupted",
        "total_rows": 9,
    }


# ---------------------------------------------------------------------------
# Apple Foundation Models lane
# ---------------------------------------------------------------------------


def test_apple_fake_provider_flat_guided_evaluation_and_error_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _allow_tmp_private(monkeypatch)
    cases = _cases()
    provider = FakeAppleProvider([_flat_gold(case) for case in cases])
    summary = evaluate_apple_foundation_models(
        ROOT,
        suite="synthetic",
        output_dir=tmp_path / "apple_run",
        apply_prefilter=False,
        provider=provider,
    )
    assert summary["lane"] == "apple_fm_python"
    assert summary["metrics"]["classification"]["accuracy"] == 1.0
    assert summary["metrics"]["strict_posted_exact_success"]["rate"] == 1.0
    assert provider.calls[0]["schema"] == json.loads(APPLE_SCHEMA.read_text(encoding="utf-8"))

    outputs: list[Any] = [_flat_gold(case) for case in cases]
    outputs[0] = {**outputs[0], "amount_text": "PRIVATE_INVALID_SPAN"}
    outputs[1] = RuntimeError("PRIVATE_RUNTIME_PROMPT_ECHO")
    outputs[2] = {**outputs[2], "unexpected": "PRIVATE_UNKNOWN_FIELD"}
    bad = evaluate_apple_foundation_models(
        ROOT,
        suite="synthetic",
        output_dir=tmp_path / "apple_bad",
        apply_prefilter=False,
        provider=FakeAppleProvider(outputs),
    )
    assert bad["metrics"]["failures"] == {
        "malformed_count": 2,
        "runtime_failure_count": 1,
        "total_count": 3,
    }
    report_text = (tmp_path / "apple_bad/report.json").read_text(encoding="utf-8")
    assert "PRIVATE_INVALID_SPAN" not in report_text
    assert "PRIVATE_RUNTIME_PROMPT_ECHO" not in report_text
    assert "PRIVATE_UNKNOWN_FIELD" not in report_text


def test_apple_lock_contention_locale_guard_and_sdk_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _allow_tmp_private(monkeypatch)
    locked = tmp_path / "locked"
    locked.mkdir()
    with (locked / "run.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ExtractorEvaluationError, match="already in use"):
            evaluate_apple_foundation_models(
                ROOT, suite="synthetic", output_dir=locked, provider=FakeAppleProvider([])
            )

    with pytest.raises(ExtractorEvaluationError, match="configuration is invalid"):
        evaluate_apple_foundation_models(
            ROOT,
            suite="synthetic",
            output_dir=tmp_path / "bad_locale",
            locale="not-a-locale",
            provider=FakeAppleProvider([]),
        )

    if sys.platform != "darwin":
        code = cli.main(
            ["--repo-root", str(ROOT), "evaluate-apple-fm", "--output-dir", str(tmp_path / "nope")]
        )
        assert code == 2
        assert "requires compatible macOS" in json.loads(capsys.readouterr().out)["reason"]

    seen: dict[str, Any] = {}

    class FakeSystemModel:
        def __init__(self, available: bool = True) -> None:
            self._available = available

        def is_available(self) -> tuple[bool, None]:
            return self._available, None

    class FakeContent:
        def __init__(self, complete: bool = True) -> None:
            self.is_complete = complete

        def to_json(self) -> str:
            return json.dumps(_flat_gold(_cases()[0]))

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            seen["session"] = kwargs

        async def respond(self, prompt: str, **kwargs: Any) -> FakeContent:
            seen["prompt"] = prompt
            seen["respond"] = kwargs
            return FakeContent()

    class FakeSDK:
        SystemLanguageModel = FakeSystemModel
        LanguageModelSession = FakeSession
        SamplingMode = type("S", (), {"greedy": staticmethod(lambda: "greedy")})
        GenerationOptions = lambda **kwargs: kwargs  # noqa: E731

    monkeypatch.setattr(evaluation.sys, "platform", "darwin")
    monkeypatch.setattr(evaluation.importlib, "import_module", lambda _name: FakeSDK)
    monkeypatch.setattr(evaluation.importlib.metadata, "version", lambda _name: "0.2.1")
    result = asyncio.run(
        AppleFMProvider().infer(
            instructions="local only", prompt="synthetic", schema={"type": "object"}, max_tokens=512
        )
    )
    assert result["decision"] == "posted"
    assert seen["session"]["instructions"] == "local only"
    assert seen["respond"]["json_schema"] == {"type": "object"}

    # Unavailable model on macOS fails closed.
    class UnavailableSDK(FakeSDK):
        SystemLanguageModel = lambda: FakeSystemModel(available=False)  # noqa: E731

    monkeypatch.setattr(evaluation.importlib, "import_module", lambda _name: UnavailableSDK)
    with pytest.raises(ExtractorEvaluationError, match="unavailable on this Mac"):
        AppleFMProvider()

    # Known Apple SDK error classes map to sanitized reason codes.
    GuardrailError = type("GuardrailViolationError", (Exception,), {})
    assert evaluation._apple_error_code(GuardrailError("raw")) == "apple_guardrail_violation"
    assert evaluation._apple_error_code(ValueError("raw")) == "apple_guided_result_invalid"
    assert evaluation._apple_error_code(RuntimeError("raw")) == "apple_runtime_failure"


# ---------------------------------------------------------------------------
# Scoring, group suppression, committed-label selection, and synthetic grandfathered loader
# ---------------------------------------------------------------------------


def test_private_group_suppression_committed_selection_and_grandfathered_loader(
    tmp_path: Path,
) -> None:
    records = [
        {
            "row_id": f"r{i}",
            "outcome": "none",
            "reason_code": None,
            "prefilter_excluded": False,
            "model_invoked": True,
            "latency_ms": float(i + 1),
            "prediction": {"decision": "none"},
            "gold": {"decision": "none"},
            "groups": {
                "sender_family": "big-family" if i < 5 else "tiny-family",
                "template_family": "big-template" if i < 5 else "tiny-template",
            },
            "account_resolution": None,
        }
        for i in range(7)
    ]
    grouped = evaluation._group_metrics(records, suite="private-canonical")
    assert grouped["available"] is True
    assert list(grouped["sender_family"]["groups"]) == ["big-family"]
    assert grouped["sender_family"]["suppressed_group_count"] == 1
    assert grouped["sender_family"]["suppressed_row_count"] == 2
    assert evaluation._group_metrics(records, suite="grandfathered")["available"] is False

    # Latency quantiles handle empty and populated series.
    assert evaluation._latency_summary([]) == {
        "count": 0,
        "mean": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
    }
    lat = evaluation._latency_summary([10.0, 20.0, 30.0, 40.0])
    assert lat["p50"] == 20.0 and lat["max"] == 40.0

    # Committed annotation selection prefers adjudicated, accepts unanimous submitted, skips conflict.
    sub_a = {"status": "submitted", "revision": 1, "canonical_label": {"decision": "none"}}
    sub_b = {"status": "submitted", "revision": 1, "canonical_label": {"decision": "abstain"}}
    adj = {"status": "adjudicated", "revision": 2, "canonical_label": {"decision": "none"}}
    assert evaluation._select_committed_annotation([sub_a, sub_b]) is None
    assert evaluation._select_committed_annotation([sub_a, sub_a]) == sub_a
    assert evaluation._select_committed_annotation([sub_a, sub_b, adj]) == adj

    # Synthetic grandfathered-format JSONL parses cleanly without touching real fixtures.
    synth_legacy = tmp_path / "legacy.jsonl"
    synth_legacy.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "sender": "AD-SYNTH",
                        "sms": "INR 50 debited from A/c XX1234 at SYNTH SHOP.",
                        "expected": json.dumps(
                            {
                                "amount": 50.0,
                                "type": "debit",
                                "account": "A/c XX1234",
                                "counterparty": "SYNTH SHOP",
                            }
                        ),
                    }
                ),
                json.dumps(
                    {
                        "sender": "AD-SYNTH",
                        "sms": "Your OTP is 123456.",
                        "expected": "null",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded = evaluation._load_grandfathered(synth_legacy, primary_currency="INR")
    assert len(loaded) == 2
    assert loaded[0].gold is not None and loaded[0].gold["minor_units"] == 5_000
    assert loaded[0].gold["spans"] is None
    assert loaded[1].gold == {"decision": "none"}


# ---------------------------------------------------------------------------
# Stateful pipeline mechanics & same-source admission
# ---------------------------------------------------------------------------


BODY = "INR 17.25 was debited from account XX7654."


def _msg(
    source_id: str | None = "invented-1", *, body: str = BODY, time_ms: int = 1000
) -> InventedMessage:
    return InventedMessage(
        body,
        "SYNTHETIC",
        time_ms,
        "invented-connector" if source_id is not None else None,
        source_id,
    )


def _posted_json(body: str = BODY) -> str:
    def span(text: str) -> dict[str, Any]:
        start = body.index(text)
        return {"start_scalar": start, "end_scalar": start + len(text), "text": text}

    return json.dumps(
        {
            "decision": "posted",
            "amount": {"value": "17.25", "currency": "INR", "evidence": span("INR 17.25")},
            "direction": {"value": "debit", "evidence": span("debited")},
            "account": {"reference": "XX7654", "evidence": span("XX7654")},
            "counterparty": None,
        }
    )


def test_shared_admission_vectors_and_retry_guards() -> None:
    vectors = json.loads((Path(__file__).parent / "golden/admission-v1.json").read_text())
    assert vectors["contract"] == "same-source-admission/1"
    for case in vectors["cases"]:
        values = {key: value for key, value in case.items() if key not in {"id", "action"}}
        assert admission_action(**values) == case["action"], case["id"]
    with pytest.raises(ValueError, match="retry requires"):
        admission_action(reliable_identity=True, existing_state=None, owner_retry=True)
    with pytest.raises(ValueError, match="unsupported admission state"):
        admission_action(reliable_identity=True, existing_state="bogus")  # type: ignore[arg-type]


def test_prefilter_and_none_tombstones_purge_bodies_and_reject_retry(tmp_path: Path) -> None:
    run = SyntheticPipelineRun(tmp_path / "run.sqlite")
    try:
        otp = _msg(body="Your OTP is 6824. Do not share it.")
        receipt = run.admit(otp)
        runtime = SuppliedRuntime([])
        assert receipt.route == "tombstoned_excluded"
        assert run.execute(receipt.operation_id, runtime).route == "tombstoned_excluded"
        assert runtime.calls == 0
        op_row = run.db.execute(
            "SELECT body_retained, evidence FROM operations WHERE id = ?",
            (receipt.operation_id,),
        ).fetchone()
        assert op_row["body_retained"] == 0 and op_row["evidence"] is None

        none_msg = _msg(
            "invented-none",
            body="INR 500.00 balance available in account XX7654.",
            time_ms=2000,
        )
        none_receipt = run.admit(none_msg)
        none_runtime = SuppliedRuntime(['{"decision":"none"}'])
        assert run.execute(none_receipt.operation_id, none_runtime).route == "tombstoned_none"
        none_row = run.db.execute(
            "SELECT body_retained, evidence FROM operations WHERE id = ?",
            (none_receipt.operation_id,),
        ).fetchone()
        assert none_row["body_retained"] == 0 and none_row["evidence"] is None
        with pytest.raises(ValueError, match="tombstoned"):
            run.admit(none_msg, retry_of=none_receipt.operation_id)

        # Altered evidence under the same reliable source identity raises conflict.
        with pytest.raises(ValueError, match="conflicts"):
            run.admit(_msg("invented-none", body="INR 999.00 balance in XX7654.", time_ms=2000))
    finally:
        run.close()


def test_five_minute_duplicate_window_and_resolution_actions(tmp_path: Path) -> None:
    run = SyntheticPipelineRun(tmp_path / "run.sqlite")
    try:
        assert DUPLICATE_WINDOW_MS == 300_000
        runtime = SuppliedRuntime([_posted_json(), _posted_json(), _posted_json(), _posted_json()])

        first = run.admit(_msg("id-1", time_ms=100_000))
        assert run.execute(first.operation_id, runtime).route == "review"
        run.confirm_account(first.operation_id, "acct-1")

        within_window = run.admit(_msg("id-2", time_ms=100_000 + DUPLICATE_WINDOW_MS - 1))
        assert run.execute(within_window.operation_id, runtime).route == "review"
        payload = json.loads(
            run.db.execute(
                "SELECT payload FROM operations WHERE id = ?", (within_window.operation_id,)
            ).fetchone()[0]
        )
        assert payload["reason_codes"] == ["duplicate_possible"]

        with pytest.raises(ValueError, match="separate"):
            run.resolve_duplicate(within_window.operation_id, action="invalid")
        run.resolve_duplicate(within_window.operation_id, action="separate")
        assert run.mechanics_summary()["transactions"] == 2

        dismiss_dup = run.admit(_msg("id-3", time_ms=100_000 + DUPLICATE_WINDOW_MS))
        assert run.execute(dismiss_dup.operation_id, runtime).route == "review"
        run.resolve_duplicate(dismiss_dup.operation_id, action="dismiss")
        dismissed_row = run.db.execute(
            "SELECT route, body_retained, evidence FROM operations WHERE id = ?",
            (dismiss_dup.operation_id,),
        ).fetchone()
        assert dismissed_row["route"] == "tombstoned_none"
        assert dismissed_row["body_retained"] == 0 and dismissed_row["evidence"] is None

        outside_window = run.admit(_msg("id-4", time_ms=100_000 + 2 * DUPLICATE_WINDOW_MS))
        assert run.execute(outside_window.operation_id, runtime).route == "transactions"
        assert run.mechanics_summary()["transactions"] == 3
    finally:
        run.close()


def test_crash_recovery_concurrency_and_resume_config_guard(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    run = SyntheticPipelineRun(path)
    seed = run.admit(_msg("seed", time_ms=1_000))
    run.execute(seed.operation_id, SuppliedRuntime([_posted_json()]))
    run.confirm_account(seed.operation_id, "acct-1")
    op_inflight = run.admit(_msg("inflight", time_ms=500_000)).operation_id
    run.db.execute("UPDATE operations SET state = 'in_flight' WHERE id = ?", (op_inflight,))

    # Resuming with mismatched primary currency fails closed.
    with pytest.raises(ValueError, match="configuration mismatch"):
        SyntheticPipelineRun(path, primary_currency="USD", resume=True)

    other = SyntheticPipelineRun(path, resume=True)
    assert other.admit(_msg("inflight", time_ms=500_000)).action == "in_flight"
    assert other.recover_interrupted() == 1
    assert other.admit(_msg("inflight", time_ms=500_000)).route == "retryable"
    child = other.admit(_msg("inflight", time_ms=500_000), retry_of=op_inflight)
    assert other.execute(child.operation_id, SuppliedRuntime([_posted_json()])).route == "transactions"
    run.close()
    other.close()

    barrier = Barrier(2)

    class SynchronizedRuntime(SuppliedRuntime):
        def __call__(self, request: dict[str, Any], cancellation: Any) -> str:
            barrier.wait(timeout=10)
            return super().__call__(request, cancellation)

    def execute_one(source_id: str) -> str:
        lane = SyntheticPipelineRun(path, resume=True)
        try:
            rec = lane.admit(_msg(source_id, time_ms=1_000_000))
            return lane.execute(rec.operation_id, SynchronizedRuntime([_posted_json()])).route
        finally:
            lane.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        routes = sorted(pool.map(execute_one, ["race-a", "race-b"]))
    assert routes == ["review", "transactions"]
