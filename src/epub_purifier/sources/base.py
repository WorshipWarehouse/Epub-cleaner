"""Shared types and helpers for book sources."""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Protocol


#: A source failed to answer: network trouble, quota, or an unreadable response.
class SourceError(Exception):
    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return self.message if not self.detail else f"{self.message} ({self.detail})"


class DownloadRefused(SourceError):
    """The source will not hand us a file (no EPUB edition, no authorisation)."""


class SearchQueryError(SourceError):
    """The search text is not usable."""


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 200


def normalize_query(raw: str) -> str:
    """Collapse whitespace and reject text we should not forward to a source."""

    if not isinstance(raw, str):
        raise SearchQueryError("Search text must be a string.")
    query = " ".join(raw.split())
    if len(query) < MIN_QUERY_LENGTH:
        raise SearchQueryError(f"Enter at least {MIN_QUERY_LENGTH} characters.")
    if len(query) > MAX_QUERY_LENGTH:
        raise SearchQueryError(f"Keep the search under {MAX_QUERY_LENGTH} characters.")
    if _CONTROL_CHARS.search(query):
        raise SearchQueryError("Search text contains control characters.")
    return query


_ISBN_IN_TEXT = re.compile(
    r"(?<![\dXx])(?:(?:\d[\s-]*){13}|(?:\d[\s-]*){9}[\dXx])(?![\dXx])"
)


def normalize_text(value: object) -> str:
    """Case-fold, remove diacritics and punctuation, and collapse whitespace.

    This is used only for matching. Display strings are never rewritten with it.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"['\u2018\u2019\u02bc`]", "", text)
    text = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(text.split())


def tokenize(value: object) -> tuple[str, ...]:
    text = normalize_text(value)
    return tuple(text.split()) if text else ()


def normalize_isbn(value: object) -> str:
    """Return a valid ISBN-10 or ISBN-13, or an empty string."""

    if not isinstance(value, str):
        return ""
    candidate = re.sub(r"^\s*(?:urn:)?isbn(?:10|13)?[\s:-]*", "", value, flags=re.I)
    compact = re.sub(r"[^0-9Xx]", "", candidate).upper()
    if len(compact) == 10 and _valid_isbn10(compact):
        return compact
    if len(compact) == 13 and compact.isdigit() and _valid_isbn13(compact):
        return compact
    return ""


def isbn_candidates(value: object) -> tuple[str | None, bool]:
    """Find one ISBN in text and say whether ISBN-like input was present.

    The boolean is true for a 10/13-character candidate even when its check
    digit is wrong, so an invalid ISBN search can return no results rather
    than accidentally becoming a title search.
    """

    if not isinstance(value, str):
        return None, False
    match = _ISBN_IN_TEXT.search(value)
    if match is None:
        return None, False
    normalized = normalize_isbn(match.group(0))
    return normalized or None, True


def isbn_keys(value: object) -> frozenset[str]:
    """Return the ISBN plus its equivalent 10/13 form when one exists."""

    normalized = normalize_isbn(value)
    if not normalized:
        return frozenset()
    keys = {normalized}
    if len(normalized) == 10:
        keys.add(_isbn13_from_isbn10(normalized))
    elif normalized.startswith("978"):
        keys.add(_isbn10_from_isbn13(normalized))
    return frozenset(keys)


def _valid_isbn10(value: str) -> bool:
    if len(value) != 10 or not value[:9].isdigit() or not (value[9].isdigit() or value[9] == "X"):
        return False
    total = sum((10 - index) * (10 if digit == "X" else int(digit)) for index, digit in enumerate(value))
    return total % 11 == 0


def _valid_isbn13(value: str) -> bool:
    if len(value) != 13 or not value.isdigit():
        return False
    total = sum((1 if index % 2 == 0 else 3) * int(digit) for index, digit in enumerate(value))
    return total % 10 == 0


def _isbn13_from_isbn10(value: str) -> str:
    core = "978" + value[:9]
    total = sum((1 if index % 2 == 0 else 3) * int(digit) for index, digit in enumerate(core))
    return core + str((10 - total % 10) % 10)


def _isbn10_from_isbn13(value: str) -> str:
    core = value[3:12]
    total = sum((10 - index) * int(digit) for index, digit in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


@dataclass(frozen=True)
class BookListing:
    """One source's view of a book (or of a source we can only link to)."""

    source: str
    source_label: str
    book_id: str
    title: str
    authors: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    isbn: tuple[str, ...] = ()
    details_url: str | None = None
    cover_url: str | None = None
    downloadable: bool = False
    download_note: str = ""
    is_search_link: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_label": self.source_label,
            "book_id": self.book_id,
            "title": self.title,
            "authors": list(self.authors),
            "languages": list(self.languages),
            "formats": list(self.formats),
            "isbn": list(self.isbn),
            "details_url": self.details_url,
            "cover_url": self.cover_url,
            "downloadable": self.downloadable,
            "download_note": self.download_note,
            "is_search_link": self.is_search_link,
        }


