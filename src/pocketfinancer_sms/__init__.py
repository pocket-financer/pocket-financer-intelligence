"""Local-first SMS processing foundation."""

from .analyzer import DeterministicSmsAnalyzer
from .currency import CurrencyContext
from .extractor import (
    ExtractorFieldEvidence,
    SourceSpan,
    build_extractor_input,
    collect_grounded_extractor_fields,
    parse_and_normalize_extraction,
    project_apple_fm_flat_output,
)
from .prefilter import PrefilterResult, prefilter
from .processing import (
    ExtractionCoordinator,
    evaluate_persistence,
    processing_result_payload,
)

__all__ = [
    "CurrencyContext",
    "DeterministicSmsAnalyzer",
    "ExtractionCoordinator",
    "ExtractorFieldEvidence",
    "PrefilterResult",
    "SourceSpan",
    "build_extractor_input",
    "collect_grounded_extractor_fields",
    "evaluate_persistence",
    "parse_and_normalize_extraction",
    "prefilter",
    "processing_result_payload",
    "project_apple_fm_flat_output",
]
