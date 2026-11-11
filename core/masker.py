"""Phase 1: email preprocessing + "Catch Messy, Store Clean" PII masking vault.

Everything here runs in local. External AI providers only ever see masked_text.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

# ------------------------------------------------------------- preprocessing

_TAG_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h\d)\s*/?\s*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(
    r"<(?!(?:ORDER|PART|LABEL|LINK)_TOKEN_\d+>)[^>]+>", re.IGNORECASE
)
_STYLE_SCRIPT_RE = re.compile(
    r"<(style|script)[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)

# Reply headers: everything from here down is the quoted previous message.
_REPLY_HEADER_RE = re.compile(
    r"^\s*(On\s.+wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|From:\s.+$)",
    re.IGNORECASE | re.MULTILINE,
)
# Signature delimiters: a line that is only `--` or only a sign-off phrase.
_SIGNATURE_RE = re.compile(
    r"^\s*(--\s*|__+\s*|(best|kind|warm)?\s*regards,?|best,?|cheers,?|thanks,?|thank you,?|"
    r"sincerely,?|sent from my \w+.*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_html(text: str) -> str:
    text = _STYLE_SCRIPT_RE.sub("", text)
    text = _TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    return html.unescape(text).replace("\xa0", " ")


def preprocess_email(text: str) -> str:
    """Strip HTML, quoted replies, and signatures. Returns only email body."""
    text = strip_html(text or "").replace("\r\n", "\n")

    m = _REPLY_HEADER_RE.search(text)
    if m:
        text = text[: m.start()]

    text = "\n".join(
        line for line in text.split("\n") if not line.lstrip().startswith(">")
    )

    # Cut at the first sign-off that follows real content: a leading "Thanks," is not a signature.
    for m in _SIGNATURE_RE.finditer(text):
        if text[: m.start()].strip():
            text = text[: m.start()]
            break

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# -----------masking-----------

# Two- etter English words that must never be mistaken for a part prefix (for example "in 2024").
_STOPWORDS = (
    "am|an|as|at|be|by|do|go|he|if|in|is|it|me|my|no|of|on|or|so|to|up|us|we|qty|ord"
)

# Generated meeting links
LINK_RE = re.compile(r"https?://meet\.axmattsupply\.com/[A-Za-z0-9_-]+", re.IGNORECASE)

LABEL_RE = re.compile(r"\bUPS\s*-?\s*RMA\s*-?\s*ORD\s*-?\s*(\d{3,8})\b", re.IGNORECASE)

# ord/9988, ORD - 9988, ord9988, ORD#9988, ord_9988
ORDER_RE = re.compile(r"\bORD\s*(?:[-/_#:.]\s*)?(\d{3,8})\b", re.IGNORECASE)

# ab - 1234, AB/1234, ab1234, AB-1234
PART_RE = re.compile(
    rf"\b(?!(?:{_STOPWORDS})\b)([A-Z]{{2}})\s*(?:[-/_]\s*)?(\d{{4}})\b", re.IGNORECASE
)

TOKEN_RE = re.compile(r"<(ORDER|PART|LABEL|LINK)_TOKEN_(\d+)>")


def normalize_order(digits: str) -> str:
    return f"ORD-{digits}"


def normalize_part(prefix: str, digits: str) -> str:
    return f"{prefix.upper()}-{digits}"


def normalize_label(order_digits: str) -> str:
    return f"UPS-RMA-ORD-{order_digits}"


@dataclass
class MaskResult:
    raw_text: str
    clean_text: str
    masked_text: str
    vault: dict[str, str]
    new_tokens: dict[str, str] = field(default_factory=dict)


def _token_index(tok: str) -> int:
    m = TOKEN_RE.fullmatch(tok)
    assert m is not None
    return int(m.group(2))


def tokens_of(vault: dict[str, str], kind: str) -> list[str]:
    """All tokens of a kind (ORDER/PART/LABEL), ordered by index."""
    toks = [t for t in vault if (m := TOKEN_RE.fullmatch(t)) and m.group(1) == kind]
    return sorted(toks, key=_token_index)


def add_to_vault(
    vault: dict[str, str],
    kind: str,
    value: str,
    new_tokens: dict[str, str] | None = None,
) -> str:
    # Return the token for `value`, reusing an existing one or minting the next index.
    for tok, val in vault.items():
        if val == value and tok.startswith(f"<{kind}_TOKEN_"):
            return tok
    next_idx = 1 + max(
        (_token_index(t) for t in tokens_of(vault, kind)), default=0
    )
    tok = f"<{kind}_TOKEN_{next_idx}>"
    vault[tok] = value
    if new_tokens is not None:
        new_tokens[tok] = value
    return tok


def mask_text(
    text: str, vault: dict[str, str] | None = None, preprocess: bool = True
) -> MaskResult:
    """Preprocess first -> replace labels/orders/parts with tokens.

    `vault` is the existing thread vault; it is copied and merged, never overwritten.
    """
    vault = dict(vault or {})
    new_tokens: dict[str, str] = {}
    clean = preprocess_email(text) if preprocess else (text or "")

    masked = LINK_RE.sub(
        lambda m: add_to_vault(vault, "LINK", m.group(0), new_tokens), clean
    )
    masked = LABEL_RE.sub(
        lambda m: add_to_vault(vault, "LABEL", normalize_label(m.group(1)), new_tokens),
        masked,
    )
    masked = ORDER_RE.sub(
        lambda m: add_to_vault(vault, "ORDER", normalize_order(m.group(1)), new_tokens),
        masked,
    )
    masked = PART_RE.sub(
        lambda m: add_to_vault(
            vault, "PART", normalize_part(m.group(1), m.group(2)), new_tokens
        ),
        masked,
    )
    return MaskResult(
        raw_text=text,
        clean_text=clean,
        masked_text=masked,
        vault=vault,
        new_tokens=new_tokens,
    )


def unmask_text(text: str, vault: dict[str, str]) -> str:
    """Replace every known token with its real value. Unknown tokens are left intact."""
    return TOKEN_RE.sub(lambda m: vault.get(m.group(0), m.group(0)), text or "")


def contains_raw_pii(text: str) -> bool:
    """True if text still contains anything that looks like an order, part, label, or meeting link."""
    return bool(
        LINK_RE.search(text)
        or LABEL_RE.search(text)
        or ORDER_RE.search(text)
        or PART_RE.search(text)
    )
