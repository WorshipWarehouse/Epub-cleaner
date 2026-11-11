"""Relevance scoring, filtering and cross-source grouping for book search.

Catalogue searches are deliberately noisy: a keyword query can return a
different edition, a study guide, or an unrelated book by the same author.  The
adapters stay responsible for fetching and parsing; this module decides whether
a returned listing is actually a match and which listings describe the same
book.

The scorer favours precision over recall.  A title must be exact, a close
variant, or strongly supported by a matching author.  ISBN searches never fall
back to title matching, and an invalid ISBN deliberately returns no books.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from .base import (
    BookListing,
    BookResult,
    isbn_candidates,
    isbn_keys,
    normalize_isbn,
    normalize_text,
    tokenize,
)

#: Scores below this are unrelated keyword matches, not book matches.
MIN_RELEVANCE = 680.0

_SOURCE_ORDER = {
    "gutenberg": 0,
    "standardebooks": 1,
    "internetarchive": 2,
    "openlibrary": 3,
    "wikisource": 4,
    "oapen": 5,
    "doab": 6,
}

_STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "edition",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "volume",
    "vol",
}

_LANGUAGE_ALIASES = {
    "ar": "ar",
    "arabic": "ar",
    "chinese": "zh",
    "de": "de",
    "deu": "de",
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "german": "de",
    "it": "it",
    "ita": "it",
    "ja": "ja",
    "japanese": "ja",
    "portuguese": "pt",
    "pt": "pt",
    "por": "pt",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "spanish": "es",
    "zh": "zh",
}

_FORMAT_ALIASES = {
    "ebook": "EPUB",
    "epub": "EPUB",
    "html": "HTML",
    "kindle": "MOBI",
    "mobi": "MOBI",
    "pdf": "PDF",
    "text": "TEXT",
    "txt": "TEXT",
}


@dataclass(frozen=True)
class SearchCriteria:
    """The user's query plus optional structured filters."""

    query: str
    title: str | None = None
    author: str | None = None
    isbn: str | None = None
    language: str | None = None
    format: str | None = None

    @property
    def isbn_query(self) -> str:
        explicit = normalize_isbn(self.isbn) if self.isbn else ""
        if explicit:
            return explicit
        if self.isbn and self.isbn.strip():
            return ""
        detected, _ = isbn_candidates(self.query)
        return detected or ""

    @property
    def isbn_like(self) -> bool:
        if self.isbn and self.isbn.strip():
            return True
        _, looks_like = isbn_candidates(self.query)
        return looks_like

    @property
    def text_query(self) -> str:
        return "" if self.isbn_like else self.query.strip()

    @property
    def remote_query(self) -> str:
        if self.isbn_like:
            return self.isbn_query or (self.isbn or self.query).strip()
        title = (self.title or "").strip()
        author = (self.author or "").strip()
        if title and author:
            return f"{title} {author}"
        return title or author or self.query.strip()

    @property
    def language_filter(self) -> str:
        return (self.language or "").strip()

    @property
    def format_filter(self) -> str:
        return (self.format or "").strip()


@dataclass(frozen=True)
class RankedListing:
    listing: BookListing
    score: float
    confidence: float
    match: str


def rank_listings(
    listings: Iterable[BookListing],
    criteria: SearchCriteria,
) -> list[RankedListing]:
    """Score listings, drop irrelevant ones, and sort the survivors."""

    if criteria.isbn_like and not criteria.isbn_query:
        return []

    ranked: list[RankedListing] = []
    seen: set[tuple[str, str]] = set()
    for listing in listings:
        if listing.is_search_link:
            continue
        key = (listing.source, listing.book_id)
        if key in seen:
            continue
        seen.add(key)
        if not _passes_filters(listing, criteria):
            continue
        scored = _score(listing, criteria)
        if scored is not None:
            ranked.append(scored)

    ranked.sort(
        key=lambda item: (
            -item.score,
            0 if item.listing.downloadable else 1,
            _SOURCE_ORDER.get(item.listing.source, 50),
            normalize_text(item.listing.title),
        )
    )
    return ranked


