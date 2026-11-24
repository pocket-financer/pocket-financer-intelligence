"""Unified private-safe, resumable end-to-end SMS pipeline evaluation.

Supports three local runtime lanes over the same deterministic host boundary:
1. GGUF / llama-cpp-python (Android target runtime)
2. Apple Foundation Models SDK on macOS (iOS target runtime)
3. Isolated stateful SQLite supplied-response mechanics runner
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sqlite3
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Protocol

from .account_resolution import (
    DUPLICATE_WINDOW_MS,
    AccountCatalogEntry,
    RecentTransactionRecord,
    is_within_duplicate_window,
    normalize_account_alias,
    resolve_account,
    transaction_fingerprint,
)
from .analyzer import ANALYSIS_CONTRACT_V2, DeterministicSmsAnalyzer
from .corpus.grouping import sender_family
from .currency import ISO_MINOR_UNITS, MAX_SIGNED_64, CurrencyContext
from .extractor import (
    APPLE_FLAT_FIELDS,
    ExtractionValidationError,
    build_extractor_input,
    normalize_account_reference,
    normalize_counterparty,
    parse_and_normalize_extraction,
    project_apple_fm_flat_output,
)
from .prefilter import POLICY_ID as PREFILTER_POLICY_ID
from .prefilter import prefilter
from .processing import ExtractionCoordinator, processing_result_payload
from .provenance import (
    PrivateArtifactError,
    atomic_write_json,
    ensure_private_directory,
    file_sha256,
    object_sha256,
    require_private_output,
)
from .types import Analysis, CandidateKind, TimestampProvenance


EVALUATION_CONTRACT = "pocketfinancer.sms-extractor-evaluation/2"
SCORING_VERSION = "pocketfinancer.sms-model-quality-scoring/2"
RUN_CONTRACT = "synthetic-stateful-pipeline/1"
SUITE_MANIFEST_PATH = Path("configs/sms_processing/evaluations/model-quality.json")
PROMPT_PATH = Path("configs/sms_processing/prompts/sms-extractor.txt")
APPLE_PROMPT_PATH = Path("configs/sms_processing/prompts/apple-fm-sms-extractor.txt")
GRAMMAR_PATH = Path("configs/sms_processing/grammars/sms-extractor.gbnf")
SCHEMA_PATH = Path("configs/sms_processing/schemas/sms-extractor.schema.json")
APPLE_SCHEMA_PATH = Path("configs/sms_processing/schemas/apple-fm-flat.schema.json")
PROFILE_PATH = Path("configs/sms_processing/profiles/extractor-validation.json")
SYNTHETIC_PATH = Path("tests/sms_processing/fixtures/extractor/synthetic-suite.jsonl")
GRANDFATHERED_PATH = Path("DATA/extraction_ds.jsonl")

_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_LOCALE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}$")
_SMALL_GROUP_MINIMUM = 5
_ATOMIC_TEMP = re.compile(r"^\.(?:checkpoint|report)\.json\.[A-Za-z0-9_-]+$")


class ExtractorEvaluationError(PrivateArtifactError):
    """A fail-closed evaluation error whose message contains no row data."""


class EvaluationInterrupted(ExtractorEvaluationError):
    """The user interrupted evaluation after completed rows were checkpointed."""

    def __init__(self, completed_rows: int, total_rows: int) -> None:
        self.completed_rows = completed_rows
        self.total_rows = total_rows
        super().__init__("extractor evaluation interrupted; completed rows were preserved")


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    row_id: str
    source: str
    sender_family: str
    gold: Mapping[str, Any] | None
    analysis: Analysis | None
    analysis_mode: str
    groups: Mapping[str, str]
    fingerprint_binding: Mapping[str, Any]


class AppleProvider(Protocol):
    provider_id: str
    runtime_version: str

    async def infer(
        self, *, instructions: str, prompt: str, schema: Mapping[str, Any], max_tokens: int
    ) -> Mapping[str, Any]: ...


class AppleFMProvider:
    """Official macOS SDK adapter; no import or model access occurs outside macOS."""

    provider_id = "apple-fm-sdk-system-language-model-general"

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise ExtractorEvaluationError(
                "live Apple Foundation Models inference requires compatible macOS; "
                "non-macOS hosts can run only fake-provider tests"
            )
        try:
            fm = importlib.import_module("apple_fm_sdk")
            model = fm.SystemLanguageModel()
            available, _reason = model.is_available()
        except (ImportError, OSError) as exc:
            raise ExtractorEvaluationError(
                "apple-fm-sdk is unavailable; install the optional macOS dependency"
            ) from exc
        except Exception as exc:
            raise ExtractorEvaluationError("Apple Foundation Models initialization failed") from exc
        if not available:
            raise ExtractorEvaluationError(
                "Apple Foundation Models is unavailable on this Mac; check Apple Intelligence, "
                "Xcode, and model download"
            )
        self.fm = fm
        self.model = model
        try:
            self.runtime_version = importlib.metadata.version("apple-fm-sdk")
        except importlib.metadata.PackageNotFoundError:
            self.runtime_version = "unknown"

    async def infer(
        self, *, instructions: str, prompt: str, schema: Mapping[str, Any], max_tokens: int
    ) -> Mapping[str, Any]:
        session = self.fm.LanguageModelSession(instructions=instructions, model=self.model)
        options = self.fm.GenerationOptions(
            sampling=self.fm.SamplingMode.greedy(), maximum_response_tokens=max_tokens
        )
        generated = await session.respond(prompt, json_schema=dict(schema), options=options)
        if not generated.is_complete:
            raise ValueError("incomplete guided output")
        return json.loads(generated.to_json(), object_pairs_hook=_reject_duplicate_keys)


def _locked_evaluation(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(func)
    def run(repo_root: Path, **kwargs: Any) -> dict[str, Any]:
        root = repo_root.resolve()
        output = require_private_output(root, _resolve_user_path(root, kwargs["output_dir"]))
        ensure_private_directory(output)
        lock_path = output / "run.lock"
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise ExtractorEvaluationError("evaluation output lock is invalid") from exc
        with os.fdopen(descriptor, "a+b") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ExtractorEvaluationError("evaluation output is already in use") from exc
            try:
                return func(repo_root, **kwargs)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    return run


@_locked_evaluation
def evaluate_extractor(
    repo_root: Path,
    *,
    gguf: Path,
    suite: str,
    output_dir: Path,
    account_catalog: Path | None = None,
    primary_currency: str = "INR",
    enabled_profile_ids: Sequence[str] = ("core-en", "india"),
    n_ctx: int = 4096,
    n_gpu_layers: int = -1,
    seed: int = 0,
    max_tokens: int = 512,
    grammar_enabled: bool = True,
    apply_prefilter: bool = True,
    model_factory: Callable[..., Any] | None = None,
    grammar_factory: Callable[[Path], Any] | None = None,
    runtime_version: str | None = None,
) -> dict[str, Any]:
    """Evaluate one local GGUF and return only aggregate-safe result metadata."""

    root = repo_root.resolve()
    if suite not in {"synthetic", "grandfathered", "private-canonical"}:
        raise ExtractorEvaluationError("extractor evaluation suite is unsupported")
    if n_ctx <= 0 or max_tokens <= 0 or not enabled_profile_ids:
        raise ExtractorEvaluationError("extractor evaluation configuration is invalid")
    model_path = gguf.expanduser().resolve()
    if not model_path.is_file():
        raise ExtractorEvaluationError("local GGUF is unavailable")
    private_output = require_private_output(root, _resolve_user_path(root, output_dir))
    ensure_private_directory(private_output)
    _refuse_unsafe_output_reuse(private_output)

    prompt_path = _required_asset(root, PROMPT_PATH, "extractor prompt")
    grammar_path = _required_asset(root, GRAMMAR_PATH, "extractor grammar")
    schema_path = _required_asset(root, SCHEMA_PATH, "extractor schema")
    profile_path = _required_asset(root, PROFILE_PATH, "extractor validation profile")
    prompt = _read_text(prompt_path, "extractor prompt")
    manifest, manifest_hash = _load_evaluation_manifest(root, suite)
    rows, capabilities, dataset_fingerprint = _load_suite(
        root, suite, primary_currency=primary_currency
    )
    _require_unique_rows(rows)
    catalog, catalog_hash = _load_account_catalog(root, account_catalog)

    llama_module = None
    if model_factory is None or (grammar_enabled and grammar_factory is None) or runtime_version is None:
        try:
            llama_module = importlib.import_module("llama_cpp")
        except ImportError as exc:
            raise ExtractorEvaluationError("local llama-cpp-python runtime is unavailable") from exc
    if runtime_version is None:
        runtime_version = str(getattr(llama_module, "__version__", "unknown"))
    assets = {
        "gguf_sha256": file_sha256(model_path),
        "prompt_sha256": file_sha256(prompt_path),
        "schema_sha256": file_sha256(schema_path),
        "grammar_sha256": file_sha256(grammar_path),
        "validation_profile_sha256": file_sha256(profile_path),
        "account_catalog_sha256": catalog_hash,
        "suite_manifest_sha256": manifest_hash,
        "evaluator_sha256": file_sha256(Path(__file__)),
        "strict_parser_sha256": file_sha256(root / "src/pocketfinancer_sms/extractor.py"),
    }
    inference = {
        "n_ctx": n_ctx,
        "n_gpu_layers": n_gpu_layers,
        "seed": seed,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "greedy_decoding": True,
        "chat_template": "gguf_embedded",
        "grammar_constrained": grammar_enabled,
        "apply_prefilter": apply_prefilter,
        "prefilter_policy": PREFILTER_POLICY_ID if apply_prefilter else None,
        "per_row_deadline_ms": 0,
        "primary_currency": primary_currency,
        "enabled_profile_ids": list(enabled_profile_ids),
    }
    provenance = {
        "lane": "android_target_gguf",
        "suite": suite,
        "evaluation_version": EVALUATION_CONTRACT,
        "scoring_version": SCORING_VERSION,
        "semantic_contract": manifest["semantic_contract"],
        "output_contract": manifest["lanes"]["android_target_gguf"]["output_contract"],
        "prompt_version": manifest["lanes"]["android_target_gguf"]["prompt_version"],
        "schema_version": manifest["lanes"]["android_target_gguf"]["schema_version"],
        "dataset_version": manifest["cohorts"][suite]["version"],
        "dataset_fingerprint": dataset_fingerprint,
        "runtime": {
            "name": "llama-cpp-python",
            "version": runtime_version,
            "model_identity_kind": "file_sha256",
            "model_file_sha256": assets["gguf_sha256"],
        },
        "assets": assets,
        "inference": inference,
    }
    configuration_hash = object_sha256(provenance)
    checkpoint_path = private_output / "checkpoint.json"
    completed = _load_checkpoint(
        checkpoint_path,
        configuration_hash=configuration_hash,
        dataset_fingerprint=dataset_fingerprint,
        row_ids={row.row_id for row in rows},
    )
    _validate_existing_report(
        private_output / "report.json",
        configuration_hash=configuration_hash,
        dataset_fingerprint=dataset_fingerprint,
    )

    if grammar_enabled and grammar_factory is None:
        grammar_class = getattr(llama_module, "LlamaGrammar")

        def load_grammar(path: Path) -> Any:
            return grammar_class.from_file(str(path))

        grammar_factory = load_grammar
    if model_factory is None:
        model_factory = getattr(llama_module, "Llama")
    try:
        grammar = grammar_factory(grammar_path) if (grammar_enabled and grammar_factory) else None
        model = model_factory(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            seed=seed,
            verbose=False,
        )
    except Exception as exc:
        raise ExtractorEvaluationError("local GGUF runtime initialization failed") from exc
    _require_embedded_chat_template(model)

    def invoke_gguf(row: EvaluationRow, extractor_input: Mapping[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = model.create_chat_completion(
                messages=(
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            extractor_input,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ),
                temperature=0.0,
                max_tokens=max_tokens,
                grammar=grammar,
                seed=seed,
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            raw_output, finish_reason, usage = _chat_response(response)
            if finish_reason == "length":
                return _failed_record(
                    row,
                    latency_ms,
                    "runtime_failure",
                    "runtime_output_truncated",
                    raw_output=raw_output,
                    usage=usage,
                )
            return _parse_record(
                row,
                raw_output,
                latency_ms,
                primary_currency=primary_currency,
                enabled_profile_ids=enabled_profile_ids,
                catalog=catalog,
                usage=usage,
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            latency_ms = (time.perf_counter() - started) * 1000.0
            return _failed_record(row, latency_ms, "runtime_failure", "runtime_failure")

    try:
        return _execute_rows(
            rows=rows,
            completed=completed,
            checkpoint_path=checkpoint_path,
            report_path=private_output / "report.json",
            configuration_hash=configuration_hash,
            dataset_fingerprint=dataset_fingerprint,
            provenance=provenance,
            capabilities=capabilities,
            suite=suite,
            primary_currency=primary_currency,
            enabled_profile_ids=tuple(enabled_profile_ids),
            apply_prefilter=apply_prefilter,
            invoke_row=invoke_gguf,
        )
    finally:
        model = None


def evaluate_apple_foundation_models(
    repo_root: Path,
    *,
    suite: str,
    output_dir: Path,
    account_catalog: Path | None = None,
    locale: str = "en_US",
    primary_currency: str = "INR",
    enabled_profile_ids: tuple[str, ...] = ("core-en", "india"),
    max_tokens: int = 512,
    apply_prefilter: bool = True,
    provider: AppleProvider | None = None,
) -> dict[str, Any]:
    """Run one private local suite with Apple Foundation Models guided generation."""

    if provider is None and sys.platform != "darwin":
        raise ExtractorEvaluationError(
            "live Apple Foundation Models inference requires compatible macOS; "
            "non-macOS hosts can run only fake-provider tests"
        )
    return _evaluate_apple_locked(
        repo_root,
        suite=suite,
        output_dir=output_dir,
        account_catalog=account_catalog,
        locale=locale,
        primary_currency=primary_currency,
        enabled_profile_ids=enabled_profile_ids,
        max_tokens=max_tokens,
        apply_prefilter=apply_prefilter,
        provider=provider,
    )


@_locked_evaluation
def _evaluate_apple_locked(
    repo_root: Path,
    *,
    suite: str,
    output_dir: Path,
    account_catalog: Path | None,
    locale: str,
    primary_currency: str,
    enabled_profile_ids: tuple[str, ...],
    max_tokens: int,
    apply_prefilter: bool,
    provider: AppleProvider | None,
) -> dict[str, Any]:
    if suite not in {"synthetic", "grandfathered", "private-canonical"}:
        raise ExtractorEvaluationError("Apple evaluation suite is unsupported")
    if not _LOCALE.fullmatch(locale) or max_tokens <= 0 or not enabled_profile_ids:
        raise ExtractorEvaluationError("Apple evaluation configuration is invalid")
    root = repo_root.resolve()
    manifest, manifest_hash = _load_evaluation_manifest(root, suite)
    rows, capabilities, dataset_fingerprint = _load_suite(
        root, suite, primary_currency=primary_currency
    )
    _require_unique_rows(rows)
    catalog, catalog_hash = _load_account_catalog(root, account_catalog)
    prompt_path = _required_asset(root, APPLE_PROMPT_PATH, "Apple extractor instructions")
    schema_path = _required_asset(root, APPLE_SCHEMA_PATH, "Apple guided schema")
    try:
        instructions_template = prompt_path.read_text(encoding="utf-8")
        instructions = (
            instructions_template + f"\nThe person's locale is {locale}. Respond in English.\n"
        )
        schema = json.loads(
            schema_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        if set(schema["required"]) != set(APPLE_FLAT_FIELDS) or set(
            schema["properties"]
        ) != set(APPLE_FLAT_FIELDS):
            raise ValueError("guided schema fields differ")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ExtractorEvaluationError("Apple guided schema or instructions are invalid") from exc

    active_provider = provider if provider is not None else AppleFMProvider()
    provider_id = getattr(active_provider, "provider_id", None)
    runtime_version = getattr(active_provider, "runtime_version", None)
    if (
        not isinstance(provider_id, str)
        or not provider_id
        or not isinstance(runtime_version, str)
        or not runtime_version
    ):
        raise ExtractorEvaluationError("Apple provider identity is invalid")
    simulated = provider is not None
    runtime_meta = {
        "name": provider_id,
        "version": runtime_version,
        "model_identity_kind": "system_managed_runtime" if not simulated else "fake_provider",
        "model_identifier": (
            "SystemLanguageModel.general" if not simulated else "fake-provider-no-apple-model"
        ),
        "model_revision": None,
        "model_file_sha256": None,
        "os": platform.mac_ver()[0] if not simulated else "simulated",
        "hardware": platform.machine() if not simulated else "simulated",
        "os_build": _mac_system_fact("sw_vers", "-buildVersion") if not simulated else "simulated",
        "hardware_model": (
            _mac_system_fact("sysctl", "-n", "hw.model") if not simulated else "simulated"
        ),
        "locale": locale,
        "locale_preflight": "not_exposed_by_python_sdk; unsupported locale errors are captured",
        "simulated": simulated,
    }
    provenance = {
        "lane": "apple_fm_python",
        "suite": suite,
        "evaluation_version": EVALUATION_CONTRACT,
        "scoring_version": SCORING_VERSION,
        "semantic_contract": manifest["semantic_contract"],
        "output_contract": manifest["lanes"]["apple_fm_python"]["output_contract"],
        "prompt_version": manifest["lanes"]["apple_fm_python"]["prompt_version"],
        "schema_version": manifest["lanes"]["apple_fm_python"]["schema_version"],
        "dataset_version": manifest["cohorts"][suite]["version"],
        "dataset_fingerprint": dataset_fingerprint,
        "runtime": runtime_meta,
        "assets": {
            "suite_manifest_sha256": manifest_hash,
            "prompt_sha256": file_sha256(prompt_path),
            "effective_instructions_sha256": object_sha256(instructions),
            "schema_sha256": file_sha256(schema_path),
            "account_catalog_sha256": catalog_hash,
            "evaluator_sha256": file_sha256(Path(__file__)),
            "strict_parser_sha256": file_sha256(root / "src/pocketfinancer_sms/extractor.py"),
            "strict_extractor_schema_sha256": file_sha256(root / SCHEMA_PATH),
        },
        "inference": {
            "max_tokens": max_tokens,
            "sampling": "greedy",
            "guided_generation": True,
            "apply_prefilter": apply_prefilter,
            "prefilter_policy": PREFILTER_POLICY_ID if apply_prefilter else None,
            "primary_currency": primary_currency,
            "enabled_profile_ids": list(enabled_profile_ids),
            "case_context": "new_session_per_case",
            "cumulative_guided_snapshots": "unavailable_in_python_sdk",
            "decoded_tokens": "unavailable",
            "logits": "unavailable",
            "confidence": "unavailable",
            "context_size": "unavailable",
            "token_throughput": "unavailable",
        },
    }
    configuration_hash = object_sha256(provenance)
    private_output = require_private_output(root, _resolve_user_path(root, output_dir))
    ensure_private_directory(private_output)
    _refuse_unsafe_output_reuse(private_output)
    checkpoint_path = private_output / "checkpoint.json"
    completed = _load_checkpoint(
        checkpoint_path,
        configuration_hash=configuration_hash,
        dataset_fingerprint=dataset_fingerprint,
        row_ids={row.row_id for row in rows},
    )
    _validate_existing_report(
        private_output / "report.json",
        configuration_hash=configuration_hash,
        dataset_fingerprint=dataset_fingerprint,
    )

    def invoke_apple(row: EvaluationRow, extractor_input: Mapping[str, Any]) -> dict[str, Any]:
        request = json.dumps(extractor_input, ensure_ascii=False, separators=(",", ":"))
        started = time.perf_counter()
        try:
            flat = asyncio.run(
                active_provider.infer(
                    instructions=instructions,
                    prompt=request,
                    schema=schema,
                    max_tokens=max_tokens,
                )
            )
            mapped = project_apple_fm_flat_output(flat)
            raw = json.dumps(mapped, ensure_ascii=False, separators=(",", ":"))
            record = _parse_record(
                row,
                raw,
                (time.perf_counter() - started) * 1000.0,
                primary_currency=primary_currency,
                enabled_profile_ids=enabled_profile_ids,
                catalog=catalog,
                usage={},
            )
            record["guided_output"] = dict(flat)
            record["validation_stages"] = {
                "guided_schema": "complete",
                "mapping": "complete",
                "strict_parser": "accepted" if record["reason_code"] is None else "rejected",
            }
            return record
        except KeyboardInterrupt:
            raise
        except (TypeError, ValueError) as exc:
            return _failed_record(
                row,
                (time.perf_counter() - started) * 1000.0,
                "malformed",
                _apple_error_code(exc),
            )
        except Exception as exc:
            return _failed_record(
                row,
                (time.perf_counter() - started) * 1000.0,
                "runtime_failure",
                _apple_error_code(exc),
            )

    return _execute_rows(
        rows=rows,
        completed=completed,
        checkpoint_path=checkpoint_path,
        report_path=private_output / "report.json",
        configuration_hash=configuration_hash,
        dataset_fingerprint=dataset_fingerprint,
        provenance=provenance,
        capabilities=capabilities,
        suite=suite,
        primary_currency=primary_currency,
        enabled_profile_ids=enabled_profile_ids,
        apply_prefilter=apply_prefilter,
        invoke_row=invoke_apple,
    )


def _execute_rows(
    *,
    rows: Sequence[EvaluationRow],
    completed: dict[str, dict[str, Any]],
    checkpoint_path: Path,
    report_path: Path,
    configuration_hash: str,
    dataset_fingerprint: str,
    provenance: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    suite: str,
    primary_currency: str,
    enabled_profile_ids: tuple[str, ...],
    apply_prefilter: bool,
    invoke_row: Callable[[EvaluationRow, Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    analyzer = DeterministicSmsAnalyzer(
        CurrencyContext(primary_currency, enabled_profile_ids),
        analysis_contract=ANALYSIS_CONTRACT_V2,
    )
    checkpoint = {
        "contract": EVALUATION_CONTRACT,
        "status": "running",
        "configuration_sha256": configuration_hash,
        "dataset_fingerprint": dataset_fingerprint,
        "total_rows": len(rows),
        "records": completed,
    }
    atomic_write_json(checkpoint_path, checkpoint)
    try:
        for row in rows:
            if row.row_id in completed:
                continue
            if apply_prefilter:
                started = time.perf_counter()
                pf = prefilter(row.source)
                if pf.decision == "exclude":
                    completed[row.row_id] = {
                        "row_id": row.row_id,
                        "outcome": "none",
                        "reason_code": f"prefilter_{pf.reason}",
                        "prefilter_excluded": True,
                        "prefilter_reason": pf.reason,
                        "model_invoked": False,
                        "latency_ms": (time.perf_counter() - started) * 1000.0,
                        "prediction": {"decision": "none"},
                        "gold": dict(row.gold) if row.gold is not None else None,
                        "groups": dict(row.groups),
                        "account_resolution": None,
                        "usage": {},
                        "raw_output": None,
                    }
                    checkpoint["records"] = completed
                    atomic_write_json(checkpoint_path, checkpoint)
                    continue
            analysis = row.analysis
            if analysis is None or analysis.contract != ANALYSIS_CONTRACT_V2:
                analysis = analyzer.analyze(
                    row.source,
                    operation_id=f"evaluate-{row.row_id}",
                    is_outgoing=False,
                    operation_config_hash=configuration_hash,
                )
            analysis = _apply_analysis_mode(analysis, row.analysis_mode)
            extractor_input = build_extractor_input(
                row.source,
                analysis,
                sender_family=row.sender_family,
                primary_currency=primary_currency,
                enabled_profile_ids=enabled_profile_ids,
            )
            record = invoke_row(row, extractor_input)
            record.setdefault("prefilter_excluded", False)
            record.setdefault("model_invoked", True)
            completed[row.row_id] = record
            checkpoint["records"] = completed
            atomic_write_json(checkpoint_path, checkpoint)
    except KeyboardInterrupt as exc:
        checkpoint["status"] = "interrupted"
        checkpoint["records"] = completed
        atomic_write_json(checkpoint_path, checkpoint)
        partial_report = _build_report(
            suite=suite,
            provenance=provenance,
            configuration_hash=configuration_hash,
            capabilities=capabilities,
            rows=rows,
            records=completed,
            status="interrupted",
        )
        atomic_write_json(report_path, partial_report)
        raise EvaluationInterrupted(len(completed), len(rows)) from exc
    checkpoint["status"] = "complete"
    atomic_write_json(checkpoint_path, checkpoint)
    report = _build_report(
        suite=suite,
        provenance=provenance,
        configuration_hash=configuration_hash,
        capabilities=capabilities,
        rows=rows,
        records=completed,
        status="complete",
    )
    atomic_write_json(report_path, report)
    return _aggregate_summary(report)


def _mac_system_fact(*command: str) -> str:
    try:
        result = subprocess_run(command)
    except Exception:
        return "unavailable"
    return result if re.fullmatch(r"[A-Za-z0-9.,_-]{1,80}", result) else "unavailable"


def subprocess_run(command: tuple[str, ...]) -> str:
    import subprocess

    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=3)
    return result.stdout.strip()


def _apple_error_code(exc: Exception) -> str:
    known = {
        "UnsupportedLanguageOrLocaleError": "apple_unsupported_locale",
        "GuardrailViolationError": "apple_guardrail_violation",
        "RefusalError": "apple_refusal",
        "ExceededContextWindowSizeError": "apple_context_exceeded",
        "AssetsUnavailableError": "apple_assets_unavailable",
        "InvalidGenerationSchemaError": "apple_guided_schema_incompatible",
        "DecodingFailureError": "apple_decoding_failure",
    }
    return known.get(
        type(exc).__name__,
        "apple_guided_result_invalid"
        if isinstance(exc, (TypeError, ValueError))
        else "apple_runtime_failure",
    )


def _resolve_user_path(root: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return (root / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()


def _required_asset(root: Path, relative: Path, label: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_file():
        raise ExtractorEvaluationError(f"{label} is unavailable")
    return path


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExtractorEvaluationError(f"{label} could not be read") from exc


def _refuse_unsafe_output_reuse(output_dir: Path) -> None:
    allowed = {"checkpoint.json", "report.json", "run.lock"}
    paths = list(output_dir.iterdir())
    if any(
        path.is_symlink() or (path.name not in allowed and not _ATOMIC_TEMP.fullmatch(path.name))
        for path in paths
    ):
        raise ExtractorEvaluationError("nonempty evaluation output cannot be reused")
    if (output_dir / "report.json").exists() and not (output_dir / "checkpoint.json").exists():
        raise ExtractorEvaluationError("nonempty evaluation output cannot be reused")
    for path in paths:
        if _ATOMIC_TEMP.fullmatch(path.name):
            if not path.is_file():
                raise ExtractorEvaluationError("evaluation temporary output is invalid")
            path.unlink()


def _load_checkpoint(
    path: Path,
    *,
    configuration_hash: str,
    dataset_fingerprint: str,
    row_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExtractorEvaluationError("evaluation checkpoint is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("contract") != EVALUATION_CONTRACT
        or value.get("configuration_sha256") != configuration_hash
        or value.get("dataset_fingerprint") != dataset_fingerprint
        or value.get("total_rows") != len(row_ids)
        or value.get("status") not in {"running", "interrupted", "complete"}
        or not isinstance(value.get("records"), dict)
        or not set(value["records"]) <= row_ids
        or any(
            not isinstance(record, dict) or record.get("row_id") != row_id
            for row_id, record in value["records"].items()
        )
        or (value.get("status") == "complete" and len(value["records"]) != len(row_ids))
    ):
        raise ExtractorEvaluationError("evaluation checkpoint provenance does not match")
    return dict(value["records"])


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_evaluation_manifest(root: Path, suite: str) -> tuple[dict[str, Any], str]:
    path = _required_asset(root, SUITE_MANIFEST_PATH, "model-quality suite manifest")
    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        cohort = manifest["cohorts"][suite]
        if (
            manifest["contract"] != "pocketfinancer.sms-model-quality-suite/1"
            or manifest["evaluation_version"] != EVALUATION_CONTRACT
            or manifest["scoring_version"] != SCORING_VERSION
            or manifest["semantic_contract"] != "pocketfinancer.sms-extractor/1"
        ):
            raise ValueError("unsupported suite manifest")
        lanes = manifest["lanes"]
        if not isinstance(cohort["version"], str) or not cohort["version"]:
            raise ValueError("cohort version is invalid")
        for lane in ("android_target_gguf", "apple_fm_python"):
            if any(
                not isinstance(lanes[lane][field], str) or not lanes[lane][field]
                for field in ("prompt_version", "schema_version", "output_contract")
            ):
                raise ValueError("lane version is invalid")
        if (
            lanes["android_target_gguf"]["prompt"] != PROMPT_PATH.as_posix()
            or lanes["android_target_gguf"]["schema"] != SCHEMA_PATH.as_posix()
            or lanes["apple_fm_python"]["prompt"] != APPLE_PROMPT_PATH.as_posix()
            or lanes["apple_fm_python"]["schema"] != APPLE_SCHEMA_PATH.as_posix()
        ):
            raise ValueError("lane asset path mismatch")
        expected_path = {
            "synthetic": SYNTHETIC_PATH,
            "grandfathered": GRANDFATHERED_PATH,
            "private-canonical": Path("PRIVATE_DATA/sms_processing"),
        }[suite]
        if cohort["path"] != expected_path.as_posix():
            raise ValueError("cohort path mismatch")
        if suite == "private-canonical":
            if cohort["sha256"] is not None:
                raise ValueError("private cohort must be dynamically bound")
        elif cohort["sha256"] != file_sha256(root / expected_path):
            raise ValueError("cohort hash mismatch")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ExtractorEvaluationError("model-quality suite manifest is invalid or stale") from exc
    return manifest, file_sha256(path)


def _require_unique_rows(rows: Sequence[EvaluationRow]) -> None:
    ids = [row.row_id for row in rows]
    if not ids or any(not isinstance(row_id, str) or not row_id for row_id in ids):
        raise ExtractorEvaluationError("evaluation suite has invalid case IDs")
    if len(ids) != len(set(ids)):
        raise ExtractorEvaluationError("evaluation suite has duplicate case IDs")


def _validate_existing_report(
    path: Path, *, configuration_hash: str, dataset_fingerprint: str
) -> None:
    if not path.exists():
        return
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
        if (
            value["contract"] != EVALUATION_CONTRACT
            or value["configuration_sha256"] != configuration_hash
            or value["provenance"]["dataset_fingerprint"] != dataset_fingerprint
        ):
            raise ValueError("report provenance mismatch")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ExtractorEvaluationError("evaluation report provenance does not match") from exc


def _load_suite(
    root: Path,
    suite: str,
    *,
    primary_currency: str,
) -> tuple[list[EvaluationRow], dict[str, Any], str]:
    if suite == "synthetic":
        path = _required_asset(root, SYNTHETIC_PATH, "synthetic extractor suite")
        rows = _load_synthetic(path, primary_currency=primary_currency)
        capabilities = {
            "human_gold": False,
            "decision": True,
            "normalized_fields": True,
            "source_spans": True,
            "account_resolution": False,
            "transaction_timestamp": False,
            "limitations": [
                "sanitized synthetic coverage is not production-quality evidence",
                "synthetic grouping labels do not estimate private sender prevalence",
            ],
        }
        fingerprint = object_sha256(
            {"asset_sha256": file_sha256(path), "rows": [row.fingerprint_binding for row in rows]}
        )
        return rows, capabilities, fingerprint
    if suite == "grandfathered":
        path = _required_asset(root, GRANDFATHERED_PATH, "grandfathered extractor suite")
        rows = _load_grandfathered(path, primary_currency=primary_currency)
        if len(rows) != 203:
            raise ExtractorEvaluationError("grandfathered extractor suite must contain 203 rows")
        capabilities = {
            "human_gold": False,
            "decision": True,
            "normalized_fields": True,
            "source_spans": False,
            "account_resolution": False,
            "transaction_timestamp": False,
            "legacy_date_excluded": True,
            "limitations": [
                "the grandfathered 203-row fixture is a regression set, not a fresh test",
                "legacy date labels are deliberately excluded",
                "gold spans, receipt-time provenance, and account-resolution truth are unavailable",
                "this suite cannot support a production-quality claim",
            ],
        }
        fingerprint = object_sha256(
            {"asset_sha256": file_sha256(path), "rows": [row.fingerprint_binding for row in rows]}
        )
        return rows, capabilities, fingerprint
    rows, private_binding, private_capabilities = _load_private_canonical(root)
    return rows, private_capabilities, object_sha256(private_binding)


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        values = [
            json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExtractorEvaluationError(f"{label} is invalid") from exc
    if not all(isinstance(value, dict) for value in values):
        raise ExtractorEvaluationError(f"{label} is invalid")
    return values


def _load_synthetic(path: Path, *, primary_currency: str) -> list[EvaluationRow]:
    values = _read_jsonl(path, "synthetic extractor suite")
    rows: list[EvaluationRow] = []
    for value in values:
        try:
            source = value["source"]
            sender = value["sender"]
            row_id = value["id"]
            gold = _direct_gold(value["gold"], source, primary_currency)
            mode = value.get("analysis_mode", "normal")
            groups = {
                "sender_family": value["sender_family_label"],
                "template_family": value["template_family_label"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractorEvaluationError("synthetic extractor suite is invalid") from exc
        if not all(isinstance(item, str) and item for item in (source, sender, row_id)):
            raise ExtractorEvaluationError("synthetic extractor suite is invalid")
        if mode not in {"normal", "zero", "incomplete", "conflicting"}:
            raise ExtractorEvaluationError("synthetic extractor suite is invalid")
        rows.append(
            EvaluationRow(
                row_id=row_id,
                source=source,
                sender_family=sender_family(sender),
                gold=gold,
                analysis=None,
                analysis_mode=mode,
                groups=groups,
                fingerprint_binding={
                    "id": row_id,
                    "source_sha256": object_sha256(source),
                    "gold_sha256": object_sha256(gold),
                    "analysis_mode": mode,
                    "groups": groups,
                },
            )
        )
    if len({row.row_id for row in rows}) != len(rows):
        raise ExtractorEvaluationError("synthetic extractor suite has duplicate row IDs")
    return rows


def _load_grandfathered(path: Path, *, primary_currency: str) -> list[EvaluationRow]:
    values = _read_jsonl(path, "grandfathered extractor suite")
    rows: list[EvaluationRow] = []
    for index, value in enumerate(values):
        try:
            source = value["sms"]
            sender = value["sender"]
            legacy = json.loads(value["expected"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ExtractorEvaluationError("grandfathered extractor suite is invalid") from exc
        if not isinstance(source, str) or not isinstance(sender, str):
            raise ExtractorEvaluationError("grandfathered extractor suite is invalid")
        if legacy is None:
            gold = {"decision": "none"}
        elif isinstance(legacy, dict) and legacy.get("type") in {"debit", "credit"}:
            try:
                minor_units = _minor_units(legacy["amount"], primary_currency)
            except (KeyError, ValueError) as exc:
                raise ExtractorEvaluationError("grandfathered extractor suite is invalid") from exc
            gold = {
                "decision": "posted",
                "minor_units": minor_units,
                "currency": primary_currency,
                "direction": legacy["type"],
                "account_reference": _account_compare(legacy.get("account")),
                "counterparty": _counterparty_compare(legacy.get("counterparty")),
                "counterparty_available": legacy.get("counterparty") is not None,
                "spans": None,
            }
        else:
            raise ExtractorEvaluationError("grandfathered extractor suite is invalid")
        row_id = f"grandfathered-{index + 1:03d}"
        rows.append(
            EvaluationRow(
                row_id=row_id,
                source=source,
                sender_family=sender_family(sender),
                gold=gold,
                analysis=None,
                analysis_mode="normal",
                groups={},
                fingerprint_binding={
                    "id": row_id,
                    "source_sha256": object_sha256(source),
                    "gold_sha256": object_sha256(gold),
                },
            )
        )
    return rows


def _load_private_canonical(
    root: Path,
) -> tuple[list[EvaluationRow], dict[str, Any], dict[str, Any]]:
    private_root = require_private_output(root, root / "PRIVATE_DATA/sms_processing")
    current_path = private_root / "CURRENT.json"
    database_path = private_root / "workbench/workbench-v2.sqlite3"
    if not current_path.is_file() or not database_path.is_file():
        raise ExtractorEvaluationError(
            "private-canonical suite requires the secure workbench and canonical corpus"
        )
    try:
        current = json.loads(current_path.read_text(encoding="utf-8"))
        run_id = current["run_id"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ExtractorEvaluationError("private canonical corpus pointer is invalid") from exc
    from .workbench.secure_store import KeyringKeyProvider, SecureWorkbenchStore

    try:
        store = SecureWorkbenchStore(database_path, key_provider=KeyringKeyProvider())
        store.verify_revision_chains()
        listed = store.list_rows(
            filters={}, search=None, sort="source_id", descending=False, limit=200, offset=0
        )
        summaries = list(listed["rows"])
        while len(summaries) < int(listed["total"]):
            page = store.list_rows(
                filters={},
                search=None,
                sort="source_id",
                descending=False,
                limit=200,
                offset=len(summaries),
            )
            summaries.extend(page["rows"])
    except Exception as exc:
        raise ExtractorEvaluationError("secure private workbench could not be opened") from exc
    rows: list[EvaluationRow] = []
    binding_rows: list[dict[str, Any]] = []
    gold_count = 0
    span_gold_count = 0
    for summary in summaries:
        source_id = str(summary["source_id"])
        record = store.get_record(source_id)
        if record is None:
            raise ExtractorEvaluationError("secure private workbench is inconsistent")
        annotations = store.submitted_annotations(source_id)
        selected = _select_committed_annotation(annotations)
        gold = None
        revision_hash = None
        if selected is not None and selected.get("canonical_label") is not None:
            gold = _canonical_gold(selected["canonical_label"], record["source"]["body"])
            revision_hash = selected["revision_hash"]
            gold_count += 1
            span_gold_count += int(gold.get("spans") is not None)
        grouping = record["grouping"]
        groups = {
            "sender_family": grouping["sender_family_hash"],
            "template_family": grouping["normalized_template_hash"],
        }
        row = EvaluationRow(
            row_id=source_id,
            source=record["source"]["body"],
            sender_family=sender_family(record["source"].get("sender", "")),
            gold=gold,
            analysis=Analysis.from_dict(record["analysis"], source=record["source"]["body"]),
            analysis_mode="normal",
            groups=groups,
            fingerprint_binding={
                "source_id": source_id,
                "record_fingerprint": record["analysis"]["source_fingerprint"],
                "revision_hash": revision_hash,
                "groups": groups,
            },
        )
        rows.append(row)
        binding_rows.append(dict(row.fingerprint_binding))
    if not rows:
        raise ExtractorEvaluationError("private canonical corpus contains no rows")
    capabilities = {
        "human_gold": True,
        "decision": gold_count > 0,
        "normalized_fields": gold_count > 0,
        "source_spans": span_gold_count > 0,
        "account_resolution": False,
        "transaction_timestamp": False,
        "submitted_or_adjudicated_gold_rows": gold_count,
        "unlabelled_outcome_latency_only_rows": len(rows) - gold_count,
        "limitations": [
            "accuracy includes submitted/adjudicated human-gold rows only",
            "unlabelled rows contribute only outcome and latency rates",
            "private sender/template groups below the suppression threshold are hidden",
        ],
    }
    return rows, {"corpus_run_id": run_id, "rows": binding_rows}, capabilities


def _select_committed_annotation(values: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    adjudicated = [value for value in values if value.get("status") == "adjudicated"]
    if adjudicated:
        return max(adjudicated, key=lambda value: int(value.get("revision", 0)))
    submitted = [value for value in values if value.get("status") == "submitted"]
    if len(submitted) == 1:
        return submitted[0]
    if submitted and len({object_sha256(value.get("canonical_label")) for value in submitted}) == 1:
        return submitted[0]
    return None


def _direct_gold(value: Any, source: str, primary_currency: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("decision") not in {"none", "abstain", "posted"}:
        raise ValueError("invalid direct gold")
    if value["decision"] != "posted":
        return {"decision": value["decision"]}
    amount = value["amount"]
    direction = value["direction"]
    account = value["account"]
    counterparty = value.get("counterparty")
    currency = amount.get("currency", primary_currency)
    spans = {
        "amount": _checked_span(amount["evidence"], source),
        "direction": _checked_span(direction["evidence"], source),
        "account": _checked_span(account["evidence"], source),
        "counterparty": (
            _checked_span(counterparty["evidence"], source) if counterparty is not None else None
        ),
    }
    return {
        "decision": "posted",
        "minor_units": _minor_units(amount["value"], currency),
        "currency": currency,
        "direction": direction["value"],
        "account_reference": _account_compare(account["reference"]),
        "counterparty": (
            _counterparty_compare(counterparty["value"]) if counterparty is not None else None
        ),
        "counterparty_available": counterparty is not None,
        "spans": spans,
    }


def _canonical_gold(value: Mapping[str, Any], source: str) -> dict[str, Any]:
    if value.get("contract") == "pocketfinancer.canonical-label/2":
        decision = str(value.get("decision", ""))
        if decision in {"none", "abstain"}:
            return {"decision": decision}
        event = value.get("event")
        if decision != "posted" or not isinstance(event, Mapping):
            raise ExtractorEvaluationError("private canonical label is unsupported")
        direction_span = event.get("direction_span")
        return {
            "decision": "posted",
            "minor_units": _minor_units(event["amount_value"], event["currency"]),
            "currency": event["currency"],
            "direction": event["direction"],
            "account_reference": _account_compare(event["account_reference"]),
            "counterparty": _counterparty_compare(event.get("counterparty")),
            "counterparty_available": event.get("counterparty") is not None,
            "spans": {
                "amount": _checked_span(event["amount_span"], source),
                "direction": (
                    _checked_span(direction_span, source) if direction_span is not None else None
                ),
                "account": _checked_span(event["account_span"], source),
                "counterparty": (
                    _checked_span(event["counterparty_span"], source)
                    if event.get("counterparty_span") is not None
                    else None
                ),
            },
        }
    decision = str(value.get("decision", ""))
    if decision in {"not_posted", "non_financial", "none"}:
        return {"decision": "none"}
    if decision in {"ambiguous", "multiple_event", "abstain"}:
        return {"decision": "abstain"}
    if decision != "posted" or not value.get("events"):
        raise ExtractorEvaluationError("private canonical label is unsupported")
    event = value["events"][0]
    amount = event.get("amount") if isinstance(event.get("amount"), dict) else {}
    direction_value = event.get("direction")
    direction = direction_value if isinstance(direction_value, dict) else {}
    account = event.get("account") if isinstance(event.get("account"), dict) else {}
    counterparty_value = event.get("counterparty")
    counterparty = counterparty_value if isinstance(counterparty_value, dict) else {}
    amount_span = event.get("amount_span") or amount.get("evidence")
    direction_span = event.get("direction_span") or direction.get("evidence")
    account_span = event.get("account_span") or account.get("evidence")
    counterparty_span = event.get("counterparty_span") or counterparty.get("evidence")
    spans = None
    if amount_span and direction_span and account_span:
        spans = {
            "amount": _checked_span(amount_span, source),
            "direction": _checked_span(direction_span, source),
            "account": _checked_span(account_span, source),
            "counterparty": (
                _checked_span(counterparty_span, source) if counterparty_span else None
            ),
        }
    currency = event.get("currency") or amount.get("currency")
    minor_units = event.get("minor_units")
    if minor_units is None and amount.get("value") is not None and currency:
        minor_units = _minor_units(amount["value"], currency)
    direction_scalar = direction.get("value") if direction else direction_value
    account_reference = event.get("account_reference") or account.get("reference")
    counterparty_scalar = event.get("counterparty_value") or counterparty.get("value")
    return {
        "decision": "posted",
        "minor_units": minor_units,
        "currency": currency,
        "direction": direction_scalar,
        "account_reference": _account_compare(account_reference),
        "counterparty": _counterparty_compare(counterparty_scalar),
        "counterparty_available": counterparty_scalar is not None,
        "spans": spans,
    }


def _checked_span(value: Mapping[str, Any], source: str) -> dict[str, Any]:
    start = value.get("start_scalar", value.get("start_char"))
    end = value.get("end_scalar", value.get("end_char"))
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or end > len(source)
    ):
        raise ValueError("invalid gold span")
    text = value.get("text", source[start:end])
    if text != source[start:end]:
        raise ValueError("invalid gold span")
    return {"start_scalar": start, "end_scalar": end, "text": text}


def _minor_units(value: Any, currency: str) -> int:
    if currency not in ISO_MINOR_UNITS:
        raise ValueError("unsupported gold currency")
    decimal = Decimal(str(value))
    scaled = decimal * (10 ** ISO_MINOR_UNITS[currency])
    if (
        not scaled.is_finite()
        or scaled <= 0
        or scaled != scaled.to_integral_value()
        or scaled > MAX_SIGNED_64
    ):
        raise ValueError("invalid gold precision")
    return int(scaled)


def _apply_analysis_mode(analysis: Analysis, mode: str) -> Analysis:
    if mode == "normal":
        return analysis
    if mode == "zero":
        return replace(analysis, clauses=(), candidates=(), cues=(), reason_codes=())
    if mode == "incomplete":
        candidates = tuple(
            candidate for candidate in analysis.candidates if candidate.kind == CandidateKind.AMOUNT
        )
        return replace(analysis, candidates=candidates, cues=())
    if mode == "conflicting":
        candidates = []
        for candidate in analysis.candidates:
            replacement = dict(candidate.value)
            if candidate.kind == CandidateKind.DIRECTION:
                direction = replacement.get("direction")
                replacement["direction"] = "credit" if direction == "debit" else "debit"
            elif candidate.kind == CandidateKind.ACCOUNT:
                replacement["normalized_identifier"] = "0000"
            elif candidate.kind == CandidateKind.AMOUNT and isinstance(
                replacement.get("minor_units"), int
            ):
                replacement["minor_units"] += 1
            candidates.append(replace(candidate, value=replacement))
        return replace(analysis, candidates=tuple(candidates))
    raise ExtractorEvaluationError("synthetic analysis mode is invalid")


def _require_embedded_chat_template(model: Any) -> None:
    metadata = getattr(model, "metadata", None)
    if not isinstance(metadata, Mapping) or not any(
        str(key).startswith("tokenizer.chat_template") and value for key, value in metadata.items()
    ):
        raise ExtractorEvaluationError("GGUF does not provide an embedded chat template")


def _chat_response(response: Any) -> tuple[str, str, dict[str, int]]:
    try:
        choice = response["choices"][0]
        raw = choice["message"]["content"]
        finish = str(choice.get("finish_reason") or "unknown")
        usage_value = response.get("usage") or {}
        usage = {
            "prompt_tokens": int(usage_value.get("prompt_tokens", 0)),
            "completion_tokens": int(usage_value.get("completion_tokens", 0)),
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid local runtime response") from exc
    if not isinstance(raw, str):
        raise RuntimeError("invalid local runtime response")
    return raw, finish, usage


def _parse_record(
    row: EvaluationRow,
    raw_output: str,
    latency_ms: float,
    *,
    primary_currency: str,
    enabled_profile_ids: Sequence[str],
    catalog: Sequence[AccountCatalogEntry] | None,
    usage: Mapping[str, int],
) -> dict[str, Any]:
    try:
        parsed = parse_and_normalize_extraction(
            raw_output,
            row.source,
            primary_currency=primary_currency,
            enabled_profile_ids=tuple(enabled_profile_ids),
        )
        prediction = _prediction_view(parsed)
    except ExtractionValidationError as exc:
        reason_code = _safe_reason_code(exc)
        outcome = "runtime_failure" if reason_code.startswith("runtime_") else "malformed"
        return _failed_record(
            row,
            latency_ms,
            outcome,
            reason_code,
            raw_output=raw_output,
            usage=usage,
        )
    except Exception:
        return _failed_record(
            row,
            latency_ms,
            "runtime_failure",
            "runtime_failure",
            usage=usage,
        )
    resolution = (
        _resolve_for_evaluation(prediction.get("account_reference"), catalog)
        if prediction["decision"] == "posted"
        else None
    )
    return {
        "row_id": row.row_id,
        "outcome": prediction["decision"],
        "reason_code": None,
        "prefilter_excluded": False,
        "model_invoked": True,
        "latency_ms": latency_ms,
        "prediction": prediction,
        "gold": dict(row.gold) if row.gold is not None else None,
        "groups": dict(row.groups),
        "account_resolution": resolution,
        "usage": dict(usage),
        "raw_output": raw_output,
    }


def _failed_record(
    row: EvaluationRow,
    latency_ms: float,
    outcome: str,
    reason_code: str,
    *,
    raw_output: str | None = None,
    usage: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "outcome": outcome,
        "reason_code": reason_code,
        "prefilter_excluded": False,
        "model_invoked": True,
        "latency_ms": latency_ms,
        "prediction": None,
        "gold": dict(row.gold) if row.gold is not None else None,
        "groups": dict(row.groups),
        "account_resolution": None,
        "usage": dict(usage or {}),
        "raw_output": raw_output,
    }


def _safe_reason_code(exc: Exception) -> str:
    candidate = getattr(exc, "reason_code", None)
    if candidate is None and exc.args:
        candidate = exc.args[0]
    if isinstance(candidate, str) and _SAFE_REASON.fullmatch(candidate):
        return candidate
    return "extractor_output_invalid"


def _prediction_view(value: Any) -> dict[str, Any]:
    decision = getattr(value, "decision", None)
    transaction = getattr(value, "transaction", None)
    if isinstance(value, Mapping):
        decision = value.get("decision")
        transaction = value.get("transaction")
    if hasattr(decision, "value"):
        decision = decision.value
    if decision not in {"none", "abstain", "posted"}:
        raise ValueError("extractor_result_invalid")
    if decision != "posted":
        return {"decision": decision}
    if transaction is None:
        raise ValueError("extractor_result_invalid")
    tx = _as_mapping(transaction)
    direction = tx.get("direction")
    if hasattr(direction, "value"):
        direction = direction.value
    return {
        "decision": "posted",
        "minor_units": tx.get("minor_units"),
        "currency": tx.get("currency"),
        "direction": direction,
        "account_reference": _account_compare(tx.get("account_reference")),
        "counterparty": _counterparty_compare(tx.get("counterparty")),
        "spans": {
            "amount": _span_view(tx.get("amount_span")),
            "direction": _span_view(tx.get("direction_span")),
            "account": _span_view(tx.get("account_span")),
            "counterparty": _span_view(tx.get("counterparty_span")),
        },
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    names = (
        "minor_units",
        "currency",
        "direction",
        "account_reference",
        "counterparty",
        "amount_span",
        "direction_span",
        "account_span",
        "counterparty_span",
    )
    return {name: getattr(value, name, None) for name in names}


def _span_view(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    mapping = _as_mapping(value)
    return {
        "start_scalar": mapping.get("start_scalar"),
        "end_scalar": mapping.get("end_scalar"),
        "text": mapping.get("text"),
    }


def _load_account_catalog(
    root: Path, path: Path | None
) -> tuple[list[AccountCatalogEntry] | None, str | None]:
    if path is None:
        return None, None
    resolved = require_private_output(root, _resolve_user_path(root, path))
    if not resolved.is_file():
        raise ExtractorEvaluationError("local account catalog is unavailable")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExtractorEvaluationError("local account catalog is invalid") from exc
    accounts = value.get("accounts") if isinstance(value, dict) else value
    if not isinstance(accounts, list) or not all(isinstance(item, dict) for item in accounts):
        raise ExtractorEvaluationError("local account catalog is invalid")
    entries = []
    for item in accounts:
        try:
            entries.append(
                AccountCatalogEntry(
                    account_id=item["account_id"],
                    account_type=item["account_type"],
                    aliases=tuple(item["aliases"]),
                    owned=item.get("owned", True),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractorEvaluationError("local account catalog is invalid") from exc
    return entries, file_sha256(resolved)


def _resolve_for_evaluation(
    reference: Any, catalog: Sequence[AccountCatalogEntry] | None
) -> dict[str, Any] | None:
    if catalog is None:
        return None
    resolution = resolve_account(reference, catalog)
    return {"status": resolution.status.value}


def _account_compare(value: Any) -> str | None:
    if value is None:
        return None
    normalized = normalize_account_reference(str(value))
    return normalized or None


def _counterparty_compare(value: Any) -> str | None:
    if value is None:
        return None
    normalized = normalize_counterparty(str(value))
    return normalized or None


def _build_report(
    *,
    suite: str,
    provenance: Mapping[str, Any],
    configuration_hash: str,
    capabilities: Mapping[str, Any],
    rows: Sequence[EvaluationRow],
    records: Mapping[str, Mapping[str, Any]],
    status: str,
) -> dict[str, Any]:
    ordered = [records[row.row_id] for row in rows if row.row_id in records]
    adjusted_capabilities = dict(capabilities)
    if provenance["assets"].get("account_catalog_sha256"):
        adjusted_capabilities["account_resolution"] = True
    return {
        "contract": EVALUATION_CONTRACT,
        "scoring_version": SCORING_VERSION,
        "status": status,
        "lane": provenance["lane"],
        "suite": suite,
        "configuration_sha256": configuration_hash,
        "provenance": dict(provenance),
        "contract_differences": {
            "gguf": "embedded chat template, direct extractor JSON, optional GBNF, observable model hash",
            "apple": "system-managed model, guided flat schema mapped to strict extractor JSON",
            "comparison": "same declared cohort, prefilter, and host parser/scoring",
        },
        "capabilities": adjusted_capabilities,
        "production_readiness_claim": False,
        "counts": {"completed": len(ordered), "total": len(rows)},
        "metrics": _metrics(ordered),
        "groups": _group_metrics(ordered, suite=suite),
    }


def _metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    labelled = [record for record in records if record.get("gold") is not None]
    decisions = ("none", "abstain", "posted")
    observed_outcomes = (*decisions, "malformed", "runtime_failure")
    confusion = {
        gold_decision: {
            outcome: sum(
                record["gold"]["decision"] == gold_decision and record["outcome"] == outcome
                for record in labelled
            )
            for outcome in observed_outcomes
        }
        for gold_decision in decisions
    }
    decision_correct = sum(record["gold"]["decision"] == record["outcome"] for record in labelled)
    gold_posted = sum(record["gold"]["decision"] == "posted" for record in labelled)
    predicted_posted = sum(record["outcome"] == "posted" for record in labelled)
    true_posted = sum(
        record["gold"]["decision"] == "posted" and record["outcome"] == "posted"
        for record in labelled
    )
    precision = _ratio(true_posted, predicted_posted)
    recall = _ratio(true_posted, gold_posted)
    f1 = None
    if precision is not None and recall is not None:
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    strict = [
        _strict_match(record) for record in labelled if _strict_gold_supported(record["gold"])
    ]
    grounding = [
        _grounding_valid(record) for record in records if record.get("outcome") == "posted"
    ]
    resolutions = [
        record["account_resolution"]["status"]
        for record in records
        if record.get("account_resolution") is not None
    ]
    outcomes = {
        name: sum(record["outcome"] == name for record in records) for name in observed_outcomes
    }
    reason_code_counts: dict[str, int] = {}
    for record in records:
        reason = record.get("reason_code")
        if reason:
            reason_code_counts[str(reason)] = reason_code_counts.get(str(reason), 0) + 1
    count = len(records)
    prefilter_excluded_count = sum(bool(record.get("prefilter_excluded")) for record in records)
    avoided_model_calls = sum(record.get("model_invoked") is False for record in records)
    false_exclusions = sum(
        bool(record.get("prefilter_excluded")) and record["gold"]["decision"] == "posted"
        for record in labelled
    )
    rejected_grounding = sum(
        str(record.get("reason_code") or "").startswith("extractor_evidence_") for record in records
    )
    rejected_derivation = sum(
        record.get("reason_code")
        in {"extractor_amount_value_disagreement", "extractor_account_reference_invalid"}
        for record in records
    )
    return {
        "total_inputs": count,
        "labelled_count": len(labelled),
        "prefilter": {
            "excluded_count": prefilter_excluded_count,
            "excluded_rate": _ratio(prefilter_excluded_count, count),
            "avoided_model_calls": avoided_model_calls,
            "false_exclusions": false_exclusions,
        },
        "classification": {
            "correct_count": decision_correct,
            "eligible_count": len(labelled),
            "accuracy": _ratio(decision_correct, len(labelled)),
            "confusion": confusion,
            "false_transactions": sum(
                record["outcome"] == "posted" and record["gold"]["decision"] != "posted"
                for record in labelled
            ),
            "missed_transactions": sum(
                record["gold"]["decision"] == "posted" and record["outcome"] != "posted"
                for record in labelled
            ),
        },
        "transaction": {"precision": precision, "recall": recall, "f1": f1},
        "strict_posted_exact_success": {
            "count": sum(strict),
            "eligible_count": len(strict),
            "rate": _ratio(sum(strict), len(strict)),
        },
        "amount_accuracy": _field_accuracy(labelled, "amount"),
        "direction_accuracy": _field_accuracy(labelled, "direction"),
        "account_reference_accuracy": _field_accuracy(labelled, "account"),
        "counterparty_accuracy": _field_accuracy(labelled, "counterparty"),
        "exact_source_spans": _source_span_accuracy(labelled),
        "source_grounding": {
            "rejected_count": rejected_grounding + rejected_derivation,
            "rejected_rate": _ratio(rejected_grounding + rejected_derivation, count),
            "source_span_rejected_count": rejected_grounding,
            "source_derivation_rejected_count": rejected_derivation,
            "accepted_posted_count": sum(grounding),
            "accepted_posted_eligible_count": len(grounding),
        },
        "amount_grounding_validity": {
            "count": sum(grounding),
            "eligible_count": len(grounding),
            "rate": _ratio(sum(grounding), len(grounding)),
        },
        "account_resolution": {
            "eligible_count": len(resolutions),
            "success_rate": _ratio(resolutions.count("uniquely_resolved"), len(resolutions)),
            "ambiguity_rate": _ratio(resolutions.count("ambiguous"), len(resolutions)),
        },
        "outcome_rates": {
            **{name: _ratio(value, count) for name, value in outcomes.items()},
            "review": _ratio(
                outcomes["abstain"] + outcomes["malformed"] + outcomes["runtime_failure"],
                count,
            ),
        },
        "abstention": {
            "predicted_count": outcomes["abstain"],
            "predicted_rate": _ratio(outcomes["abstain"], count),
            "correct_on_gold_abstain": confusion["abstain"]["abstain"],
            "gold_abstain_count": sum(
                record["gold"]["decision"] == "abstain" for record in labelled
            ),
        },
        "failures": {
            "malformed_count": outcomes["malformed"],
            "runtime_failure_count": outcomes["runtime_failure"],
            "total_count": outcomes["malformed"] + outcomes["runtime_failure"],
        },
        "reason_code_counts": dict(sorted(reason_code_counts.items())),
        "latency_ms": _latency_summary([float(record["latency_ms"]) for record in records]),
    }


def _strict_gold_supported(gold: Mapping[str, Any]) -> bool:
    return gold.get("decision") == "posted" and all(
        gold.get(name) is not None
        for name in ("minor_units", "currency", "direction", "account_reference")
    )


def _strict_match(record: Mapping[str, Any]) -> bool:
    gold = record["gold"]
    prediction = record.get("prediction")
    if record.get("outcome") != "posted" or not isinstance(prediction, Mapping):
        return False
    fields_match = (
        prediction.get("minor_units") == gold.get("minor_units")
        and prediction.get("currency") == gold.get("currency")
        and prediction.get("direction") == gold.get("direction")
        and prediction.get("account_reference") == gold.get("account_reference")
        and prediction.get("counterparty") == gold.get("counterparty")
    )
    gold_spans = gold.get("spans")
    if not fields_match or gold_spans is None:
        return fields_match
    prediction_spans = prediction.get("spans")
    if not isinstance(prediction_spans, Mapping):
        return False
    return all(
        expected is None or prediction_spans.get(name) == expected
        for name, expected in gold_spans.items()
    )


def _field_accuracy(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    eligible = []
    for record in records:
        gold = record["gold"]
        if gold["decision"] != "posted":
            continue
        if field == "amount" and gold.get("minor_units") is None:
            continue
        if field == "direction" and gold.get("direction") is None:
            continue
        if field == "account" and gold.get("account_reference") is None:
            continue
        if field == "counterparty" and not gold.get("counterparty_available"):
            continue
        eligible.append(record)
    correct = 0
    for record in eligible:
        prediction = record.get("prediction") or {}
        gold = record["gold"]
        if field == "amount":
            matched = prediction.get("minor_units") == gold.get("minor_units") and prediction.get(
                "currency"
            ) == gold.get("currency")
        elif field == "direction":
            matched = prediction.get("direction") == gold.get("direction")
        elif field == "account":
            matched = prediction.get("account_reference") == gold.get("account_reference")
        else:
            matched = prediction.get("counterparty") == gold.get("counterparty")
        correct += int(matched)
    return {
        "count": correct,
        "eligible_count": len(eligible),
        "rate": _ratio(correct, len(eligible)),
    }


def _source_span_accuracy(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = ("amount", "direction", "account", "counterparty")
    result: dict[str, Any] = {}
    for field in fields:
        eligible = [
            record
            for record in records
            if record["gold"].get("decision") == "posted"
            and isinstance(record["gold"].get("spans"), Mapping)
            and record["gold"]["spans"].get(field) is not None
        ]
        correct = sum(
            record.get("outcome") == "posted"
            and ((record.get("prediction") or {}).get("spans") or {}).get(field)
            == record["gold"]["spans"][field]
            for record in eligible
        )
        result[field] = {
            "count": correct,
            "eligible_count": len(eligible),
            "rate": _ratio(correct, len(eligible)),
        }
    return result


def _grounding_valid(record: Mapping[str, Any]) -> bool:
    prediction = record.get("prediction") or {}
    spans = prediction.get("spans") or {}
    required = ("amount", "direction", "account")
    if prediction.get("counterparty") is not None:
        required = (*required, "counterparty")
    return all(
        isinstance(spans.get(field), Mapping)
        and isinstance(spans[field].get("text"), str)
        and spans[field]["text"]
        for field in required
    )


def _latency_summary(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(ordered),
        "mean": statistics.fmean(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
    }


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _group_metrics(records: Sequence[Mapping[str, Any]], *, suite: str) -> dict[str, Any]:
    if suite == "grandfathered":
        return {
            "available": False,
            "reason": "grandfathered fixture has no approved evaluation grouping labels",
        }
    result: dict[str, Any] = {
        "available": True,
        "small_group_minimum": _SMALL_GROUP_MINIMUM,
    }
    for dimension in ("sender_family", "template_family"):
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            group = record.get("groups", {}).get(dimension)
            if group:
                buckets.setdefault(str(group), []).append(record)
        visible = {}
        suppressed_groups = 0
        suppressed_rows = 0
        for group, values in sorted(buckets.items()):
            if suite == "private-canonical" and len(values) < _SMALL_GROUP_MINIMUM:
                suppressed_groups += 1
                suppressed_rows += len(values)
                continue
            aggregate = _metrics(values)
            visible[group] = {
                "count": len(values),
                "transaction": aggregate["transaction"],
                "strict_posted_exact_success": aggregate["strict_posted_exact_success"],
                "outcome_rates": aggregate["outcome_rates"],
            }
        result[dimension] = {
            "groups": visible,
            "suppressed_group_count": suppressed_groups,
            "suppressed_row_count": suppressed_rows,
        }
    return result


def _aggregate_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": report["status"],
        "lane": report["lane"],
        "suite": report["suite"],
        "completed_rows": report["counts"]["completed"],
        "total_rows": report["counts"]["total"],
        "configuration_sha256": report["configuration_sha256"],
        "metrics": report["metrics"],
        "production_readiness_claim": False,
    }


# ---------------------------------------------------------------------------
# Stateful supplied-response pipeline runner (isolated synthetic SQLite runs)
# ---------------------------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _host_source_hashes() -> dict[str, str]:
    root = Path(__file__).parent
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "evaluation.py", "admission.py", "prefilter.py", "processing.py",
            "extractor.py", "analyzer.py", "account_resolution.py", "currency.py",
            "structural_text.py", "profiles.py", "types.py", "feedback.py", "corpus/grouping.py",
        )
    }


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InventedMessage:
    body: str
    sender: str
    received_at_epoch_ms: int
    connector: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if not self.body or len(self.body.encode("utf-8")) > 16_384:
            raise ValueError("invalid invented body")
        if type(self.received_at_epoch_ms) is not int or self.received_at_epoch_ms < 0:
            raise ValueError("invalid receipt time")
        if (self.connector is None) != (self.source_id is None):
            raise ValueError("source identity requires both connector and ID")
        if self.connector == "" or self.source_id == "":
            raise ValueError("source identity must not be empty")

    @property
    def reliable_key(self) -> str | None:
        if self.source_id is None:
            return None
        return _hash([self.connector, self.source_id])


@dataclass(frozen=True)
class RunReceipt:
    operation_id: str
    action: str
    route: str
    model_invoked: bool = False


class SuppliedRuntime:
    """Counted fake runtime; responses are supplied, never inferred or repaired."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any], _cancellation: Any) -> str:
        self.calls += 1
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("no supplied response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SyntheticPipelineRun:
    """Isolated end-to-end stateful SQLite pipeline runner for mechanics testing."""

    def __init__(
        self,
        path: Path,
        *,
        primary_currency: str = "INR",
        enabled_profiles: tuple[str, ...] = ("core-en", "india"),
        duplicate_window_ms: int = DUPLICATE_WINDOW_MS,
        resume: bool = False,
    ) -> None:
        self.settings = {
            "contract": RUN_CONTRACT,
            "primary_currency": primary_currency,
            "enabled_profiles": list(enabled_profiles),
            "prefilter": PREFILTER_POLICY_ID,
            "duplicate_window_ms": duplicate_window_ms,
            "host_source_hashes": _host_source_hashes(),
        }
        context = CurrencyContext(primary_currency, enabled_profiles)
        self.settings["primary_currency"] = context.primary_currency
        self.settings["analyzer_configuration_hash"] = context.config_hash
        self.duplicate_window_ms = duplicate_window_ms
        if path.is_symlink() or (resume and not path.is_file()):
            raise ValueError("resume requires an existing regular run file")
        if not resume:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        try:
            if not resume:
                self.db.executescript("""
                    CREATE TABLE run (configuration TEXT NOT NULL);
                    CREATE TABLE operations (
                        id TEXT PRIMARY KEY, source_key TEXT,
                        evidence_digest TEXT NOT NULL, evidence TEXT,
                        body_retained INTEGER NOT NULL,
                        settings TEXT NOT NULL, parent_id TEXT REFERENCES operations(id),
                        state TEXT NOT NULL, route TEXT NOT NULL,
                        payload TEXT, fingerprint TEXT
                    );
                    CREATE UNIQUE INDEX ordinary_source ON operations(source_key)
                        WHERE parent_id IS NULL AND source_key IS NOT NULL;
                    CREATE TABLE admissions (
                        operation_id TEXT NOT NULL REFERENCES operations(id),
                        action TEXT NOT NULL, route TEXT NOT NULL
                    );
                    CREATE TABLE attempts (
                        operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                        raw_output TEXT
                    );
                    CREATE TABLE accounts (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, aliases TEXT NOT NULL
                    );
                    CREATE TABLE ledger (
                        source_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                        account_id TEXT NOT NULL REFERENCES accounts(id),
                        direction TEXT NOT NULL, minor_units INTEGER NOT NULL,
                        currency TEXT NOT NULL, received_at_epoch_ms INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                """)
                self.db.execute("INSERT INTO run VALUES (?)", (_json(self.settings),))
            rows = self.db.execute("SELECT configuration FROM run").fetchall()
            if len(rows) != 1 or rows[0][0] != _json(self.settings):
                raise ValueError("run configuration mismatch; create a fresh isolated run")
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def admit(self, message: InventedMessage, *, retry_of: str | None = None) -> RunReceipt:
        from .admission import admission_action

        evidence = _json(asdict(message))
        evidence_digest = _hash(asdict(message))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = None
            if retry_of is not None:
                existing = self._operation(retry_of)
                if self.db.execute(
                    "SELECT 1 FROM operations WHERE parent_id = ?", (retry_of,)
                ).fetchone():
                    raise ValueError("retry must target the latest operation")
                if not existing["body_retained"]:
                    raise ValueError("tombstoned operation has no body to retry")
            elif message.reliable_key is not None:
                existing = self.db.execute(
                    "SELECT * FROM operations WHERE source_key = ? ORDER BY rowid DESC LIMIT 1",
                    (message.reliable_key,),
                ).fetchone()
            action = admission_action(
                reliable_identity=message.reliable_key is not None,
                existing_state=existing["state"] if existing else None,
                same_evidence=existing["evidence_digest"] == evidence_digest if existing else True,
                owner_retry=retry_of is not None,
            )
            if action == "conflict":
                raise ValueError("source identity conflicts with immutable evidence")
            if action in {"reuse", "resume", "in_flight"}:
                assert existing is not None
                receipt = RunReceipt(existing["id"], action, existing["route"])
            else:
                operation_id = str(uuid.uuid4())
                decision = prefilter(message.body)
                excluded = decision.decision == "exclude"
                self.db.execute(
                    "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        operation_id,
                        message.reliable_key,
                        evidence_digest,
                        None if excluded else evidence,
                        0 if excluded else 1,
                        _json(self.settings),
                        retry_of,
                        "settled" if excluded else "ready",
                        "tombstoned_excluded" if excluded else "pending",
                        _json({"prefilter_reason": decision.reason}) if excluded else None,
                    ),
                )
                receipt = RunReceipt(
                    operation_id, action, "tombstoned_excluded" if excluded else "pending"
                )
            self.db.execute(
                "INSERT INTO admissions VALUES (?, ?, ?)",
                (receipt.operation_id, receipt.action, receipt.route),
            )
            self.db.execute("COMMIT")
            return receipt
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def execute(self, operation_id: str, runtime: SuppliedRuntime) -> RunReceipt:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._operation(operation_id)
            if row["state"] != "ready":
                self.db.execute("COMMIT")
                return RunReceipt(
                    operation_id,
                    "in_flight" if row["state"] == "in_flight" else "reuse",
                    row["route"],
                )
            if self.db.execute(
                "SELECT 1 FROM operations WHERE parent_id = ?", (operation_id,)
            ).fetchone():
                self.db.execute("COMMIT")
                return RunReceipt(operation_id, "reuse", "superseded")
            if not row["evidence"]:
                raise ValueError("cannot execute operation without retained source evidence")
            self.db.execute(
                "UPDATE operations SET state = 'in_flight' WHERE id = ?", (operation_id,)
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        settings = json.loads(row["settings"])
        message = InventedMessage(**json.loads(row["evidence"]))
        profiles = tuple(settings["enabled_profiles"])
        catalog = tuple(
            AccountCatalogEntry(item["id"], item["kind"], tuple(json.loads(item["aliases"])))
            for item in self.db.execute("SELECT * FROM accounts ORDER BY id")
        )
        recent = self._recent_transactions(exclude_operation_id=operation_id)
        coordinator = ExtractionCoordinator(
            DeterministicSmsAnalyzer(
                CurrencyContext(settings["primary_currency"], profiles),
                analysis_contract=ANALYSIS_CONTRACT_V2,
            ),
            runtime,
            primary_currency=settings["primary_currency"],
            enabled_profile_ids=profiles,
            account_catalog=catalog,
            apply_prefilter=False,
            duplicate_window_ms=self.duplicate_window_ms,
        )

        def invoke(request: dict[str, Any], cancellation: Any) -> str:
            self.db.execute("INSERT INTO attempts VALUES (?, NULL)", (operation_id,))
            raw = runtime(request, cancellation)
            self.db.execute(
                "UPDATE attempts SET raw_output = ? WHERE operation_id = ?",
                (_json(raw), operation_id),
            )
            return raw

        coordinator.model_invoke = invoke
        calls_before = runtime.calls
        try:
            outcome = coordinator.process(
                source=message.body,
                raw_sender=message.sender,
                operation_id=operation_id,
                operation_config_hash=_hash(settings),
                received_at_epoch_ms=message.received_at_epoch_ms,
                received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
                source_event_key=row["source_key"] or operation_id,
                idempotency_key=operation_id,
                persisted_source_event_keys=frozenset(
                    item[0] for item in self.db.execute("SELECT source_key FROM ledger")
                ),
                recent_transactions=recent,
                current_source_id=row["source_key"],
            )
            self.db.execute("BEGIN IMMEDIATE")
            if self._operation(operation_id)["state"] != "in_flight":
                raise ValueError("runtime ownership was interrupted")
            if outcome.status == "eligible":
                persisted_sources = frozenset(
                    item[0] for item in self.db.execute("SELECT source_key FROM ledger")
                )
                recent_locked = self._recent_transactions(exclude_operation_id=operation_id)
                assert outcome.result and outcome.result.transaction and outcome.account_resolution
                tx = outcome.result.transaction
                dup_hit = is_within_duplicate_window(
                    minor_units=tx.minor_units,
                    currency=tx.currency,
                    direction=tx.direction.value,
                    account_id=outcome.account_resolution.account_id,
                    received_at_epoch_ms=message.received_at_epoch_ms,
                    recent_transactions=recent_locked,
                    current_source_id=row["source_key"],
                    current_operation_id=operation_id,
                    window_ms=self.duplicate_window_ms,
                )
                if dup_hit or (row["source_key"] or operation_id) in persisted_sources:
                    raw = self.db.execute(
                        "SELECT raw_output FROM attempts WHERE operation_id = ?", (operation_id,)
                    ).fetchone()[0]
                    coordinator.model_invoke = lambda *_args: json.loads(raw)
                    outcome = coordinator.process(
                        source=message.body,
                        raw_sender=message.sender,
                        operation_id=operation_id,
                        operation_config_hash=_hash(settings),
                        received_at_epoch_ms=message.received_at_epoch_ms,
                        received_timestamp_provenance=TimestampProvenance.PLATFORM_RECEIVED,
                        source_event_key=row["source_key"] or operation_id,
                        idempotency_key=operation_id,
                        persisted_source_event_keys=persisted_sources,
                        recent_transactions=recent_locked,
                        current_source_id=row["source_key"],
                    )
            payload = processing_result_payload(outcome)
            payload["review"] = outcome.review_case.to_dict() if outcome.review_case else None
            route = {
                "eligible": "transactions",
                "tombstoned_none": "tombstoned_none",
                "tombstoned_excluded": "tombstoned_excluded",
            }.get(outcome.status, "review")
            fingerprint = (
                outcome.duplicate_assessment.transaction_fingerprint
                if outcome.result and outcome.result.transaction and outcome.duplicate_assessment
                else None
            )
            if route == "transactions":
                assert (
                    outcome.account_resolution
                    and outcome.account_resolution.account_id
                    and outcome.result
                    and outcome.result.transaction
                )
                tx = outcome.result.transaction
                self._save(
                    row,
                    outcome.account_resolution.account_id,
                    tx.direction.value,
                    tx.minor_units,
                    tx.currency,
                    message.received_at_epoch_ms,
                    payload,
                )
            body_retained = 1 if outcome.body_retained else 0
            retained_evidence = row["evidence"] if outcome.body_retained else None
            if not outcome.body_retained:
                payload["review"] = None
            self.db.execute(
                "UPDATE operations SET state = 'settled', route = ?, payload = ?, "
                "fingerprint = ?, evidence = ?, body_retained = ? WHERE id = ?",
                (route, _json(payload), fingerprint, retained_evidence, body_retained, operation_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.execute(
                "UPDATE operations SET state = 'interrupted', route = 'retryable' "
                "WHERE id = ? AND state = 'in_flight'",
                (operation_id,),
            )
            raise
        return RunReceipt(operation_id, "execute", route, runtime.calls > calls_before)

    def recover_interrupted(self) -> int:
        return self.db.execute(
            "UPDATE operations SET state = 'interrupted', route = 'retryable' "
            "WHERE state = 'in_flight'"
        ).rowcount

    def confirm_account(self, operation_id: str, account_id: str, *, kind: str = "bank_account") -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._operation(operation_id)
            if self.db.execute(
                "SELECT 1 FROM operations WHERE parent_id = ?", (operation_id,)
            ).fetchone():
                raise ValueError("confirmation must target the latest operation")
            payload = json.loads(row["payload"] or "null")
            if row["route"] != "review" or not payload or payload["recognition_decision"] != "posted":
                raise ValueError("confirmation requires a validated posted Review")
            if payload["reason_codes"] != ["account_resolution_unresolved"]:
                raise ValueError("other Review reasons require separate resolution")
            semantic = payload["semantic_result"]
            received_ms = payload["receipt_timestamp"]["epoch_ms"]
            recent = self._recent_transactions(exclude_operation_id=operation_id)
            if is_within_duplicate_window(
                minor_units=semantic["money"]["minor_units"],
                currency=semantic["money"]["currency"],
                direction=semantic["direction"],
                account_id=account_id,
                received_at_epoch_ms=received_ms,
                recent_transactions=recent,
                current_source_id=row["source_key"],
                current_operation_id=operation_id,
                window_ms=self.duplicate_window_ms,
            ):
                raise ValueError("possible duplicate requires separate owner resolution")
            reference = semantic["account_reference"]
            entry = AccountCatalogEntry(account_id, kind, (reference,))
            existing = self.db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            aliases = list(json.loads(existing["aliases"])) if existing else []
            if existing and existing["kind"] != kind:
                raise ValueError("account type conflict")
            if reference not in aliases:
                aliases.append(reference)
            for other in self.db.execute("SELECT * FROM accounts WHERE id != ?", (account_id,)):
                if any(
                    normalize_account_alias(alias) == normalize_account_alias(reference)
                    for alias in json.loads(other["aliases"])
                ):
                    raise ValueError("account alias already belongs to another account")
            self.db.execute(
                "INSERT INTO accounts VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET aliases=excluded.aliases",
                (entry.account_id, entry.account_type, _json(aliases)),
            )
            self._save(
                row,
                account_id,
                semantic["direction"],
                semantic["money"]["minor_units"],
                semantic["money"]["currency"],
                received_ms,
                payload,
            )
            fingerprint = transaction_fingerprint(
                minor_units=semantic["money"]["minor_units"],
                currency=semantic["money"]["currency"],
                direction=semantic["direction"],
                account_id=account_id,
                received_at_epoch_ms=received_ms,
            )
            self.db.execute(
                "UPDATE operations SET route = 'transactions', fingerprint = ? WHERE id = ?",
                (fingerprint, operation_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def resolve_duplicate(self, operation_id: str, *, action: str) -> None:
        if action not in {"separate", "dismiss"}:
            raise ValueError("duplicate resolution action must be 'separate' or 'dismiss'")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._operation(operation_id)
            if self.db.execute(
                "SELECT 1 FROM operations WHERE parent_id = ?", (operation_id,)
            ).fetchone():
                raise ValueError("resolution must target the latest operation")
            payload = json.loads(row["payload"] or "null")
            if row["route"] != "review" or not payload or payload.get("reason_codes") != ["duplicate_possible"]:
                raise ValueError("duplicate resolution requires a duplicate_possible Review")
            if action == "dismiss":
                self.db.execute(
                    "UPDATE operations SET route = 'tombstoned_none', evidence = NULL, "
                    "body_retained = 0 WHERE id = ?",
                    (operation_id,),
                )
            else:
                semantic = payload["semantic_result"]
                account_id = payload["account_resolution"]["account_id"]
                received_ms = payload["receipt_timestamp"]["epoch_ms"]
                self._save(
                    row,
                    account_id,
                    semantic["direction"],
                    semantic["money"]["minor_units"],
                    semantic["money"]["currency"],
                    received_ms,
                    payload,
                )
                self.db.execute(
                    "UPDATE operations SET route = 'transactions' WHERE id = ?",
                    (operation_id,),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def mechanics_summary(self) -> dict[str, int]:
        return {
            "inputs": self.db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0],
            "operations": self.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            "model_calls": self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            "excluded_inputs": self.db.execute(
                "SELECT COUNT(*) FROM admissions WHERE route = 'tombstoned_excluded'"
            ).fetchone()[0],
            "tombstoned_none": self.db.execute(
                "SELECT COUNT(*) FROM operations WHERE route = 'tombstoned_none'"
            ).fetchone()[0],
            "reused_inputs": self.db.execute(
                "SELECT COUNT(*) FROM admissions WHERE action IN ('reuse', 'in_flight')"
            ).fetchone()[0],
            "transactions": self.db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
            "review_operations": self.db.execute(
                "SELECT COUNT(*) FROM operations WHERE route = 'review'"
            ).fetchone()[0],
        }

    def _recent_transactions(self, *, exclude_operation_id: str) -> list[RecentTransactionRecord]:
        rows = self.db.execute(
            "SELECT source_key, operation_id, account_id, direction, minor_units, currency, "
            "received_at_epoch_ms FROM ledger WHERE operation_id != ?",
            (exclude_operation_id,),
        ).fetchall()
        return [
            RecentTransactionRecord(
                account_id=r["account_id"],
                direction=r["direction"],
                minor_units=int(r["minor_units"]),
                currency=r["currency"],
                received_at_epoch_ms=int(r["received_at_epoch_ms"]),
                source_id=r["source_key"],
                operation_id=r["operation_id"],
            )
            for r in rows
        ]

    def _save(
        self,
        row: sqlite3.Row,
        account_id: str,
        direction: str,
        minor_units: int,
        currency: str,
        received_at_epoch_ms: int,
        payload: dict[str, Any],
    ) -> None:
        self.db.execute(
            "INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["source_key"] or row["id"],
                row["id"],
                account_id,
                direction,
                minor_units,
                currency,
                received_at_epoch_ms,
                _json(payload),
            ),
        )

    def _operation(self, operation_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("unknown operation")
        return row
