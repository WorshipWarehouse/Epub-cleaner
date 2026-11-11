"""Source registry and fan-out search."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace

from .availability import MAX_LOOKUPS, find_authorised_copies, needs_authorised_lookup
from .base import (
    BookListing,
    BookResult,
    BookSource,
    SearchResponse,
    SourceError,
    SourceStatus,
    normalize_query,
    normalize_text,
)
from .custom import load_extra_sources
from .doab import DoabSource
from .gutenberg import GutenbergSource
from .internetarchive import InternetArchiveSource
from .matching import SearchCriteria, group_listings, rank_listings
from .oapen import OapenSource
from .openlibrary import OpenLibrarySource
from .standardebooks import StandardEbooksSource
from .wikisource import WikisourceSource


def default_sources() -> list[BookSource]:
    """The built-in sources plus whatever this server added in its own file.

    Operator-added sources come last so the built-in catalogue order stays the
    download preference order used by the ranking and availability code.
    """

    built_in: list[BookSource] = [
        GutenbergSource(),
        WikisourceSource(),
        InternetArchiveSource(),
        OpenLibrarySource(),
        StandardEbooksSource(),
        OapenSource(),
        DoabSource(),
    ]
    extra = load_extra_sources(reserved=(source.name for source in built_in))
    return built_in + extra


def get_source(name: str, sources: Sequence[BookSource] | None = None) -> BookSource | None:
    for source in sources if sources is not None else default_sources():
        if source.name == name:
            return source
    return None


def search_books(
    query: str,
    sources: Sequence[BookSource] | None = None,
    *,
    title: str | None = None,
    author: str | None = None,
    isbn: str | None = None,
    language: str | None = None,
    format: str | None = None,
) -> SearchResponse:
    """Search every source, rank relevant books and group cross-source copies."""

    query = normalize_query(query)
    criteria = SearchCriteria(
        query=query,
        title=title,
        author=author,
        isbn=isbn,
        language=language,
        format=format,
    )
    available = list(sources if sources is not None else default_sources())
    remote_query = criteria.remote_query

    found_listings: list[BookListing] = []
    search_links: list[BookListing] = []
    statuses: list[SourceStatus] = []
    for source in available:
        try:
            found = source.search(remote_query)
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
                raw_count=len(found),
                note=source.note,
            )
        )
        for listing in found:
            if listing.is_search_link:
                search_links.append(listing)
            else:
                found_listings.append(listing)

    if statuses and all(not status.ok for status in statuses):
        raise SourceError(
            "No source could answer that search",
            detail="; ".join(
                f"{status.label}: {status.error}" for status in statuses if status.error
            ),
        )

    ranked = rank_listings(found_listings, criteria)
    groups = list(group_listings(ranked))
    if groups and search_links:
        groups = _attach_search_links(groups, search_links, available)
    groups = _resolve_downloads(groups, available)

    counts = Counter(
        listing.source
        for group in groups
        for listing in group.listings
        if not listing.is_search_link
    )
    for status in statuses:
        status.result_count = counts.get(status.name, 0)

    return SearchResponse(
        query=query,
        results=tuple(groups),
        sources=tuple(statuses),
        source_links=tuple(search_links) if not groups else (),
    )


def _attach_search_links(
    groups: list,
    search_links: list[BookListing],
    sources: Sequence[BookSource],
) -> list:
    """Add each link-only source to a matched book as an explicit search link.

    We cannot verify that Anna's Archive or Ocean of PDF hold a particular
    edition, so the link is labelled as a search link and never as availability.
    """

    by_name = {source.name: source for source in sources}
    templates: dict[str, BookListing] = {}
    for listing in search_links:
        templates.setdefault(listing.source, listing)

    result = []
    for group in groups:
        extra: list[BookListing] = []
        for source_name, template in templates.items():
            source = by_name.get(source_name)
            search_url = getattr(source, "search_url", None)
            query = group.title
            if group.authors:
                query = f"{query} {group.authors[0]}"
            details_url = search_url(query) if callable(search_url) else template.details_url
            extra.append(
                replace(
                    template,
                    title=group.title,
                    authors=group.authors,
                    isbn=(),
                    languages=(),
                    formats=(),
                    details_url=details_url,
                    cover_url=None,
                    downloadable=False,
                    is_search_link=True,
                )
            )
        result.append(replace(group, listings=group.listings + tuple(extra)))
    return result


def _resolve_downloads(
    groups: list[BookResult],
    sources: Sequence[BookSource],
) -> list[BookResult]:
    """Give a card a real download when an authorised source holds the work.

    Anna's Archive and Ocean of PDF are never contacted. Only sources with an
    authorised download path are asked, only cards without a download are
    considered, and at most :data:`MAX_LOOKUPS` cards per search pay for the
    extra requests.
    """

    if not groups:
        return groups
    downloadable = [source for source in sources if source.supports_download]
    if not downloadable:
        return groups

    remaining = MAX_LOOKUPS
    resolved: list[BookResult] = []
    for group in groups:
        if remaining <= 0 or not needs_authorised_lookup(group):
            resolved.append(group)
            continue
        remaining -= 1
        copies = find_authorised_copies(group, downloadable)
        resolved.append(_promote(group, copies) if copies else group)
    return resolved


def _promote(group: BookResult, copies: tuple[BookListing, ...]) -> BookResult:
    """Put a found download first so the card becomes downloadable.

    ``BookResult`` derives ``downloadable``, ``details_url`` and ``cover_url``
    from its primary listing, so reordering is what turns "available elsewhere"
    into a working download without reshaping the response.
    """

    return replace(
        group,
        listings=copies + group.listings,
        isbn=_merge_values(group.isbn, (listing.isbn for listing in copies)),
        languages=_merge_values(group.languages, (listing.languages for listing in copies)),
        formats=_merge_values(group.formats, (listing.formats for listing in copies)),
    )


def _merge_values(
    existing: tuple[str, ...],
    groups_of_values: Iterable[tuple[str, ...]],
) -> tuple[str, ...]:
    """Union listing fields the way cross-source grouping would have."""

    seen = {normalize_text(value) for value in existing}
    merged = list(existing)
    for values in groups_of_values:
        for value in values:
            key = normalize_text(value)
            if key and key not in seen:
                seen.add(key)
                merged.append(value)
    return tuple(merged)


__all__ = ["default_sources", "get_source", "search_books"]
