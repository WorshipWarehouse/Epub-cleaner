"""Source adapters: parsing, rate limits, downloads, and link-only refusal."""

from __future__ import annotations

import json

import pytest

from epub_purifier.sources.base import (
    BookListing,
    DownloadRefused,
    MinInterval,
    SearchQueryError,
    SourceError,
    normalize_query,
)
from epub_purifier.sources.gutenberg import GutenbergSource
from epub_purifier.sources.links import ANNAS_ARCHIVE, OCEAN_OF_PDF
from epub_purifier.sources.net import Fetched, check_url, host_allowed
from epub_purifier.sources.registry import search_books

PAYLOAD = {
    "count": 2,
    "results": [
        {
            "id": 1342,
            "title": "Pride and Prejudice",
            "authors": [{"name": "Austen, Jane"}],
            "languages": ["en"],
            "formats": {
                "application/epub+zip": "https://www.gutenberg.org/ebooks/1342.epub3.images",
                "text/html": "https://www.gutenberg.org/ebooks/1342.html.images",
                "image/jpeg": "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg",
            },
        },
        {
            "id": 11,
            "title": "Alice's Adventures in Wonderland",
            "authors": [{"name": "Carroll, Lewis"}],
            "languages": ["en"],
            "formats": {"text/plain": "https://www.gutenberg.org/files/11/11-0.txt"},
        },
    ],
}

EPUB_BYTES = b"PK\x03\x04fake-epub-body"


def make_fetch(payload=None, blob=EPUB_BYTES, search_type="application/json"):
    payload = payload or PAYLOAD
    calls: list[tuple[str, dict]] = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith(".epub3.images"):
            return Fetched(url=url, content_type="application/epub+zip", data=blob)
        if url.endswith(".jpg"):
            return Fetched(url=url, content_type="image/jpeg", data=b"\xff\xd8\xffeight")
        if url.startswith("https://gutendex.com/books/"):
            book_id = url.rsplit("/", 1)[-1]
            entry = next(item for item in payload["results"] if str(item["id"]) == book_id)
            return Fetched(
                url=url, content_type="application/json", data=json.dumps(entry).encode()
            )
        return Fetched(
            url=url, content_type=search_type, data=json.dumps(payload).encode()
        )

    return fetch, calls


def test_normalize_query():
    assert normalize_query("  dune   messiah ") == "dune messiah"
    with pytest.raises(SearchQueryError):
        normalize_query(" a ")
    with pytest.raises(SearchQueryError):
        normalize_query("x" * 201)
    with pytest.raises(SearchQueryError):
        normalize_query("dune\x00")
    with pytest.raises(SearchQueryError):
        normalize_query(42)  # type: ignore[arg-type]


def test_search_parses_listings():
    fetch, _calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    listings = source.search("austen")

    assert [listing.book_id for listing in listings] == ["1342", "11"]
    first, second = listings
    assert first.title == "Pride and Prejudice"
    assert first.authors == ("Austen, Jane",)
    assert first.formats == ("EPUB", "HTML")
    assert first.downloadable is True
    assert first.details_url == "https://www.gutenberg.org/ebooks/1342"
    assert first.cover_url and first.cover_url.endswith(".jpg")
    assert second.downloadable is False
    assert "No EPUB edition" in second.download_note
    assert second.formats == ("Text",)


def test_search_rejects_short_query_without_fetching():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SearchQueryError):
        source.search(" a ")
    assert calls == []


def test_search_rejects_unexpected_content_type():
    fetch, _calls = make_fetch(search_type="text/html")
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.search("dune")
    assert "content type" in str(excinfo.value)


def test_download_picks_epub_edition():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    result = source.download("1342")

    assert result.data == EPUB_BYTES
    assert result.file_name == "Pride and Prejudice.epub"
    assert result.origin_url == "https://www.gutenberg.org/ebooks/1342.epub3.images"
    assert any(url.endswith(".epub3.images") for url, _ in calls)


def test_download_uses_cached_metadata():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    source.download("1342")
    source.download("1342")

    metadata_calls = [url for url, _ in calls if url == "https://gutendex.com/books/1342"]
    assert len(metadata_calls) == 1


def test_download_without_epub_is_refused():
    fetch, _calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("11")


