"""OAPEN and DOAB adapters: DSpace REST parsing, downloads and refusals."""

from __future__ import annotations

import json

import pytest

from epub_purifier.sources.base import DownloadRefused, SearchQueryError, SourceError
from epub_purifier.sources.doab import DoabSource
from epub_purifier.sources.net import Fetched
from epub_purifier.sources.oapen import OapenSource
from epub_purifier.sources.registry import default_sources

EPUB_BYTES = b"PK\x03\x04fake-epub-body"
JPEG_BYTES = b"\xff\xd8\xffcover"

ITEM_UUID = "11111111-1111-4111-8111-111111111111"
EPUB_UUID = "22222222-2222-4222-8222-222222222222"
PDF_UUID = "33333333-3333-4333-8333-333333333333"
COVER_UUID = "44444444-4444-4444-8444-444444444444"
NO_EPUB_UUID = "55555555-5555-4555-8555-555555555555"

SEARCH_RESULTS = [
    {
        "uuid": ITEM_UUID,
        "name": "The Deliverance of Open Access Books",
        "handle": "20.500.12657/25287",
        "type": "item",
        "metadata": [
            {"key": "dc.title", "value": "The Deliverance of Open Access Books"},
            {"key": "dc.contributor.author", "value": "Jane Scholar"},
            {"key": "dc.contributor.author", "value": "John Researcher"},
            {"key": "dc.language.iso", "value": "eng"},
            {"key": "dc.identifier.isbn", "value": "978-0-306-40615-7"},
        ],
        "bitstreams": [
            {
                "uuid": COVER_UUID,
                "name": "cover.jpg",
                "mimeType": "image/jpeg",
                "bundleName": "THUMBNAIL",
                "sizeBytes": 100,
            },
            {
                "uuid": EPUB_UUID,
                "name": "book.epub",
                "mimeType": "application/epub+zip",
                "bundleName": "ORIGINAL",
                "sizeBytes": 1234,
            },
            {
                "uuid": PDF_UUID,
                "name": "book.pdf",
                "mimeType": "application/pdf",
                "bundleName": "ORIGINAL",
                "sizeBytes": 1234,
            },
        ],
    },
    {
        "uuid": NO_EPUB_UUID,
        "name": "Open Textbook Without EPUB",
        "handle": "20.500.12854/26303",
        "type": "item",
        "metadata": [{"key": "dc.title", "value": "Open Textbook Without EPUB"}],
        "bitstreams": [
            {
                "uuid": PDF_UUID,
                "name": "book.pdf",
                "mimeType": "application/pdf",
                "bundleName": "ORIGINAL",
            }
        ],
    },
    {"uuid": "not-a-uuid", "name": "Skipped", "type": "item"},
    {
        "uuid": "66666666-6666-4666-8666-666666666666",
        "name": "A collection, not a book",
        "handle": "20.500.12657/1",
        "type": "collection",
    },
]

ITEM = SEARCH_RESULTS[0]
ITEM_WITHOUT_EPUB = SEARCH_RESULTS[1]


def dspace_fetch(
    *,
    search=SEARCH_RESULTS,
    item=ITEM,
    epub: bytes = EPUB_BYTES,
    epub_type: str = "application/epub+zip",
    cover: bytes = JPEG_BYTES,
    cover_type: str = "image/jpeg",
    search_type: str = "application/json",
    search_body: bytes | None = None,
):
    calls: list[tuple[str, dict]] = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if "/rest/search" in url:
            data = (
                search_body
                if search_body is not None
                else json.dumps(search).encode()
            )
            return Fetched(url=url, content_type=search_type, data=data)
        if "/rest/items/" in url:
            return Fetched(
                url=url, content_type="application/json", data=json.dumps(item).encode()
            )
        if "/rest/bitstreams/" in url:
            if COVER_UUID in url:
                return Fetched(url=url, content_type=cover_type, data=cover)
            return Fetched(url=url, content_type=epub_type, data=epub)
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch, calls


# -- search --------------------------------------------------------------
def test_search_lists_open_access_books_with_attached_epubs():
    fetch, calls = dspace_fetch()
    listings = OapenSource(fetch=fetch, sleep=lambda _seconds: None).search(
        "open access books"
    )

    assert [listing.book_id for listing in listings] == [ITEM_UUID, NO_EPUB_UUID]
    first = listings[0]
    assert first.source == "oapen"
    assert first.source_label == "OAPEN Library"
    assert first.title == "The Deliverance of Open Access Books"
    assert first.authors == ("Jane Scholar", "John Researcher")
    assert first.languages == ("eng",)
    assert first.formats == ("EPUB", "PDF")
    assert first.isbn == ("9780306406157",)
    assert first.downloadable is True
    assert first.details_url == "https://library.oapen.org/handle/20.500.12657/25287"
    assert (
        first.cover_url
        == f"https://library.oapen.org/rest/bitstreams/{COVER_UUID}/retrieve"
    )
    assert "OAPEN Library" in first.download_note

    second = listings[1]
    assert second.downloadable is False
    assert second.formats == ("PDF",)
    assert "No EPUB" in second.download_note

    url, kwargs = calls[0]
    assert "query=open+access+books" in url
    assert "expand=metadata%2Cbitstreams" in url
    assert "limit=20" in url
    assert kwargs["allowed_hosts"] == ("oapen.org",)


