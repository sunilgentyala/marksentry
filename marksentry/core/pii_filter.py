"""
PII Masking Filter.

Pre-export compliance step that detects and masks sensitive patterns before
Markdown output is passed to RAG pipelines, vector stores, or shared storage.

Supported patterns
------------------
  - US Social Security Numbers  (SSN)
  - Email addresses
  - US/international phone numbers
  - Credit / debit card numbers  (with Luhn validation)
  - IPv4 addresses
  - AWS access keys and secret keys
  - Generic high-entropy API keys / bearer tokens
  - PEM private keys (RSA, EC, DSA, OpenSSH, PKCS#8)
  - JWT tokens
  - Passwords in key=value assignment patterns

Each masked value is replaced with a placeholder of the form
``[REDACTED:<TYPE>]`` so downstream consumers can see that data existed
without accessing the raw value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, NamedTuple


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

class _PiiPattern(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    validator: "Callable[[str], bool] | None" = None  # type: ignore[name-defined]


_PATTERNS: Final[list[_PiiPattern]] = [
    # PEM private keys — match the full block
    _PiiPattern(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
            re.MULTILINE,
        ),
    ),
    # AWS Access Key ID
    _PiiPattern(
        "AWS_ACCESS_KEY",
        re.compile(r"\b(?:AKIA|ABIA|ACCA|AGPA|AIDA|AIPA|AKIA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b"),
    ),
    # AWS Secret Access Key (40 base64 chars after common assignment patterns)
    _PiiPattern(
        "AWS_SECRET_KEY",
        re.compile(
            r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[=:]\s*"
            r"([A-Za-z0-9/+]{40})",
            re.IGNORECASE,
        ),
    ),
    # JWT (three base64url segments)
    _PiiPattern(
        "JWT_TOKEN",
        re.compile(
            r"eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_.+/=]+"
        ),
    ),
    # US Social Security Number
    _PiiPattern(
        "SSN",
        re.compile(r"\b(?!000|666|9\d{2})\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b"),
    ),
    # Email address
    _PiiPattern(
        "EMAIL",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
            re.IGNORECASE,
        ),
    ),
    # Credit/debit card (13-19 digits, optional separators) — Luhn-validated below
    _PiiPattern(
        "CREDIT_CARD",
        re.compile(r"\b(?:\d[ \-]?){13,19}\b"),
        validator=None,  # assigned after class def
    ),
    # US phone numbers  (+1 optional, various separator styles)
    _PiiPattern(
        "PHONE",
        re.compile(
            r"(?<!\d)(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)"
        ),
    ),
    # IPv4 address
    _PiiPattern(
        "IPV4",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
        ),
    ),
    # Password / secret in assignment expressions (loose heuristic)
    _PiiPattern(
        "PASSWORD_ASSIGNMENT",
        re.compile(
            r"""(?:password|passwd|secret|token|apikey|api_key)\s*[=:]\s*['"]?([^\s'"]{8,})['"]?""",
            re.IGNORECASE,
        ),
    ),
    # Generic high-entropy hex string (32+ chars — SHA-256, API keys, etc.)
    _PiiPattern(
        "HIGH_ENTROPY_HEX",
        re.compile(r"\b[0-9a-fA-F]{32,64}\b"),
    ),
]


# ---------------------------------------------------------------------------
# Luhn validation for credit card candidates
# ---------------------------------------------------------------------------

def _luhn_valid(digits: str) -> bool:
    clean = re.sub(r"[\s\-]", "", digits)
    if not clean.isdigit() or not (13 <= len(clean) <= 19):
        return False
    total = 0
    reverse = clean[::-1]
    for i, ch in enumerate(reverse):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# Patch the credit card pattern with its validator
_PATTERNS[_PATTERNS.index(
    next(p for p in _PATTERNS if p.name == "CREDIT_CARD")
)] = _PiiPattern(
    "CREDIT_CARD",
    re.compile(r"\b(?:\d[ \-]?){13,19}\b"),
    validator=_luhn_valid,
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class MaskingResult:
    masked_text: str
    redaction_count: int
    redaction_summary: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mask_pii(
    text: str,
    *,
    patterns: list[str] | None = None,
    placeholder_template: str = "[REDACTED:{type}]",
) -> MaskingResult:
    """
    Scan ``text`` for PII patterns and replace matches with placeholders.

    Parameters
    ----------
    text:
        The Markdown (or any plain text) to scrub.
    patterns:
        Optional allow-list of pattern names to apply (e.g. ``["SSN", "EMAIL"]``).
        If ``None``, all registered patterns are applied.
    placeholder_template:
        Format string for the replacement token.  ``{type}`` is substituted
        with the pattern name (e.g. ``REDACTED:EMAIL``).

    Returns
    -------
    MaskingResult with the sanitized text and counts by category.
    """
    active = [
        p for p in _PATTERNS
        if patterns is None or p.name in patterns
    ]

    summary: dict[str, int] = {}
    result = text

    for pat in active:
        replacement = placeholder_template.format(type=pat.name)
        count = 0

        def _replace(m: re.Match[str], r: str = replacement, v=pat.validator, pname=pat.name) -> str:
            nonlocal count
            raw = m.group(0)
            if v is not None and not v(raw):
                return raw
            count += 1
            return r

        result = pat.pattern.sub(_replace, result)
        if count:
            summary[pat.name] = summary.get(pat.name, 0) + count

    total = sum(summary.values())
    return MaskingResult(
        masked_text=result,
        redaction_count=total,
        redaction_summary=summary,
    )


def audit_pii(text: str) -> dict[str, list[str]]:
    """
    Return a report of found PII values without masking them.

    Useful for a dry-run / pre-flight check.  Does NOT write any output;
    only returns detected matches grouped by pattern name.
    """
    found: dict[str, list[str]] = {}
    for pat in _PATTERNS:
        matches = [
            m.group(0)
            for m in pat.pattern.finditer(text)
            if pat.validator is None or pat.validator(m.group(0))
        ]
        if matches:
            found[pat.name] = matches
    return found