def group_listings(ranked: Iterable[RankedListing]) -> tuple[BookResult, ...]:
    """Merge listings for the same book while keeping editions separate."""

    items = list(ranked)
    if not items:
        return ()

    parents = list(range(len(items)))
    listing_isbns = [_all_isbn_keys(item.listing) for item in items]
    root_isbns = [set(keys) for keys in listing_isbns]

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        left_keys = root_isbns[left_root]
        right_keys = root_isbns[right_root]
        # An ISBN-less listing can match both editions; do not let it bridge
        # roots that already disagree about the edition.
        if left_keys and right_keys and left_keys.isdisjoint(right_keys):
            return
        parents[right_root] = left_root
        root_isbns[left_root] = left_keys | right_keys

    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if _same_book(
                items[left].listing,
                items[right].listing,
                listing_isbns[left],
                listing_isbns[right],
            ):
                union(left, right)

    groups: dict[int, list[RankedListing]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)

    results: list[BookResult] = []
    for members in groups.values():
        members.sort(
            key=lambda item: (
                -item.score,
                0 if item.listing.downloadable else 1,
                _SOURCE_ORDER.get(item.listing.source, 50),
            )
        )
        primary = members[0]
        result = BookResult(
            title=primary.listing.title,
            authors=primary.listing.authors,
            isbn=_isbn_union(member.listing for member in members),
            languages=_field_union(member.listing.languages for member in members),
            formats=_field_union(member.listing.formats for member in members),
            listings=tuple(member.listing for member in members),
            score=max(member.score for member in members),
            confidence=max(member.confidence for member in members),
            match=primary.match,
        )
        results.append(result)

    results.sort(
        key=lambda result: (
            -result.score,
            _SOURCE_ORDER.get(result.source, 50),
            normalize_text(result.title),
        )
    )
    return tuple(results)


def _score(listing: BookListing, criteria: SearchCriteria) -> RankedListing | None:
    if criteria.isbn_like:
        wanted = isbn_keys(criteria.isbn_query)
        available: set[str] = set()
        for value in listing.isbn:
            available.update(isbn_keys(value))
        if not wanted or not wanted.intersection(available):
            return None
        return RankedListing(
            listing=listing,
            score=1200.0,
            confidence=1.0,
            match="isbn",
        )

    explicit_title = (criteria.title or "").strip()
    explicit_author = (criteria.author or "").strip()
    if explicit_title or explicit_author:
        return _score_structured(listing, explicit_title, explicit_author)

    query = criteria.text_query
    if not query:
        return None

    query_tokens = tokenize(query)
    title_tokens = tokenize(listing.title)
    author_tokens = tokenize(" ".join(listing.authors))
    if not query_tokens or not title_tokens:
        return None

    title_coverage = _token_coverage(query_tokens, title_tokens)
    author_coverage = _token_coverage(query_tokens, author_tokens)
    query_significant = _significant(query_tokens)
    title_significant = _significant(title_tokens)

    if normalize_text(query) == normalize_text(listing.title):
        score = 1000.0
        match = "exact_title"
    elif query_significant and query_significant == title_significant:
        score = 960.0
        match = "exact_title"
    elif _significant_contained(query_significant, title_tokens):
        score = 880.0 + min(20.0, title_coverage * 20.0)
        match = "title_contains"
    elif title_coverage >= 0.78:
        score = 700.0 + title_coverage * 140.0
        match = "fuzzy_title"
    elif author_coverage >= 0.40 and title_coverage >= 0.30:
        score = 720.0 + title_coverage * 110.0 + author_coverage * 70.0
        match = "title_author"
    elif author_coverage >= 0.90:
        score = 670.0 + author_coverage * 80.0
        match = "author"
    else:
        return None

    if score < MIN_RELEVANCE:
        return None
    return RankedListing(
        listing=listing,
        score=score,
        confidence=min(1.0, score / 1000.0),
        match=match,
    )


def _score_structured(
    listing: BookListing,
    title_query: str,
    author_query: str,
) -> RankedListing | None:
    title_similarity = _field_similarity(title_query, listing.title) if title_query else 0.0
    author_similarity = (
        _field_similarity(author_query, " ".join(listing.authors)) if author_query else 0.0
    )

    if title_query and title_similarity < 0.70:
        return None
    if author_query and author_similarity < 0.65:
        return None
    if not title_query and not author_query:
        return None

    if title_query and author_query:
        score = 820.0 + title_similarity * 130.0 + author_similarity * 50.0
        match = "title_author"
    elif title_query:
        score = 700.0 + title_similarity * 250.0
        match = "title"
    else:
        score = 600.0 + author_similarity * 250.0
        match = "author"

    return RankedListing(
        listing=listing,
        score=score,
        confidence=min(1.0, score / 1000.0),
        match=match,
    )


