"""Detection patterns, limits and user configuration.

Everything that decides *what* counts as unwanted lives here so the rules can be
extended without touching the pipeline. Defaults target the promotional
insertions commonly added by third-party EPUB sources (Ocean of PDF being the
reference case) plus generic advertising and tracking infrastructure.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import UsageError


@dataclass(frozen=True)
class Limits:
    """Hard ceilings applied to untrusted archives.

    These exist to make ZIP bombs, decompression amplification and resource
    exhaustion cheap to reject instead of something we try to survive.
    """

    max_upload_bytes: int = 256 * 1024 * 1024  # 256 MiB
    max_entries: int = 5000
    max_total_uncompressed: int = 1024 * 1024 * 1024  # 1 GiB
    max_entry_uncompressed: int = 128 * 1024 * 1024  # 128 MiB
    max_compression_ratio: float = 120.0  # total uncompressed / compressed
    max_entry_ratio: float = 400.0
    max_path_depth: int = 16
    max_name_length: int = 255

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_upload_bytes": self.max_upload_bytes,
            "max_entries": self.max_entries,
            "max_total_uncompressed": self.max_total_uncompressed,
            "max_entry_uncompressed": self.max_entry_uncompressed,
            "max_compression_ratio": self.max_compression_ratio,
            "max_entry_ratio": self.max_entry_ratio,
        }


#: Case-insensitive patterns matching the reference promotional brand in all of
#: its common spellings ("Ocean of PDF", "OceanofPDF", "OCEAN-OF-PDF", ...).
DEFAULT_BRAND_PATTERNS: tuple[str, ...] = (
    r"ocean\s*[_\-.]?\s*of\s*[_\-.]?\s*pdf",
    r"oceanofpdf",
)

#: Domains that belong to the reference brand; links to them are promotional.
DEFAULT_PROMO_DOMAINS: frozenset[str] = frozenset(
    {
        "oceanofpdf.com",
        "www.oceanofpdf.com",
        "oceanofpdf.net",
        "oceanofpdf.org",
        "oceanofpdf.io",
        "oceanofpdf.co",
    }
)

#: Advertising, analytics and beacon endpoints. A resource pointing at one of
#: these is treated as tracking even when no brand pattern matches.
DEFAULT_TRACKER_DOMAINS: frozenset[str] = frozenset(
    {
        "google-analytics.com",
        "www.google-analytics.com",
        "googletagmanager.com",
        "www.googletagmanager.com",
        "doubleclick.net",
        "ad.doubleclick.net",
        "googlesyndication.com",
        "pagead2.googlesyndication.com",
        "googleadservices.com",
        "facebook.com",
        "connect.facebook.net",
        "pixel.facebook.com",
        "hotjar.com",
        "static.hotjar.com",
        "mixpanel.com",
        "api.mixpanel.com",
        "segment.com",
        "api.segment.io",
        "amplitude.com",
        "api.amplitude.com",
        "scorecardresearch.com",
        "quantserve.com",
        "quantcount.com",
        "newrelic.com",
        "bam.nr-data.net",
        "sentry.io",
        "criteo.com",
        "criteo.net",
        "taboola.com",
        "outbrain.com",
        "matomo.cloud",
        "statcounter.com",
        "clarity.ms",
        "branch.io",
        "appsflyer.com",
        "adjust.com",
        "onesignal.com",
        "pushengage.com",
        "propellerads.com",
        "popads.net",
        "exoclick.com",
        "juicyads.com",
    }
)

#: Words that, next to a brand mention, indicate promotional intent.
PROMO_KEYWORDS: tuple[str, ...] = (
    "download",
    "free ebook",
    "free epub",
    "free pdf",
    "more books",
    "more ebooks",
    "visit us",
    "visit our",
    "follow us",
    "join us",
    "subscribe",
    "donate",
    "support us",
    "buy the book",
    "buy now",
    "click here",
    "for more",
    "request a book",
    "request books",
    "converted by",
    "converted from",
    "scanned by",
    "edited by",
    "provided by",
    "courtesy of",
    "source:",
    "website:",
    "our website",
    "library",
    "ebook library",
    "share this",
    "sponsor",
    "advertisement",
    "promo",
)

#: Class/id/attribute hints that an element is promotional furniture.
PROMO_HINTS: tuple[str, ...] = (
    "promo",
    "advert",
    "ad-",
    "-ad",
    "banner",
    "watermark",
    "brand",
    "sponsor",
    "ocean",
    "credit",
    "footer-note",
    "bookmark",
)

#: Class/id hints that an element is legitimate quoted or cited material.
CITATION_HINTS: tuple[str, ...] = (
    "quote",
    "quotation",
    "cite",
    "citation",
    "epigraph",
    "excerpt",
    "footnote",
    "endnote",
    "reference",
    "bibliograph",
    "sourced",
    "attribution",
    "blockquote",
)

#: File extensions that have no business inside an EPUB.
EXECUTABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bat",
        ".cmd",
        ".com",
        ".scr",
        ".msi",
        ".jar",
        ".class",
        ".py",
        ".pyc",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".mjs",
        ".wsf",
        ".hta",
        ".lnk",
        ".pif",
        ".apk",
        ".dmg",
        ".app",
        ".deb",
        ".rpm",
        ".bin",
        ".elf",
        ".swf",
        ".php",
        ".asp",
        ".aspx",
        ".jsp",
        ".cgi",
        ".pl",
        ".sql",
        ".htaccess",
        ".htpasswd",
        ".ini",
        ".inf",
        ".reg",
        ".vxd",
        ".sys",
        ".drv",
        ".ocx",
        ".cpl",
        ".msix",
        ".appx",
    }
)

#: Junk files that packaging tools leave behind.
JUNK_NAMES: frozenset[str] = frozenset(
    {
        ".ds_store",
        "thumbs.db",
        "desktop.ini",
        "ehthumbs.db",
        ".directory",
        ".fseventsd",
        ".spotlight-v100",
        ".trashes",
        "readme.txt.bak",
        "~$temp.docx",
    }
)

#: Markup media types we treat as XHTML/HTML documents.
MARKUP_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/xhtml+xml",
        "text/html",
        "application/xml",
        "text/xml",
        "application/x-dtbook+xml",
    }
)

CSS_MEDIA_TYPES: frozenset[str] = frozenset({"text/css"})

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff", ".avif"}
)

SVG_MEDIA_TYPES: frozenset[str] = frozenset({"image/svg+xml"})

#: Front-matter documents that must never be dropped as "promotional junk".
PROTECTED_DOC_HINTS: tuple[str, ...] = (
    "cover",
    "titlepage",
    "title-page",
    "title_page",
    "copyright",
    "colophon",
    "dedication",
    "toc",
    "contents",
    "nav",
    "halftitle",
)


@dataclass(frozen=True)
class PurifierConfig:
    """Immutable configuration for one run."""

    brand_patterns: tuple[str, ...] = DEFAULT_BRAND_PATTERNS
    promo_domains: frozenset[str] = DEFAULT_PROMO_DOMAINS
    tracker_domains: frozenset[str] = DEFAULT_TRACKER_DOMAINS
    promo_keywords: tuple[str, ...] = PROMO_KEYWORDS
    promo_hints: tuple[str, ...] = PROMO_HINTS
    citation_hints: tuple[str, ...] = CITATION_HINTS

    #: Confidence at or above which an ambiguous finding is removed without
    #: asking. Findings between ``review_confidence`` and this value are shown
    #: for confirmation; below ``review_confidence`` they are informational.
    auto_remove_confidence: float = 0.85
    review_confidence: float = 0.5

    #: Remove all remote resources (not only trackers). Off by default because
    #: some legal books reference remote figures.
    remove_remote_resources: bool = False
    #: Strip links that survive as text (keeps the words, drops the URL).
    neutralize_promo_links: bool = True
    #: Drop meta tags, dc:* elements and OPF attributes carrying promotions.
    clean_metadata: bool = True
    #: Drop whole documents that exist only to advertise.
    drop_promo_pages: bool = True
    #: Remove scripts, event handlers, iframes, objects and JS URLs.
    sanitize_active_content: bool = True
    #: Drop executable archives, junk files and unsafe member names.
    drop_unsafe_files: bool = True
    #: Rewrite the modification timestamp of the package document.
    update_modified_timestamp: bool = True

    limits: Limits = field(default_factory=Limits)

    # -- derived helpers -------------------------------------------------
    @property
    def brand_regex(self) -> re.Pattern[str]:
        return _compile_brand(self.brand_patterns)

    def with_overrides(self, **overrides: Any) -> PurifierConfig:
        return replace(self, **overrides)

    # -- (de)serialisation ----------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_patterns": list(self.brand_patterns),
            "promo_domains": sorted(self.promo_domains),
            "tracker_domains": sorted(self.tracker_domains),
            "auto_remove_confidence": self.auto_remove_confidence,
            "review_confidence": self.review_confidence,
            "remove_remote_resources": self.remove_remote_resources,
            "neutralize_promo_links": self.neutralize_promo_links,
            "clean_metadata": self.clean_metadata,
            "drop_promo_pages": self.drop_promo_pages,
            "sanitize_active_content": self.sanitize_active_content,
            "drop_unsafe_files": self.drop_unsafe_files,
            "update_modified_timestamp": self.update_modified_timestamp,
            "limits": self.limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PurifierConfig:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise UsageError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key == "limits":
                values["limits"] = _limits_from_dict(value)
            elif key in {"promo_domains", "tracker_domains"}:
                values[key] = frozenset(str(v).lower().strip(".") for v in value)
            elif key in {"brand_patterns", "promo_keywords", "promo_hints", "citation_hints"}:
                values[key] = tuple(str(v) for v in value)
            else:
                values[key] = value
        config = cls(**values)
        _compile_brand(config.brand_patterns)  # validate early
        if not 0.0 <= config.review_confidence <= config.auto_remove_confidence <= 1.0:
            raise UsageError(
                "confidence thresholds must satisfy 0 <= review_confidence <= "
                "auto_remove_confidence <= 1"
            )
        return config


def _limits_from_dict(value: Any) -> Limits:
    if not isinstance(value, dict):
        raise UsageError("limits must be a mapping")
    known = set(Limits.__dataclass_fields__)
    unknown = set(value) - known
    if unknown:
        raise UsageError(f"unknown limit keys: {', '.join(sorted(unknown))}")
    return Limits(**value)


def _compile_brand(patterns: Iterable[str]) -> re.Pattern[str]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:  # pragma: no cover - defensive
            raise UsageError(f"invalid brand pattern {pattern!r}: {exc}") from exc
    if not compiled:
        raise UsageError("at least one brand pattern is required")
    return re.compile("|".join(f"(?:{p.pattern})" for p in compiled), re.IGNORECASE)


def load_config_file(path: str | Path) -> PurifierConfig:
    """Load a TOML or JSON configuration file."""

    path = Path(path)
    if not path.exists():
        raise UsageError(f"configuration file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".json"}:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UsageError(f"invalid JSON configuration: {exc}") from exc
    elif suffix in {".toml", ".tml", ""}:
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError as exc:
                raise UsageError(
                    "TOML configuration requires Python 3.11+ or the 'tomli' package; "
                    "use JSON instead"
                ) from exc
        try:
            data = tomllib.loads(raw)
        except Exception as exc:  # tomllib raises TOMLDecodeError
            raise UsageError(f"invalid TOML configuration: {exc}") from exc
    else:
        raise UsageError(f"unsupported configuration format: {suffix or '(none)'}")

    if not isinstance(data, dict):
        raise UsageError("configuration root must be a table/object")
    # Allow [purifier] or [tool.epub-purifier] wrappers.
    if "purifier" in data and isinstance(data["purifier"], dict):
        data = data["purifier"]
    elif "tool" in data and isinstance(data["tool"], dict):
        section = data["tool"].get("epub-purifier") or data["tool"].get("epub_purifier")
        if isinstance(section, dict):
            data = section
    return PurifierConfig.from_dict(data)
