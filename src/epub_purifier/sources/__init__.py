"""Book search sources.

Each source is a small adapter with a uniform contract:

``search(query)`` returns :class:`BookListing` objects and ``download(book_id)``
returns a :class:`DownloadResult` **only** when the source offers an authorised
programmatic download. Sources that do not are link-only adapters: they return a
search link so users can open the site themselves.
"""

from __future__ import annotations

from .base import (
    BookListing,
    BookSource,
    DownloadRefused,
    DownloadResult,
    MinInterval,
    SearchQueryError,
    SearchResponse,
    SourceError,
    SourceStatus,
    normalize_query,
)
from .gutenberg import GutenbergSource
from .links import ANNAS_ARCHIVE, OCEAN_OF_PDF, LinkOnlySource
from .registry import default_sources, get_source, search_books

__all__ = [
    "ANNAS_ARCHIVE",
    "BookListing",
    "BookSource",
    "DownloadRefused",
    "DownloadResult",
    "GutenbergSource",
    "LinkOnlySource",
    "MinInterval",
    "OCEAN_OF_PDF",
    "SearchQueryError",
    "SearchResponse",
    "SourceError",
    "SourceStatus",
    "default_sources",
    "get_source",
    "normalize_query",
    "search_books",
]
