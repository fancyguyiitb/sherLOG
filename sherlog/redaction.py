"""The redaction gate — the single chokepoint between the evidence layer and any model.

Two passes:

*   **Pattern pass** — named regexes for formats we know (AWS keys, JWTs, connection
    strings, private key blocks, bearer tokens, emails, card numbers).
*   **Entropy pass** — Shannon entropy over candidate tokens, to catch secrets we have no
    pattern for. Gated on charset diversity so that trace IDs, span IDs and commit SHAs
    (lowercase hex, low diversity) survive: the agent needs those to correlate.

Redaction preserves the *type* (``[REDACTED:aws_key]``) so the agent can still reason
about "credentials appear here" without seeing the value.

A third pass neutralises instruction-shaped text. Logs are attacker-controlled input; the
containment story is layered — this pass, the untrusted-data delimiters applied at context
assembly, and the fact that the agent holds no write capability at all.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from .models import _GATE_KEY, Evidence, RedactedEvidence

# --- Pattern pass ----------------------------------------------------------------------

PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_key": re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
    "aws_secret": re.compile(r"(?i)\baws_secret_access_key\s*[=:]\s*\S+"),
    "private_key": re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "bearer_token": re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._\-]{16,}"),
    "conn_string": re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s\"']*:[^\s\"'@]+@[^\s\"']+"
    ),
    "slack_token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    "card_number": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
}

# Applied in this order; earlier entries win on overlap. Broad patterns go last so a
# connection string is not chewed up by the email matcher first.
_PATTERN_ORDER = [
    "private_key",
    "jwt",
    "conn_string",
    "aws_secret",
    "aws_key",
    "slack_token",
    "openai_key",
    "bearer_token",
    "card_number",
    "email",
]

# --- Entropy pass ----------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_\-]{24,}={0,2}")
_ENTROPY_MIN_BITS = 3.6
_ENTROPY_MIN_LEN = 24

# Fields the agent must be able to read to do its job at all. Values of these keys are
# exempt from the entropy pass; they are identifiers, not secrets.
_EXEMPT_KEYS = ("trace_id", "span_id", "commit_sha", "config_hash", "template_id", "job_id")
_EXEMPT_VALUE_RE = re.compile(
    r"(?:%s)\W{1,4}([A-Za-z0-9_\-]+)" % "|".join(_EXEMPT_KEYS)
)

# --- Prompt-injection containment ------------------------------------------------------

_INJECTION_RE = re.compile(
    r"(?i)\b("
    r"ignore (?:all )?(?:prior|previous|above) instructions"
    r"|disregard (?:all )?(?:prior|previous) (?:instructions|context)"
    r"|you are now (?:a|an|the)\b"
    r"|system prompt\s*[:>]"
    r"|new instructions\s*[:>]"
    r"|<\s*/?\s*(?:system|assistant|instructions)\s*>"
    r")"
)

INJECTION_MARKER = "[NEUTRALISED:injection_attempt]"


def shannon_entropy(s: str) -> float:
    """Bits per character. ~4.0 for hex, ~5–6 for base64 secrets, ~2–3 for English."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_diverse(s: str) -> bool:
    """Secrets mix cases and digits. Lowercase-hex identifiers do not."""
    return (
        any(c.islower() for c in s)
        and any(c.isupper() for c in s)
        and any(c.isdigit() for c in s)
    )


def _looks_secret(token: str) -> bool:
    return (
        len(token) >= _ENTROPY_MIN_LEN
        and _is_diverse(token)
        and shannon_entropy(token) >= _ENTROPY_MIN_BITS
    )


def scrub(text: str) -> tuple[str, list[str]]:
    """Redact ``text``. Returns the cleaned text and the list of redaction types applied.

    Pure and side-effect free so it can be unit-tested and reused by the canary check.
    """
    found: list[str] = []
    out = text

    for name in _PATTERN_ORDER:
        pattern = PATTERNS[name]
        if pattern.search(out):
            out = pattern.sub(f"[REDACTED:{name}]", out)
            found.append(name)

    exempt = {m.group(1) for m in _EXEMPT_VALUE_RE.finditer(out)}

    def _entropy_sub(match: re.Match[str]) -> str:
        token = match.group(0)
        if token in exempt or not _looks_secret(token):
            return token
        found.append("high_entropy")
        return "[REDACTED:high_entropy]"

    out = _TOKEN_RE.sub(_entropy_sub, out)

    if _INJECTION_RE.search(out):
        out = _INJECTION_RE.sub(INJECTION_MARKER, out)
        found.append("injection_attempt")

    # Preserve first-seen order without duplicates.
    return out, list(dict.fromkeys(found))


def redact(ev: Evidence) -> RedactedEvidence:
    """The gate. The only path from ``Evidence`` to something a model may see."""
    cleaned, kinds = scrub(ev.content)
    return RedactedEvidence(
        id=ev.id,
        kind=ev.kind,
        query=ev.query,
        t_start=ev.t_start,
        t_end=ev.t_end,
        content=cleaned,
        source_ref=ev.source_ref,
        raw_hash=ev.raw_hash,
        redactions=tuple(kinds),
        _key=_GATE_KEY,
    )


# --- Canary ----------------------------------------------------------------------------

#: Planted in a log line, a config value and a corpus document. CI asserts this string
#: never appears in an outbound model payload. A green canary test is the security claim
#: made checkable rather than asserted.
CANARY_SECRET = "AKIACANARYDONOTLEAK7"


def canary_clean(payload: str) -> bool:
    """True when a serialised model payload is free of the canary."""
    return CANARY_SECRET not in payload
