"""Shared types and helpers for book sources."""

from __future__ import annotations

import re
import threading
import time
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
            "details_url": self.details_url,
            "cover_url": self.cover_url,
            "downloadable": self.downloadable,
            "download_note": self.download_note,
            "is_search_link": self.is_search_link,
        }


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
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "supports_download": self.supports_download,
            "ok": self.ok,
            "error": self.error,
            "result_count": self.result_count,
            "note": self.note,
        }


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: tuple[BookListing, ...]
    sources: tuple[SourceStatus, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "results": [listing.to_dict() for listing in self.results],
            "sources": [status.to_dict() for status in self.sources],
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

    Sites such as Gutendex ask clients to stay under roughly one request per
    second; this keeps a single process inside that budget without a scheduler.
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
    "BookSource",
    "DownloadRefused",
    "DownloadResult",
    "MinInterval",
    "SearchQueryError",
    "SearchResponse",
    "SourceError",
    "SourceStatus",
    "normalize_query",
]
