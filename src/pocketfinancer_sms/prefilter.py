"""Pure, deterministic, strictly fail-open SMS prefilter (sms-prefilter/1).

No storage, network, model runtime, or sender dependency: takes only an immutable
SMS body and excludes unambiguously non-transactional messages before inference.
Any uncertainty or completed-transaction cue always passes through to the SLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


POLICY_ID = "sms-prefilter/1"

_FLAGS = re.IGNORECASE | re.DOTALL

_HAS_DIGIT = re.compile(r"\d")

# Future/conditional/negated clauses (for example, "Once approved, money will
# be debited") must not count as completed movement inside the guard.
_FUTURE_OR_CONDITIONAL = re.compile(
    r"(?:"
    r"\b(?:once\s+approved|on\s+approving\s+the\s+request)\b[^.!?\n]*|"
    r"\b(?:will|shall|may|might|would|should|scheduled\s+to|set\s+to|about\s+to|going\s+to|yet\s+to|not(?:\s+been)?)\s+"
    r"(?:be\s+)?(?:debited|credited|spent|deducted|withdrawn|deposited|transferred|refunded|reversed|charged)\b"
    r")",
    _FLAGS,
)

# Never exclude via OTP, collect-request, or telecom rules if a completed
# movement phrase appears (for example, "Rs. 500 without OTP/PIN at MERCHANT").
_COMPLETED_GUARD = re.compile(
    r"\b(?:"
    r"debited|credited|spent|deducted|withdrawn|deposited|transferred|refunded|reversed|"
    r"autopaid|auto-?debited|sent\s+(?:rs|inr|₹)|money\s+transfer|"
    r"payment\s+of\s+(?:rs\.?|inr|₹).*(?:successful|received)|"
    r"transaction\s+of\s+(?:rs\.?|inr|₹).*successful|"
    r"has\s+been\s+received|received\s+towards|without\s+otp|txn\s+(?:rs\.?|inr|₹)"
    r")\b",
    _FLAGS,
)

_GAMING_PROMO = re.compile(
    r"\b(?:rummy|my11circle|dream11|junglee\s*rummy|mpl\s*pro|winzo)\b",
    _FLAGS,
)

_COLLECT_OR_MANDATE_REQUEST = re.compile(
    r"(?:"
    r"\bhas\s+requested\s+(?:rs\.?|inr|₹|money)\b|"
    r"\bonce\s+approved,?\s*(?:money|the\s+amount)\s+will\s+be\s+debited\b|"
    r"\bon\s+approving\s+the\s+request\b|"
    r"\bupi[- ]mandate\s+request\b|"
    r"\bcollect\s+request\b"
    r")",
    _FLAGS,
)

_OTP_VERIFICATION = re.compile(
    r"(?:"
    r"\b(?:otp|one[- ]time[- ]password|verification\s+code|security\s+code|"
    r"login\s+code|auth(?:entication)?\s+code)\b.*\b(?:is|for|:)\b|"
    r"\b\d{4,8}\b\s+is\s+(?:your|the)\s+(?:otp|one[- ]time[- ]password|"
    r"verification\s+code|security\s+code|secret\s+code|pin|mpin)|"
    r"\botp\s*(?:is|:|-)\s*\d{4,8}\b|"
    r"\buse\s+(?:otp\s*)?\d{4,8}\s+(?:as|to)\b"
    r")",
    _FLAGS,
)

_TELECOM_NOTICE = re.compile(
    r"(?:"
    r"\bdaily\s+data\s+quota\s+used\b|"
    r"\bhigh[- ]speed\s+data\b|"
    r"\bplan\s+has\s+expired\b|"
    r"प्लॅन\s+(?:समाप्त|लवकरच)|"
    r"कॉलरट्यून्स|"
    r"\bvi\s+super\s+hour\b|"
    r"\bvi\s+exclusive\b|"
    r"\bbinge\s+all\s+night\b|"
    r"\bcaller\s*tune\b|"
    r"\bmissed\s+call\s+alert\b|"
    r"\brecharge\s+(?:now|today|with\s+rs|your\s+(?:jio|vi|airtel))\b|"
    r"\bunlimited\s+pack\b.*\bexpir"
    r")",
    _FLAGS,
)

_BROKERAGE_EOD_REPORT = re.compile(
    r"(?:"
    r"\breported\s+your\s+fund\s+bal(?:ance)?\b|"
    r"\byour\s+traded\s+value\s+for\b.*national\s+stock\s+exchange|"
    r"\btotal\s+amount\s+payable\s+to\s+you\s+at\s+the\s+end\s+of\s+date\b"
    r")",
    _FLAGS,
)

_FINANCIAL_CUE = re.compile(
    r"(?:"
    r"rs\.?|inr|₹|usd|\$|eur|€|gbp|£|aed|sgd|aud|cad|jpy|¥|"
    r"\b(?:bank|a/c|acct|account|card|upi|vpa|neft|imps|rtgs|wallet|"
    r"paytm|phonepe|gpay|simpl|slice|onecard|cred|apay|amazon\s*pay|"
    r"debited|credited|spent|paid|deducted|withdrawn|deposited|transferred|"
    r"received|refund|reversed|txn|transaction|payment|bill|emi|mandate|"
    r"autopay|balance|salary|dividend|sip|redemption|payout)\b"
    r")",
    _FLAGS,
)


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    decision: Literal["pass", "exclude"]
    reason: str


def prefilter(body: str) -> PrefilterResult:
    """Classify an immutable SMS body; ``pass`` means send to the local SLM."""
    if not body or not body.strip():
        return PrefilterResult("exclude", "empty")
    if not _HAS_DIGIT.search(body):
        return PrefilterResult("exclude", "no_digits")
    if _GAMING_PROMO.search(body):
        return PrefilterResult("exclude", "gaming_promo")

    has_completed = bool(_COMPLETED_GUARD.search(_FUTURE_OR_CONDITIONAL.sub(" ", body)))
    if not has_completed and _COLLECT_OR_MANDATE_REQUEST.search(body):
        return PrefilterResult("exclude", "collect_or_mandate_request")
    if not has_completed and _OTP_VERIFICATION.search(body):
        return PrefilterResult("exclude", "otp_verification")
    if not has_completed and _TELECOM_NOTICE.search(body):
        return PrefilterResult("exclude", "telecom_notice")
    if _BROKERAGE_EOD_REPORT.search(body):
        return PrefilterResult("exclude", "brokerage_eod_report")
    if not _FINANCIAL_CUE.search(body):
        return PrefilterResult("exclude", "no_financial_cue")
    return PrefilterResult("pass", "eligible")
