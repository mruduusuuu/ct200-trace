"""Text normalisation + hashing.

The CT-200 PDFs contain typographic ligatures (U+FB01 'fi', U+FB02 'fl', U+FB00 'ff') and
non-breaking hyphens (U+2011, emitted as their own span). Without NFKC + hyphen folding,
searching for "Specifications" or "CT-200" silently fails and hashes differ for no reason.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_TRANS = {
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2043: "-",
    0x00AD: None, 0x200B: None, 0x200C: None, 0x200D: None, 0xFEFF: None,
}


def normalize_text(s: str) -> str:
    return unicodedata.normalize("NFKC", s).translate(_TRANS)


def clean_line(s: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", normalize_text(s)).strip()


def canonical(s: str) -> str:
    """Whitespace-insensitive canonical form used for hashing and diffing."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in normalize_text(s).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def title_key(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", normalize_text(s).casefold()).strip()


def content_hash(title: str, body: str) -> str:
    payload = canonical(title) + "\n\u241f\n" + canonical(body)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def body_hash(body: str) -> str:
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
