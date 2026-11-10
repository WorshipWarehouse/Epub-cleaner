"""Markup parsing, inspection and serialization helpers.

XHTML inside EPUB is *usually* well-formed XML, but real-world books often carry
stray named entities or unclosed tags. We therefore parse strictly first and fall
back to the tolerant HTML parser, remembering which parser was used so the
report can say when a document had to be repaired.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

#: Tags that hold a paragraph-level run of content.
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "td",
        "th",
        "section",
        "article",
        "aside",
        "footer",
        "header",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "figcaption",
        "figure",
        "dd",
        "dt",
        "pre",
        "blockquote",
        "caption",
        "nav",
        "main",
    }
)

#: Tags whose presence makes a container "meaningful" even without text.
MEDIA_TAGS = frozenset(
    {"img", "svg", "image", "audio", "video", "table", "object", "canvas", "math", "picture"}
)

#: Elements that should never be decomposed when pruning empty wrappers.
STRUCTURAL_TAGS = frozenset({"html", "head", "body", "package", "opf:package"})

_WS = re.compile(r"\s+")
_ENTITY_DECL = re.compile(r"<!ENTITY\b", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!DOCTYPE\b[^>]*(?:\[[^\]]*\]|SYSTEM|PUBLIC)", re.IGNORECASE | re.DOTALL)
_DOCTYPE_FULL = re.compile(r"<!DOCTYPE\b(?:[^>\[]|\[[^\]]*\])*>", re.IGNORECASE | re.DOTALL)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def parse_markup(text: str, *, force_html: bool = False) -> tuple[BeautifulSoup, str]:
    """Parse markup, returning ``(soup, parser)`` where parser is xml or html.

    Documents that declare custom XML entities are parsed *after* their doctype
    is removed, so no entity is ever expanded while scanning untrusted input.
    """

    if has_entity_declarations(text):
        text = _DOCTYPE_FULL.sub("", text)

    if not force_html:
        try:
            soup = BeautifulSoup(text, "lxml-xml")
        except Exception:  # pragma: no cover - lxml rarely raises here
            soup = None
        if soup is not None and soup.find() is not None:
            return soup, "xml"

    soup = BeautifulSoup(text, "lxml")
    return soup, "html"


def has_entity_declarations(text: str) -> bool:
    """True when a document declares XML entities (classic XXE vehicle)."""

    for match in _DOCTYPE.finditer(text):
        if _ENTITY_DECL.search(match.group(0)):
            return True
    # A bare <!ENTITY outside a doctype is equally suspicious.
    return bool(_ENTITY_DECL.search(text))


def serialize_markup(soup: BeautifulSoup, parser: str, *, xml_declaration: bool) -> bytes:
    """Serialize a document back to bytes, preserving the XML declaration."""

    text = str(soup)
    if parser == "xml" and xml_declaration and not _XML_DECL.match(text):
        text = '<?xml version="1.0" encoding="utf-8"?>\n' + text
    return text.encode("utf-8")


def normalize_space(value: str | None) -> str:
    return _WS.sub(" ", (value or "")).strip()


def text_of(element: Tag | None, *, separator: str = " ") -> str:
    if element is None:
        return ""
    return normalize_space(element.get_text(separator))


def iter_tags(soup: BeautifulSoup | Tag, names: Iterable[str] | None = None) -> Iterator[Tag]:
    """Iterate real tags (not comments/doctype), optionally filtered by name."""

    wanted = set(names) if names is not None else None
    for element in soup.find_all(True):
        if wanted is None or local_name(element.name) in wanted:
            yield element


def local_name(name: str) -> str:
    """Strip any namespace prefix from a tag name."""

    return name.rsplit(":", 1)[-1].lower()


def nearest_block(element: Tag, *, limit: int = 6) -> Tag | None:
    """Walk up from a text-bearing node to the closest paragraph-like container."""

    current: Tag | None = element
    depth = 0
    while current is not None and depth <= limit:
        if local_name(current.name) in BLOCK_TAGS:
            return current
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
        depth += 1
    return None


def ancestors(element: Tag) -> Iterator[Tag]:
    parent = element.parent
    while isinstance(parent, Tag):
        yield parent
        parent = parent.parent


def is_inside(element: Tag, names: Iterable[str]) -> bool:
    wanted = {n.lower() for n in names}
    return any(local_name(a.name) in wanted for a in ancestors(element))


def has_media(element: Tag) -> bool:
    if local_name(element.name) in MEDIA_TAGS:
        return True
    return any(local_name(child.name) in MEDIA_TAGS for child in element.find_all(True))


def is_empty_container(element: Tag) -> bool:
    """True when nothing visible or meaningful remains inside the element."""

    if local_name(element.name) in STRUCTURAL_TAGS:
        return False
    if normalize_space(element.get_text(" ")):
        return False
    return not has_media(element)


def attribute_text(element: Tag) -> str:
    """Concatenated class/id/name/role values, lowercased, for hint matching."""

    parts: list[str] = []
    for key in ("class", "id", "name", "role", "epub:type", "type", "rel", "aria-label", "title"):
        value = element.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def string_nodes(element: Tag) -> Iterator[NavigableString]:
    for node in element.find_all(string=True):
        if isinstance(node, NavigableString) and node.strip():
            yield node


def clean_string(value: str, pattern: re.Pattern[str]) -> str:
    """Remove brand matches from a string and tidy the leftover punctuation."""

    cleaned = pattern.sub("", value)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\(\s*\)|\[\s*\]", "", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?)])", r"\1", cleaned)
    cleaned = re.sub(r"([(])\s+", r"\1", cleaned)
    return cleaned.strip(" \t-–—|·,;:")


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.match(r"\s*(\d+)", str(value))
    return int(match.group(1)) if match else None