def _passes_filters(listing: BookListing, criteria: SearchCriteria) -> bool:
    language = criteria.language_filter
    if language and language.lower() not in {"any", "all"}:
        wanted = _language_key(language)
        if not wanted or not any(_language_key(value) == wanted for value in listing.languages):
            return False

    file_format = criteria.format_filter
    if file_format and file_format.lower() not in {"any", "all"}:
        wanted = _format_key(file_format)
        if not wanted or not any(_format_key(value) == wanted for value in listing.formats):
            return False
    return True


def _same_book(
    left: BookListing,
    right: BookListing,
    left_isbns: set[str],
    right_isbns: set[str],
) -> bool:
    if left_isbns and right_isbns:
        # Different known ISBNs are different editions, even when the title is
        # the same. Keep them as separate cards so the user can choose.
        return bool(left_isbns.intersection(right_isbns))

    if _group_title(left) != _group_title(right):
        return False
    return _author_key(left) == _author_key(right)


def _group_title(listing: BookListing) -> str:
    title = re.sub(r"\s*\(\s*(?:1[5-9]|20)\d{2}\s*\)\s*$", "", listing.title)
    return normalize_text(title)


def _author_key(listing: BookListing) -> str:
    return " ".join(sorted(tokenize(" ".join(listing.authors))))


def _all_isbn_keys(listing: BookListing) -> set[str]:
    values: set[str] = set()
    for value in listing.isbn:
        values.update(isbn_keys(value))
    return values


def _isbn_union(listings: Iterable[BookListing]) -> tuple[str, ...]:
    seen: set[str] = set()
    values: list[str] = []
    for listing in listings:
        for value in listing.isbn:
            normalized = normalize_isbn(value)
            if normalized and normalized not in seen:
                seen.add(normalized)
                values.append(normalized)
    return tuple(values)


def _field_union(values: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for group in values:
        for value in group:
            key = normalize_text(value)
            if key and key not in seen:
                seen.add(key)
                result.append(value)
    return tuple(result)


def _significant(tokens: tuple[str, ...]) -> set[str]:
    significant = {token for token in tokens if token not in _STOPWORDS}
    return significant or set(tokens)


def _token_coverage(query: tuple[str, ...], field: tuple[str, ...]) -> float:
    if not query or not field:
        return 0.0
    return sum(_best_similarity(token, field) for token in query) / len(query)


def _significant_contained(query: set[str], field: tuple[str, ...]) -> bool:
    return bool(query) and bool(field) and all(
        _best_similarity(token, field) >= 0.82 for token in query
    )


def _best_similarity(token: str, field: tuple[str, ...]) -> float:
    best = 0.0
    for candidate in field:
        if token == candidate:
            return 1.0
        if len(token) <= 3 or len(candidate) <= 3:
            continue
        ratio = SequenceMatcher(None, token, candidate).ratio()
        # A short accidental character overlap ("dune" / "jane") must not
        # satisfy title/author coverage. Only genuine spelling variants pass.
        if ratio >= 0.78:
            best = max(best, ratio)
    return best


def _field_similarity(query: str, value: str) -> float:
    normalized_query = normalize_text(query)
    normalized_value = normalize_text(value)
    if not normalized_query or not normalized_value:
        return 0.0
    if normalized_query == normalized_value:
        return 1.0

    query_tokens = tokenize(query)
    value_tokens = tokenize(value)
    query_significant = _significant(query_tokens)
    value_significant = _significant(value_tokens)
    if query_significant and query_significant == value_significant:
        return 0.98

    coverage = _token_coverage(query_tokens, value_tokens)
    sequence = SequenceMatcher(None, normalized_query, normalized_value).ratio()
    return min(1.0, 0.8 * coverage + 0.2 * sequence)


def _language_key(value: str) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    first = normalized.split()[0]
    return _LANGUAGE_ALIASES.get(first, first[:2])


def _format_key(value: str) -> str:
    normalized = normalize_text(value)
    return _FORMAT_ALIASES.get(normalized, normalized.upper())


__all__ = [
    "MIN_RELEVANCE",
    "RankedListing",
    "SearchCriteria",
    "group_listings",
    "rank_listings",
]
