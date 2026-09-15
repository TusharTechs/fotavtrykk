"""Organisation-number handling: the spine of the precision strategy.

Nothing is published about a company without a deterministic, re-checkable link
back to its organisasjonsnummer. This module is what makes that link.
"""

from __future__ import annotations

import re
import unicodedata

# Norwegian organisation numbers are nine digits with a MOD-11 check digit.
MOD11_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)

# "Org.nr 923 609 016", "Organisasjonsnummer: 923609016", "NO 923 609 016 MVA",
# "foretaksregisteret 923.609.016". The keyword anchor is what makes this precise.
_KEYWORD = r"(?:org(?:anisasjons)?\.?\s*(?:nr|nummer)?|foretaksregisteret|vat|mva|orgnr)"
KEYWORD_ORGNR_RE = re.compile(
    _KEYWORD + r"[^0-9]{0,24}?(\d[\d\s. -]{7,15}\d)",
    re.IGNORECASE,
)
# A bare nine-digit group, optionally split 3-3-3. Used only with checksum validation.
BARE_ORGNR_RE = re.compile(r"(?<!\d)(\d{3}[\s. ]?\d{3}[\s. ]?\d{3})(?!\d)")


def normalise(value: object) -> str:
    """Reduce any organisation-number spelling to its nine digits."""
    return re.sub(r"\D", "", str(value or ""))


def is_valid(org: str) -> bool:
    """MOD-11 check. Rejects most random nine-digit runs (phone numbers, dates)."""
    digits = normalise(org)
    if len(digits) != 9:
        return False
    total = sum(int(d) * w for d, w in zip(digits[:8], MOD11_WEIGHTS))
    remainder = total % 11
    if remainder == 0:
        check = 0
    elif remainder == 1:
        return False  # No valid check digit exists.
    else:
        check = 11 - remainder
    return check == int(digits[8])


def format_display(org: str) -> str:
    digits = normalise(org)
    return f"{digits[0:3]} {digits[3:6]} {digits[6:9]}" if len(digits) == 9 else digits


def _flatten(text: str) -> str:
    """Normalise unicode spacing so '923 609 016' matches '923 609 016'."""
    return unicodedata.normalize("NFKC", text or "").replace(" ", " ")


# Proof strengths, strongest first. The label is written into the observation's
# `identity_proof` field so a reviewer can re-check exactly how we decided.
PROOF_KEYWORD = "org_number_labelled_on_page"
PROOF_BARE = "org_number_on_page"
PROOF_REGISTRY_SITE = "registry_declared_website"
PROOF_NONE = ""

_PROOF_CONFIDENCE = {
    PROOF_KEYWORD: 1.0,
    PROOF_BARE: 0.97,
    PROOF_REGISTRY_SITE: 0.80,
}


def proof_confidence(proof: str) -> float:
    return _PROOF_CONFIDENCE.get(proof, 0.0)


def find_org_number_proof(text: str, expected: str) -> tuple[str, str | None]:
    """Look for `expected` in `text`.

    Returns (proof_label, evidence_span). The span is the literal substring a
    reviewer can search for, which is what the evaluator audits.

    Deliberately does NOT use the "strip every non-digit from the page and
    substring-match" trick: concatenating unrelated numbers manufactures false
    positives, and one wrong-company publication ends qualification.
    """
    want = normalise(expected)
    if len(want) != 9:
        return PROOF_NONE, None
    flat = _flatten(text)

    for match in KEYWORD_ORGNR_RE.finditer(flat):
        if normalise(match.group(1)) == want:
            return PROOF_KEYWORD, _span(flat, match.start(), match.end())

    for match in BARE_ORGNR_RE.finditer(flat):
        candidate = normalise(match.group(1))
        if candidate == want and is_valid(candidate):
            return PROOF_BARE, _span(flat, match.start(), match.end())

    return PROOF_NONE, None


def _span(text: str, start: int, end: int, pad: int = 45) -> str:
    """A short, quotable excerpt around the match."""
    excerpt = text[max(0, start - pad) : min(len(text), end + pad)]
    return " ".join(excerpt.split())


# --- legal-name fallback -------------------------------------------------
# Used only when no organisation number appears. Weaker, and labelled as such.

LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}

_NORDIC = str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})


def name_tokens(value: object) -> list[str]:
    """Normalise a Norwegian legal name to distinctive lowercase tokens."""
    text = str(value or "").translate(_NORDIC)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in LEGAL_AND_GENERIC and len(t) > 1]
