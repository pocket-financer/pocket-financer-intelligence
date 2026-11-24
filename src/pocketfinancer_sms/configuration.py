"""Immutable, canonically hashed SMS processing configuration snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from .currency import ISO_MINOR_UNITS
from .extractor import EXTRACTOR_PROMPT, EXTRACTOR_VALIDATION_PROFILE
from .provenance import object_sha256
from .types import TimestampProvenance


PIPELINE_CONTRACT = "pocketfinancer.sms-pipeline/1"
PERSISTENCE_POLICY_CONTRACT = "pocketfinancer.persistence-policy/1"
EXTRACTOR_GRAMMAR_VERSION = "pocketfinancer.extractor-grammar/1"
ANALYZER_BEHAVIOR_VERSION = "pocketfinancer.structural-sms-analyzer/2"
APPLE_PROMPT_VERSION = "pocketfinancer.apple-fm-extractor-prompt/1"


class ProcessingTrigger(StrEnum):
    REALTIME = "realtime"
    HISTORICAL = "historical"
    MANUAL = "manual"
    RETRY = "retry"
    DIAGNOSTIC = "diagnostic"
    APP_INTENT = "app_intent"
    BACKGROUND_RECOVERY = "background_recovery"


class RolloutMode(StrEnum):
    AUTOMATIC = "automatic"


class WireTransport(StrEnum):
    DIRECT_JSON = "direct_json"
    APPLE_FM_GUIDED_FLAT = "apple_fm_guided_flat"


@dataclass(frozen=True, slots=True)
class AssetBinding:
    asset_id: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.asset_id or not _is_sha256(self.sha256):
            raise ValueError("configuration asset binding is invalid")

    def to_dict(self) -> dict[str, str]:
        return {"asset_id": self.asset_id, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ExtractorRuntimeConfig:
    eligible: bool
    ineligibility_reason: str | None
    model_identifier: str | None
    model_file_sha256: str | None
    model_identity_kind: str
    runtime_version: str
    os_version: str
    device_cohort: str
    prompt_sha256: str
    validation_profile_sha256: str
    grammar_sha256: str | None = None
    grammar_enabled: bool = False
    wire_transport: WireTransport = WireTransport.DIRECT_JSON
    prompt_version: str = EXTRACTOR_PROMPT
    validation_profile: str = EXTRACTOR_VALIDATION_PROFILE
    grammar_version: str = EXTRACTOR_GRAMMAR_VERSION
    generation_mode: str = "DIRECT_NON_THINKING"
    decoding: str = "greedy"
    answer_token_limit: int = 512
    raw_output_utf8_byte_limit: int = 16_384
    parser_deadline_ms: int = 0

    def __post_init__(self) -> None:
        if self.eligible:
            if self.ineligibility_reason is not None or not self.model_identifier:
                raise ValueError("eligible extractor runtime metadata is inconsistent")
        elif not self.ineligibility_reason:
            raise ValueError("ineligible extractor runtime requires a reason")
        if self.model_identity_kind not in {"file_sha256", "system_managed_runtime"}:
            raise ValueError("extractor model identity kind is unsupported")
        if self.model_identity_kind == "system_managed_runtime":
            if self.model_file_sha256 is not None:
                raise ValueError("system-managed extractor cannot bind a GGUF hash")
        elif self.eligible and not _is_sha256(self.model_file_sha256):
            raise ValueError("eligible file-backed extractor requires model SHA-256")
        if self.model_file_sha256 is not None and not _is_sha256(self.model_file_sha256):
            raise ValueError("extractor model file hash is invalid")
        if not isinstance(self.wire_transport, WireTransport):
            raise ValueError("extractor wire transport is unsupported")
        if not isinstance(self.grammar_enabled, bool):
            raise ValueError("grammar setting must be a boolean")
        if self.grammar_sha256 is not None and not _is_sha256(self.grammar_sha256):
            raise ValueError("extractor grammar hash is invalid")
        if self.wire_transport == WireTransport.DIRECT_JSON and self.grammar_sha256 is None:
            raise ValueError("direct JSON runtime requires a grammar hash binding")
        if not all(
            (self.runtime_version, self.os_version, self.device_cohort, self.prompt_version)
        ) or not all(
            _is_sha256(h) for h in (self.prompt_sha256, self.validation_profile_sha256)
        ):
            raise ValueError("extractor runtime provenance is incomplete")
        if (
            self.prompt_version not in {EXTRACTOR_PROMPT, APPLE_PROMPT_VERSION}
            or self.validation_profile != EXTRACTOR_VALIDATION_PROFILE
            or self.grammar_version != EXTRACTOR_GRAMMAR_VERSION
            or self.generation_mode != "DIRECT_NON_THINKING"
            or self.decoding != "greedy"
            or self.answer_token_limit != 512
            or self.raw_output_utf8_byte_limit != 16_384
            or self.parser_deadline_ms != 0
        ):
            raise ValueError("extractor runtime policy does not match the active profile")

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "ineligibility_reason": self.ineligibility_reason,
            "model_identifier": self.model_identifier,
            "model_file_sha256": self.model_file_sha256,
            "model_identity_kind": self.model_identity_kind,
            "runtime_version": self.runtime_version,
            "os_version": self.os_version,
            "device_cohort": self.device_cohort,
            "prompt_sha256": self.prompt_sha256,
            "grammar_sha256": self.grammar_sha256,
            "grammar_enabled": self.grammar_enabled,
            "validation_profile_sha256": self.validation_profile_sha256,
            "wire_transport": self.wire_transport.value,
            "prompt_version": self.prompt_version,
            "validation_profile": self.validation_profile,
            "grammar_version": self.grammar_version,
            "generation_mode": self.generation_mode,
            "decoding": self.decoding,
            "answer_token_limit": self.answer_token_limit,
            "raw_output_utf8_byte_limit": self.raw_output_utf8_byte_limit,
            "parser_deadline_ms": self.parser_deadline_ms,
        }


@dataclass(frozen=True, slots=True)
class ProcessingConfigSnapshot:
    operation_id: str
    parent_operation_id: str | None
    source_ref_hash: str
    trigger: ProcessingTrigger
    created_at_epoch_ms: int
    admission_epoch_ms: int
    release_id: str
    release_manifest_sha256: str
    analyzer_behavior_version: str
    unicode_behavior_version: str
    currency_asset_sha256: str
    profile_assets: tuple[AssetBinding, ...]
    primary_currency: str
    enabled_profile_ids: tuple[str, ...]
    received_at_epoch_ms: int
    received_timestamp_provenance: TimestampProvenance
    timezone_id: str
    extractor: ExtractorRuntimeConfig
    persistence_policy_version: str = PERSISTENCE_POLICY_CONTRACT
    rollout_mode: RolloutMode = RolloutMode.AUTOMATIC
    contract: str = PIPELINE_CONTRACT

    def __post_init__(self) -> None:
        _require_uuid(self.operation_id)
        if self.parent_operation_id is not None:
            _require_uuid(self.parent_operation_id)
            if self.parent_operation_id == self.operation_id:
                raise ValueError("operation cannot be its own parent")
        if self.trigger == ProcessingTrigger.RETRY and self.parent_operation_id is None:
            raise ValueError("retry requires an explicit parent operation")
        if not _is_sha256(self.source_ref_hash):
            raise ValueError("configuration source reference hash is invalid")
        if (
            not _is_nonnegative_int(self.created_at_epoch_ms)
            or not _is_nonnegative_int(self.admission_epoch_ms)
            or not _is_nonnegative_int(self.received_at_epoch_ms)
            or self.created_at_epoch_ms < self.admission_epoch_ms
        ):
            raise ValueError("configuration timestamps are invalid")
        if self.release_id != PIPELINE_CONTRACT or not _is_sha256(self.release_manifest_sha256):
            raise ValueError("configuration release provenance is invalid")
        if self.analyzer_behavior_version != ANALYZER_BEHAVIOR_VERSION or not self.unicode_behavior_version:
            raise ValueError("configuration analyzer provenance is incomplete")
        if not _is_sha256(self.currency_asset_sha256):
            raise ValueError("configuration currency hash is invalid")
        if self.primary_currency not in ISO_MINOR_UNITS:
            raise ValueError("configuration primary currency is unsupported")
        if not self.enabled_profile_ids or len(set(self.enabled_profile_ids)) != len(
            self.enabled_profile_ids
        ):
            raise ValueError("configuration profiles are invalid")
        if tuple(asset.asset_id for asset in self.profile_assets) != self.enabled_profile_ids:
            raise ValueError("configuration profile hashes do not match enabled profiles")
        if self.received_timestamp_provenance not in {
            TimestampProvenance.PLATFORM_RECEIVED,
            TimestampProvenance.ACQUISITION_SUPPLIED_MESSAGE_TIME,
        }:
            raise ValueError("extractor receipt timestamp provenance is unsupported")
        if not self.timezone_id:
            raise ValueError("configuration timezone is missing")
        if self.persistence_policy_version != PERSISTENCE_POLICY_CONTRACT:
            raise ValueError("persistence policy contract is unsupported")
        if self.rollout_mode != RolloutMode.AUTOMATIC:
            raise ValueError("pipeline operations require automatic routing")
        if self.contract != PIPELINE_CONTRACT:
            raise ValueError("configuration contract is unsupported")

    def payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "operation_id": self.operation_id,
            "parent_operation_id": self.parent_operation_id,
            "source_ref_hash": self.source_ref_hash,
            "trigger": self.trigger.value,
            "created_at_epoch_ms": self.created_at_epoch_ms,
            "admission_epoch_ms": self.admission_epoch_ms,
            "contract_release": {
                "release_id": self.release_id,
                "manifest_sha256": self.release_manifest_sha256,
            },
            "analyzer": {
                "behavior_version": self.analyzer_behavior_version,
                "unicode_behavior_version": self.unicode_behavior_version,
                "currency_asset_sha256": self.currency_asset_sha256,
                "profile_assets": [asset.to_dict() for asset in self.profile_assets],
            },
            "currency_context": {
                "primary_currency": self.primary_currency,
                "enabled_profile_ids": list(self.enabled_profile_ids),
            },
            "received_timestamp": {
                "epoch_ms": self.received_at_epoch_ms,
                "provenance": self.received_timestamp_provenance.value,
                "timezone_id": self.timezone_id,
                "read_only": True,
            },
            "extractor": self.extractor.to_dict(),
            "persistence_policy": {
                "version": self.persistence_policy_version,
                "rollout_mode": self.rollout_mode.value,
            },
        }

    @property
    def config_hash(self) -> str:
        return object_sha256(self.payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "config_hash": self.config_hash}


def _require_uuid(value: str) -> None:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("configuration operation ID is invalid") from exc
    if str(parsed) != value.lower():
        raise ValueError("configuration operation ID is not canonical")


def _is_sha256(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_nonnegative_int(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
