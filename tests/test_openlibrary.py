"""Open Library adapter: public-only listings, delegated downloads."""

from __future__ import annotations

import json

import pytest

from epub_purifier.sources.base import (
    DownloadRefused,
    DownloadResult,
    SearchQueryError,
    SourceError,
)
from epub_purifier.sources.net import Fetched
from epub_purifier.sources.openlibrary import OpenLibrarySource
from epub_purifier.sources.registry import default_sources

OL_SEARCH = {
    "numFound": 5,
    "docs": [
        {
            "key": "/works/OL66554W",
            "title": "Pride and Prejudice",
            "author_name": ["Austen, Jane"],
            "language": ["eng"],
            "ia": ["synapseml_audio_only", "prideandprejudic01342gut"],
            "ebook_access": "public",
        },
        {
            "key": "/works/OL2W",
            "title": "Borrowed Copy",
            "ia": ["lentcopy"],
            "ebook_access": "borrowable",
        },
        {"key": "/works/OL3W", "title": "No Scan", "ia": [], "ebook_access": "public"},
        {
            "key": "/works/OL4W",
            "title": "Bad Identifier",
            "ia": ["not a valid id"],
            "ebook_access": "public",
        },
        {
            "key": "/works/OL66554W",
            "title": "Duplicate",
            "ia": ["prideandprejudic01342gut"],
            "ebook_access": "public",
        },
        {
            "title": "Keyless Scan",
            "ia": ["keyless001"],
            "ebook_access": "public",
        },
    ],
}


def ol_fetch(payload: dict = OL_SEARCH, content_type: str = "application/json", body: bytes | None = None):
    calls: list[str] = []

    def fetch(url, **kwargs):
        calls.append(url)
        data = body if body is not None else json.dumps(payload).encode()
        return Fetched(url=url, content_type=content_type, data=data)

    return fetch, calls


class FakeArchive:
    def __init__(self, *, result: DownloadResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.downloads: list[str] = []
        self.covers: list[str] = []

    def download(self, book_id: str) -> DownloadResult:
        self.downloads.append(book_id)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        self.covers.append(book_id)
        return b"img", "image/jpeg"


def test_search_lists_only_public_scans_with_a_usable_identifier():
    fetch, calls = ol_fetch()
    listings = OpenLibrarySource(fetch=fetch).search("pride and prejudice")

    assert [listing.book_id for listing in listings] == [
        "prideandprejudic01342gut",
        "keyless001",
    ]
    first = listings[0]
    assert first.source == "openlibrary"
    assert first.source_label == "Open Library"
    assert first.title == "Pride and Prejudice"
    assert first.authors == ("Austen, Jane",)
    assert first.languages == ("eng",)
    assert first.formats == ("EPUB",)
    assert first.downloadable is True
    assert first.details_url == "https://openlibrary.org/works/OL66554W"
    assert first.cover_url == "https://archive.org/services/img/prideandprejudic01342gut"
    assert listings[1].details_url is None

    assert "q=pride+and+prejudice" in calls[0]
    assert "ebook_access=public" in calls[0]
    assert "fields=key%2Ctitle%2Cauthor_name" in calls[0]


def test_search_rejects_short_queries_without_fetching():
    fetch, calls = ol_fetch()
    with pytest.raises(SearchQueryError):
        OpenLibrarySource(fetch=fetch).search("a")
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content_type": "text/html"},
        {"body": b"{not json"},
        {"payload": {"docs": "not a list"}},
    ],
)
def test_search_rejects_unusable_responses(kwargs):
    fetch, _ = ol_fetch(**kwargs)
    with pytest.raises(SourceError):
        OpenLibrarySource(fetch=fetch).search("pride and prejudice")


def test_download_and_cover_delegate_to_internet_archive():
    archive = FakeArchive(
        result=DownloadResult(data=b"PK\x03\x04epub", file_name="Pride and Prejudice.epub")
    )
    source = OpenLibrarySource(
        fetch=lambda *a, **k: pytest.fail("the search fetch must not be used"),
        archive=archive,
    )

    result = source.download("prideandprejudic01342gut")
    assert result.file_name == "Pride and Prejudice.epub"
    assert archive.downloads == ["prideandprejudic01342gut"]

    assert source.cover("prideandprejudic01342gut") == (b"img", "image/jpeg")
    assert archive.covers == ["prideandprejudic01342gut"]


def test_download_refusal_is_preserved():
    archive = FakeArchive(error=DownloadRefused("Internet Archive lends this item"))
    with pytest.raises(DownloadRefused):
        OpenLibrarySource(archive=archive).download("lentcopy")
    assert archive.downloads == ["lentcopy"]


def test_registry_places_open_library_among_download_sources():
    sources = default_sources()
    assert [source.name for source in sources] == [
        "gutenberg",
        "wikisource",
        "internetarchive",
        "openlibrary",
        "standardebooks",
        "oapen",
        "doab",
    ]
    library = next(source for source in sources if source.name == "openlibrary")
    assert library.supports_download is True
    assert library.label == "Open Library"
    assert sum(source.supports_download for source in sources) == 7