@dataclass(frozen=True)
class BookResult:
    """One book, with the source listings that were matched to it."""

    title: str
    authors: tuple[str, ...]
    isbn: tuple[str, ...]
    languages: tuple[str, ...]
    formats: tuple[str, ...]
    listings: tuple[BookListing, ...]
    score: float
    confidence: float
    match: str

    @property
    def primary(self) -> BookListing:
        return self.listings[0]

    @property
    def source(self) -> str:
        return self.primary.source

    @property
    def source_label(self) -> str:
        return self.primary.source_label

    @property
    def book_id(self) -> str:
        return self.primary.book_id

    @property
    def downloadable(self) -> bool:
        return self.primary.downloadable

    @property
    def details_url(self) -> str | None:
        return self.primary.details_url

    @property
    def cover_url(self) -> str | None:
        return self.primary.cover_url

    @property
    def source_count(self) -> int:
        return sum(1 for listing in self.listings if not listing.is_search_link)

    def to_dict(self) -> dict[str, object]:
        payload = self.primary.to_dict()
        payload.update(
            {
                "title": self.title,
                "authors": list(self.authors),
                "isbn": list(self.isbn),
                "languages": list(self.languages),
                "formats": list(self.formats),
                "listings": [listing.to_dict() for listing in self.listings],
                "source_count": self.source_count,
                "match": {
                    "type": self.match,
                    "score": round(self.score, 3),
                    "confidence": round(self.confidence, 3),
                },
            }
        )
        return payload


@dataclass(frozen=True)
class DownloadResult:
    """Bytes fetched by a source, before any scanning or cleaning."""

    data: bytes
    file_name: str
    media_type: str = "application/epub+zip"
    origin_url: str | None = None


@dataclass
class SourceStatus:
    """Per-source outcome of one search, including failures."""

    name: str
    label: str
    supports_download: bool
    ok: bool = True
    error: str | None = None
    result_count: int = 0
    raw_count: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "supports_download": self.supports_download,
            "ok": self.ok,
            "error": self.error,
            "result_count": self.result_count,
            "raw_count": self.raw_count,
            "note": self.note,
        }


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: tuple[BookResult, ...]
    sources: tuple[SourceStatus, ...]
    source_links: tuple[BookListing, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "results": [result.to_dict() for result in self.results],
            "sources": [status.to_dict() for status in self.sources],
            "source_links": [listing.to_dict() for listing in self.source_links],
        }


class BookSource(Protocol):
    """The contract every adapter implements."""

    name: str
    label: str
    supports_download: bool
    note: str

    def search(self, query: str) -> list[BookListing]: ...

    def download(self, book_id: str) -> DownloadResult: ...


class MinInterval:
    """Serialize calls to one host to at most one every ``seconds``.

    Catalogue sites ask clients to stay under roughly one request per second;
    this keeps a single process inside that budget without a scheduler.
    """

    def __init__(
        self,
        seconds: float,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.seconds = seconds
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                remaining = self.seconds - (now - self._last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last = self._clock()


__all__ = [
    "BookListing",
    "BookResult",
    "BookSource",
    "DownloadRefused",
    "DownloadResult",
    "MinInterval",
    "SearchQueryError",
    "SearchResponse",
    "SourceError",
    "SourceStatus",
    "isbn_candidates",
    "isbn_keys",
    "normalize_isbn",
    "normalize_query",
    "normalize_text",
    "tokenize",
]