def test_search_rejects_a_short_query_without_fetching():
    fetch, calls = dspace_fetch()
    with pytest.raises(SearchQueryError):
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).search(" a ")
    assert calls == []


def test_search_rejects_unusable_payloads():
    fetch, _calls = dspace_fetch(search_type="text/html")
    with pytest.raises(SourceError) as excinfo:
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).search("open access")
    assert "content type" in str(excinfo.value)

    fetch, _calls = dspace_fetch(search_body=b"{not json")
    with pytest.raises(SourceError) as excinfo:
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).search("open access")
    assert "unreadable JSON" in str(excinfo.value)

    fetch, _calls = dspace_fetch(search={"items": []})
    with pytest.raises(SourceError) as excinfo:
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).search("open access")
    assert "unexpected payload" in str(excinfo.value)


def test_empty_search_returns_no_listings():
    fetch, _calls = dspace_fetch(search=[])
    assert OapenSource(fetch=fetch, sleep=lambda _seconds: None).search("open access") == []


# -- download ------------------------------------------------------------
def test_download_fetches_the_attached_epub_bitstream():
    fetch, calls = dspace_fetch()
    result = DoabSource(fetch=fetch, sleep=lambda _seconds: None).download(ITEM_UUID)

    assert result.data == EPUB_BYTES
    assert result.file_name == "The Deliverance of Open Access Books.epub"
    assert result.origin_url == (
        f"https://directory.doabooks.org/rest/bitstreams/{EPUB_UUID}/retrieve"
    )
    record_url = next(url for url, _kwargs in calls if "/rest/items/" in url)
    assert f"/rest/items/{ITEM_UUID}?expand=metadata,bitstreams" in record_url
    assert calls[-1][1]["allowed_hosts"] == ("doabooks.org",)


def test_download_refuses_invalid_identifiers_without_fetching():
    fetch, calls = dspace_fetch()
    for bad in ("", "not-a-uuid", ITEM_UUID + "/../other", "{}"):
        with pytest.raises(DownloadRefused):
            OapenSource(fetch=fetch, sleep=lambda _seconds: None).download(bad)
    assert calls == []


def test_download_refuses_a_record_without_an_epub():
    fetch, calls = dspace_fetch(item=ITEM_WITHOUT_EPUB)
    with pytest.raises(DownloadRefused) as excinfo:
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).download(NO_EPUB_UUID)
    assert "no EPUB attached" in str(excinfo.value)
    assert not any("/rest/bitstreams/" in url for url, _kwargs in calls)


def test_download_rejects_pages_and_non_zip_bodies():
    fetch, _calls = dspace_fetch(epub_type="text/html")
    with pytest.raises(SourceError) as excinfo:
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).download(ITEM_UUID)
    assert "EPUB" in str(excinfo.value)

    fetch, _calls = dspace_fetch(epub=b"<html>not a zip</html>")
    with pytest.raises(SourceError) as excinfo:
        OapenSource(fetch=fetch, sleep=lambda _seconds: None).download(ITEM_UUID)
    assert "ZIP" in str(excinfo.value)


# -- cover ---------------------------------------------------------------
def test_cover_uses_the_attached_image():
    fetch, calls = dspace_fetch()
    source = OapenSource(fetch=fetch, sleep=lambda _seconds: None)

    assert source.cover(ITEM_UUID) == (JPEG_BYTES, "image/jpeg")
    assert calls[-1][0].endswith(f"/rest/bitstreams/{COVER_UUID}/retrieve")

    plain, _calls = dspace_fetch(cover_type="text/html")
    assert OapenSource(fetch=plain, sleep=lambda _seconds: None).cover(ITEM_UUID) is None


# -- limits and registry -------------------------------------------------
def test_anti_scraping_block_is_reported_as_a_source_error():
    def blocked(url, **kwargs):
        raise SourceError("The source answered HTTP 403", detail=url)

    with pytest.raises(SourceError) as excinfo:
        OapenSource(fetch=blocked, sleep=lambda _seconds: None).search("open access")
    assert "refused this server's address" in str(excinfo.value)
    assert "anti-scraping" in str(excinfo.value)


def test_registry_places_both_open_access_sources_among_downloads():
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
    by_name = {source.name: source for source in sources}
    assert by_name["oapen"].label == "OAPEN Library"
    assert by_name["doab"].label == "Directory of Open Access Books"
    assert by_name["oapen"].supports_download is True
    assert by_name["doab"].supports_download is True
    assert sum(source.supports_download for source in sources) == 7