def test_download_rejects_invalid_identifier():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("../../etc/passwd")
    assert calls == []


def test_download_rejects_html_page():
    def fetch(url, **kwargs):
        if url.endswith(".epub3.images"):
            return Fetched(url=url, content_type="text/html", data=b"<html>nope</html>")
        if url.startswith("https://gutendex.com/books/"):
            return Fetched(
                url=url,
                content_type="application/json",
                data=json.dumps(PAYLOAD["results"][0]).encode(),
            )
        return Fetched(url=url, content_type="application/json", data=b"{}")

    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.download("1342")
    assert "EPUB" in str(excinfo.value)


def test_download_rejects_non_zip_body():
    def fetch(url, **kwargs):
        if url.endswith(".epub3.images"):
            return Fetched(url=url, content_type="application/octet-stream", data=b"not a zip")
        if url.startswith("https://gutendex.com/books/"):
            return Fetched(
                url=url,
                content_type="application/json",
                data=json.dumps(PAYLOAD["results"][0]).encode(),
            )
        return Fetched(url=url, content_type="application/json", data=b"{}")

    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.download("1342")
    assert "ZIP" in str(excinfo.value)


def test_cover_returns_image():
    fetch, _calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    cover = source.cover("1342")
    assert cover is not None
    data, media_type = cover
    assert data.startswith(b"\xff\xd8\xff")
    assert media_type == "image/jpeg"


def test_cover_is_absent_when_no_image_is_listed():
    fetch, _calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    assert source.cover("11") is None


def test_min_interval_waits_between_calls():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    limiter = MinInterval(1.0, clock=clock, sleep=sleep)
    limiter.wait()
    clock.now += 0.25
    limiter.wait()

    assert slept == [pytest.approx(0.75)]


def test_link_only_sources_are_links_not_downloads():
    for source in (ANNAS_ARCHIVE, OCEAN_OF_PDF):
        assert source.supports_download is False
        listings = source.search("dune messiah")
        assert len(listings) == 1
        listing = listings[0]
        assert listing.is_search_link is True
        assert listing.downloadable is False
        assert listing.details_url and "dune+messiah" in listing.details_url
        with pytest.raises(DownloadRefused):
            source.download("anything")


def test_check_url_enforces_scheme_host_and_network():
    assert host_allowed("www.gutenberg.org", ("gutenberg.org",)) is True
    assert host_allowed("gutenberg.org", ("gutenberg.org",)) is True
    assert host_allowed("evil-gutenberg.org", ("gutenberg.org",)) is False

    with pytest.raises(DownloadRefused):
        check_url("http://www.gutenberg.org/ebooks/1342.epub3.images", allowed_hosts=("gutenberg.org",))
    with pytest.raises(DownloadRefused):
        check_url("https://user:pass@gutenberg.org/x.epub", allowed_hosts=("gutenberg.org",))
    with pytest.raises(DownloadRefused):
        check_url("https://example.org/x.epub", allowed_hosts=("gutenberg.org",))
    with pytest.raises(DownloadRefused):
        check_url("https://127.0.0.1/x.epub", allowed_hosts=("127.0.0.1",))
    with pytest.raises(DownloadRefused):
        check_url("https://10.0.0.5/x.epub", allowed_hosts=("10.0.0.5",))


class _Boom:
    name = "boom"
    label = "Broken"
    supports_download = False
    note = ""

    def search(self, query):
        raise SourceError("source is down")

    def download(self, book_id):
        raise DownloadRefused("no")


class _Ok:
    name = "ok"
    label = "Working"
    supports_download = True
    note = ""

    def search(self, query):
        return [
            BookListing(
                source=self.name,
                source_label=self.label,
                book_id="7",
                title=query,
                downloadable=True,
            )
        ]

    def download(self, book_id):
        raise DownloadRefused("not used")


def test_registry_keeps_partial_results():
    response = search_books("dune", [_Boom(), _Ok()])
    assert [listing.book_id for listing in response.results] == ["7"]
    assert response.sources[0].ok is False
    assert "source is down" in (response.sources[0].error or "")
    assert response.sources[1].ok is True
    assert response.sources[1].result_count == 1


def test_registry_raises_when_every_source_fails():
    with pytest.raises(SourceError):
        search_books("dune", [_Boom()])
