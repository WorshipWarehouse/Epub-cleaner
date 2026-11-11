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
from .doab import DoabSource
from .gutenberg import GutenbergSource
from .internetarchive import InternetArchiveSource
from .links import ANNAS_ARCHIVE, OCEAN_OF_PDF, LinkOnlySource
from .oapen import OapenSource
from .openlibrary import OpenLibrarySource
from .registry import default_sources, get_source, search_books
from .standardebooks import StandardEbooksSource
from .wikisource import WikisourceSource

__all__ = [
    "ANNAS_ARCHIVE",
    "BookListing",
    "BookSource",
    "DoabSource",
    "DownloadRefused",
    "DownloadResult",
    "GutenbergSource",
    "InternetArchiveSource",
    "LinkOnlySource",
    "MinInterval",
    "OCEAN_OF_PDF",
    "OapenSource",
    "OpenLibrarySource",
    "SearchQueryError",
    "SearchResponse",
    "SourceError",
    "SourceStatus",
    "StandardEbooksSource",
    "WikisourceSource",
    "default_sources",
    "get_source",
    "normalize_query",
    "search_books",
]
