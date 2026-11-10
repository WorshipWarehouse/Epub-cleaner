"""Shared analysis context: resources, URLs and brand matching."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import PurifierConfig
from ..opf import PackageDocument

#: Optional domain suffix that belongs to a brand mention (``.com``, ``.co.uk``).
_DOMAIN_TAIL = re.compile(r"(?:\.[a-z]{2,12})+\b", re.IGNORECASE)


@dataclass
class Resource:
    """One archive member, parsed lazily."""

    path: str
    media_type: str
    data: bytes = b""
    text: str | None = None
    soup: Any = None
    parser: str = ""
    xml_declaration: bool = False

    @property
    def is_markup(self) -> bool:
        return self.soup is not None

    @property
    def basename(self) -> str:
        return posixpath.basename(self.path)

    @property
    def extension(self) -> str:
        return posixpath.splitext(self.path)[1].lower()


@dataclass(frozen=True)
class URLCheck:
    """Classification of an attribute value that might be a URL."""

    raw: str
    scheme: str
    host: str | None
    path: str
    is_remote: bool
    is_protocol_relative: bool
    is_promo: bool
    is_tracker: bool
    is_data: bool
    is_script: bool

    @property
    def is_local(self) -> bool:
        return not (self.is_remote or self.is_protocol_relative or self.is_data or self.is_script)


@dataclass
class Context:
    """Everything the detectors need to reason about one book."""

    config: PurifierConfig
    package: PackageDocument
    resources: dict[str, Resource]
    spine_paths: list[str] = field(default_factory=list)
    nav_paths: set[str] = field(default_factory=set)
    nav_targets: set[str] = field(default_factory=set)
    protected_paths: set[str] = field(default_factory=set)
    referenced_images: set[str] = field(default_factory=set)
    metadata: dict[str, str] = field(default_factory=dict)
    #: Cached Pillow dimensions for referenced images (None when unreadable).
    dimensions: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    #: Non-fatal notes collected while analysing or cleaning.
    warnings: list[str] = field(default_factory=list)

    # -- brand matching --------------------------------------------------
    def brand_hits(self, text: str | None) -> list[str]:
        """Every brand mention, widened to include a trailing domain suffix.

        A footer that says "OceanofPDF.com" mentions more of the brand than the
        bare name, and counting the domain keeps the dominance signal honest for
        the plain-text promotions that appear in converted books.
        """

        if not text:
            return []
        hits: list[str] = []
        for match in self.config.brand_regex.finditer(text):
            tail = _DOMAIN_TAIL.match(text, match.end())
            hits.append(text[match.start() : tail.end() if tail else match.end()])
        return hits

    def has_brand(self, text: str | None) -> bool:
        return bool(text and self.config.brand_regex.search(text))

    def promo_keyword(self, text: str | None) -> str | None:
        if not text:
            return None
        lowered = text.lower()
        for keyword in self.config.promo_keywords:
            if keyword in lowered:
                return keyword
        return None

    def hint_match(self, attributes: str | None, hints: tuple[str, ...]) -> str | None:
        if not attributes:
            return None
        lowered = attributes.lower()
        for hint in hints:
            if hint in lowered:
                return hint
        return None

    def is_citation_context(self, attributes: str | None, inside_citation: bool) -> bool:
        return inside_citation or self.hint_match(attributes, self.config.citation_hints) is not None

    # -- URLs ------------------------------------------------------------
    def classify(self, raw: str | None) -> URLCheck | None:
        if raw is None:
            return None
        value = raw.strip().strip("\"'")
        if not value:
            return None
        lowered = value.lower()
        if lowered.startswith(("javascript:", "vbscript:", "livescript:")):
            return URLCheck(
                raw=value,
                scheme=lowered.split(":", 1)[0],
                host=None,
                path="",
                is_remote=False,
                is_protocol_relative=False,
                is_promo=False,
                is_tracker=False,
                is_data=False,
                is_script=True,
            )
        if lowered.startswith("data:"):
            return URLCheck(
                raw=value,
                scheme="data",
                host=None,
                path="",
                is_remote=False,
                is_protocol_relative=False,
                is_promo=False,
                is_tracker=False,
                is_data=True,
                is_script=False,
            )

        protocol_relative = lowered.startswith("//")
        split = urlsplit(value if not protocol_relative else f"http:{value}")
        host = (split.hostname or "").lower().rstrip(".") or None
        scheme = (split.scheme or "").lower()
        is_remote = bool(host) and (scheme in {"http", "https", "ftp", "ftps"} or protocol_relative)
        return URLCheck(
            raw=value,
            scheme=scheme,
            host=host,
            path=split.path,
            is_remote=is_remote,
            is_protocol_relative=protocol_relative,
            is_promo=bool(host and (self.is_promo_host(host) or self.has_brand(host))),
            is_tracker=bool(host and self.is_tracker_host(host)),
            is_data=False,
            is_script=False,
        )

    def is_promo_host(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for domain in self.config.promo_domains:
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def is_tracker_host(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for domain in self.config.tracker_domains:
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def is_promo_url(self, raw: str | None) -> bool:
        check = self.classify(raw)
        return bool(check and (check.is_promo or (check.host and self.has_brand(check.host))))


def describe_element(element: Any) -> str:
    """Build a short CSS-like path for the report (p.footer-note > a)."""

    parts: list[str] = []
    current = element
    while current is not None and len(parts) < 3:
        name = getattr(current, "name", None)
        if not isinstance(name, str):
            break
        local = name.rsplit(":", 1)[-1]
        if local.lower() in {"html", "[document]"}:
            break
        segment = local
        attrs = getattr(current, "attrs", None) or {}
        ident = attrs.get("id")
        classes = attrs.get("class")
        if ident:
            segment += f"#{ident}"
        elif classes:
            if isinstance(classes, str):
                classes = classes.split()
            segment += "." + ".".join(list(classes)[:2])
        parts.append(segment)
        current = getattr(current, "parent", None)
    if not parts:
        return "(document)"
    return " > ".join(reversed(parts))


def truncate(value: str | None, limit: int = 140) -> str | None:
    if not value:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def count_words(text: str | None) -> int:
    return len((text or "").split())
