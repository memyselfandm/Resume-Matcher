"""Text normalization shared by parse checks and round-trip matching.

Extracted PDF text and source resume data differ in ways that are not parse
failures: ligatures, CSS-uppercased headings, typographic dashes and quotes,
soft line wraps, and HTML markup in rich-text bullets. Normalizing both sides
with the same pipeline keeps those differences from being reported as missing
content.
"""

from __future__ import annotations

import html
import re
import unicodedata

_DASHES = dict.fromkeys(
    map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-"
)
_QUOTES = {
    **dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032\uff07"), "'"),
    **dict.fromkeys(map(ord, "\u201c\u201d\u201e\u201f\u2033\uff02"), '"'),
}
_BULLETS = dict.fromkeys(map(ord, "\u2022\u2023\u2043\u25aa\u25cf\u25e6\u00b7\u25a0"), " ")
_TRANSLATION = {**_DASHES, **_QUOTES, **_BULLETS, 0x00A0: " ", 0x00AD: None}

_HTML_TAG_RE = re.compile(r"<[^<>]{0,500}>")
# A word broken across lines by a hyphen: "develop-\nment" -> "development".
_LINE_WRAP_HYPHEN_RE = re.compile(r"(\w)-\n(\w)")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^\W_]+(?:[.+#'][^\W_]+)*[+#]*", re.UNICODE)
# URLs and emails stay single tokens so a slug such as ".../jordan-rivera"
# never counts as the name "Jordan Rivera".
_URL_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?")


def strip_html(value: str) -> str:
    """Remove HTML tags and decode entities from source-side rich text."""
    without_tags = _HTML_TAG_RE.sub(" ", value)
    return html.unescape(without_tags)


def normalize_text(value: str, *, source_html: bool = False) -> str:
    """Return a comparison form: NFKC, casefolded, unified punctuation, one space.

    Args:
        value: Raw text from either the extracted document or source data.
        source_html: Strip HTML tags/entities first (source resume bullets).
    """
    if source_html:
        value = strip_html(value)
    value = unicodedata.normalize("NFKC", value)
    value = _LINE_WRAP_HYPHEN_RE.sub(r"\1\2", value)
    value = value.translate(_TRANSLATION)
    value = value.casefold()
    return _WHITESPACE_RE.sub(" ", value).strip()


def _link_token(chunk: str) -> str:
    return _URL_PREFIX_RE.sub("", chunk.strip(".,;:()[]<>|")).rstrip("/")


def tokenize(value: str) -> list[str]:
    """Split normalized text into word tokens (keeps c++, node.js, URLs, emails)."""
    tokens: list[str] = []
    for chunk in value.split():
        if ("/" in chunk or "@" in chunk) and any(char.isalnum() for char in chunk):
            link = _link_token(chunk)
            if "/" in link or "@" in link:
                tokens.append(link)
                continue
        tokens.extend(_TOKEN_RE.findall(chunk))
    return tokens
