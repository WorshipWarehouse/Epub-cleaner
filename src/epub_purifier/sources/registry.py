"""Source registry and fan-out search."""

from __future__ import annotations

from collections.abc import Sequence

from .base import (
    BookListing,
    BookSource,
    SearchResponse,
    SourceError,
    SourceStatus,
    normalize_query,
)
from .gutenberg import GutenbergSource
from .internetarchive import InternetArchiveSource
from .links import ANNAS_ARCHIVE, OCEAN_OF_PDF
from .openlibrary import OpenLibrarySource
from .wikisource import WikisourceSource


def default_sources() -> list[BookSource]:
    return [
        GutenbergSource(),
        WikisourceSource(),
        InternetArchiveSource(),
        OpenLibrarySource(),
        ANNAS_ARCHIVE,
        OCEAN_OF_PDF,
    ]


def get_source(name: str, sources: Sequence[BookSource] | None = None) -> BookSource | None:
    for source in sources if sources is not None else default_sources():
        if source.name == name:
            return source
    return None


def search_books(
    query: str,
    sources: Sequence[BookSource] | None = None,
) -> SearchResponse:
    """Search every source, keeping partial results when one of them fails."""

    query = normalize_query(query)
    available = list(sources if sources is not None else default_sources())

    results: list[BookListing] = []
    statuses: list[SourceStatus] = []
    for source in available:
        try:
            found = source.search(query)
        except SourceError as exc:
            statuses.append(
                SourceStatus(
                    name=source.name,
                    label=source.label,
                    supports_download=source.supports_download,
                    ok=False,
                    error=str(exc),
                    note=source.note,
                )
            )
            continue
        statuses.append(
            SourceStatus(
                name=source.name,
                label=source.label,
                supports_download=source.supports_download,
                ok=True,
                result_count=len(found),
                note=source.note,
            )
        )
        results.extend(found)

    if statuses and all(not status.ok for status in statuses):
        raise SourceError(
            "No source could answer that search",
            detail="; ".join(
                f"{status.label}: {status.error}" for status in statuses if status.error
            ),
        )
    return SearchResponse(query=query, results=tuple(results), sources=tuple(statuses))


__all__ = ["default_sources", "get_source", "search_books"]
