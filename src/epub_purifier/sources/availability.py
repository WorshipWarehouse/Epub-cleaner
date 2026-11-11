"""Turn an external listing into a downloadable, openly licensed copy.

Anna's Archive and Ocean of PDF are represented by
:class:`~epub_purifier.sources.links.LinkOnlySource` adapters: they take part in
every search, but they never hand this server a file.  A catalogue that lists a
book it cannot serve - a record without an EPUB, a lending copy, an HTML-only
edition - has the same shape at the listing level: a real book with
``downloadable=False``.

This module bridges those two cases without contacting the link-only sites.
When a matched book has no downloadable copy, the *authorised* sources already
in the search are asked for that exact title and author.  If one of them holds
an openly licensed EPUB it becomes the card's primary listing, so the download
button points at a source this server is allowed to fetch from and the existing
:mod:`epub_purifier.transfer` pipeline takes over.  If none of them has it,
nothing changes: the card stays an honest "available elsewhere" listing, not an
error.

Lookups are deliberately bounded.  Only cards without a download are
considered, at most :data:`MAX_LOOKUPS` cards per search, a source that is
already on the card is not asked twice, and the first source that yields a
confirmed copy wins because the registry order is the preference order.  A
source that fails during a lookup is skipped rather than propagated, so a rate
limit, a timeout or an outage degrades that card to "available elsewhere"
instead of failing the whole search.
"""

from __future__ import annotations

from collections.abc import Sequence

from .base import BookListing, BookResult, BookSource, SourceError
from .matching import SearchCriteria, rank_listings

#: How many resolved cards a single search may try to enrich.
MAX_LOOKUPS = 2

#: Copies taken from one source before the lookup stops.
MAX_COPIES = 3

#: Ranking verdicts strong enough to mean "the same work", not merely
#: "something by that author".  ``author`` and ``fuzzy_title`` are excluded on
#: purpose: a lookup must never attach the wrong book to a card.
ACCEPTED_MATCHES = frozenset({"isbn", "exact_title", "title_author", "title"})

#: The structured scorer tops out at 1000 for an exact title and author; 900
#: keeps near variants (a subtitle, a spelling variant) and drops the rest.
MIN_MATCH_SCORE = 900.0


def needs_authorised_lookup(result: BookResult) -> bool:
    """True when a matched book has listings but no downloadable one."""

    if not result.listings:
        return False
    return not any(
        listing.downloadable and not listing.is_search_link
        for listing in result.listings
    )


def lookup_criteria(result: BookResult) -> SearchCriteria | None:
    """Build the structured query for one card, or ``None`` if unusable."""

    title = (result.title or "").strip()
    if not title:
        return None
    author = next((name.strip() for name in result.authors if name and name.strip()), "")
    criteria = SearchCriteria(query=title, title=title, author=author or None)
    # ``title`` came from a real listing, but a title that looks like an ISBN
    # would send the scorer down the ISBN path and match nothing.
    return None if criteria.isbn_like else criteria


def find_authorised_copies(
    result: BookResult,
    sources: Sequence[BookSource],
    *,
    max_copies: int = MAX_COPIES,
) -> tuple[BookListing, ...]:
    """Ask the downloadable sources for this exact work and return real copies.

    Only sources with an authorised download path are consulted, and only for
    listings that can actually be fetched.  A failing source is skipped.
    """

    criteria = lookup_criteria(result)
    if criteria is None:
        return ()

    present = {listing.source for listing in result.listings}
    copies: list[BookListing] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        if not source.supports_download or source.name in present:
            continue
        try:
            found = source.search(criteria.remote_query)
        except SourceError:
            # A rate limit or an outage here must not fail the search: the
            # card simply stays "available elsewhere".
            continue

        accepted: list[BookListing] = []
        for ranked in rank_listings(found, criteria):
            listing = ranked.listing
            if ranked.match not in ACCEPTED_MATCHES or ranked.score < MIN_MATCH_SCORE:
                continue
            if listing.is_search_link or not listing.downloadable or not listing.book_id:
                continue
            key = (listing.source, listing.book_id)
            if key in seen:
                continue
            seen.add(key)
            accepted.append(listing)
        if accepted:
            copies.extend(accepted)
            break
    return tuple(copies[:max_copies])


__all__ = [
    "ACCEPTED_MATCHES",
    "MAX_COPIES",
    "MAX_LOOKUPS",
    "MIN_MATCH_SCORE",
    "find_authorised_copies",
    "lookup_criteria",
    "needs_authorised_lookup",
]
