"""Wikisource and Internet Archive adapters: parsing, refusals and downloads."""

from __future__ import annotations

import json

import pytest

from epub_purifier.sources.base import DownloadRefused, SearchQueryError, SourceError
from epub_purifier.sources.internetarchive import InternetArchiveSource
from epub_purifier.sources.net import Fetched
from epub_purifier.sources.registry import default_sources
from epub_purifier.sources.wikisource import WikisourceSource

EPUB_BYTES = b"PK\x03\x04fake-epub-body"
JPEG_BYTES = b"\xff\xd8\xffcover"

WS_SEARCH = {
    "query": {
        "search": [
            {"pageid": 942, "title": "Pride and Prejudice (1817)"},
            {"pageid": 4185790, "title": "Pride and Prejudice"},
            {"title": "no page id"},
        ]
    }
}
WS_PAGE = {
    "query": {
        "pages": [
            {
                "pageid": 942,
                "title": "Pride and Prejudice (1817)",
                "fullurl": "https://en.wikisource.org/wiki/Pride_and_Prejudice_(1817)",
            }
        ]
    }
}
WS_MISSING = {"query": {"pages": [{"pageid": 5, "missing": True}]}}


def ws_fetch(
    *,
    search: dict = WS_SEARCH,
    page: dict = WS_PAGE,
    export: bytes = EPUB_BYTES,
    export_type: str = "application/epub+zip",
    search_type: str = "application/json",
):
    calls: list[str] = []

    def fetch(url, **kwargs):
        calls.append(url)
        if "list=search" in url:
            return Fetched(url=url, content_type=search_type, data=json.dumps(search).encode())
        if "pageids=" in url:
            return Fetched(url=url, content_type="application/json", data=json.dumps(page).encode())
        if url.startswith("https://ws-export.wmcloud.org/"):
            return Fetched(url=url, content_type=export_type, data=export)
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch, calls


IA_SEARCH = {
    "response": {
        "docs": [
            {
                "identifier": "prideandprejudic01342gut",
                "title": ["Pride and Prejudice"],
                "creator": ["Austen, Jane"],
                "language": ["eng"],
            },
            {"identifier": "not a valid id", "title": "Skipped"},
            {"identifier": "missingtitle"},
        ]
    }
}
IA_META = {
    "metadata": {
        "identifier": "prideandprejudic01342gut",
        "title": "Pride and Prejudice",
        "access-restricted-item": "false",
    },
    "files": [
        {"name": "thumb.jpg", "format": "JPEG"},
        {"name": "pandp12p2.epub", "format": "EPUB"},
    ],
}
IA_RESTRICTED = {
    "metadata": {"title": "Lent Book", "access-restricted-item": "true"},
    "files": [{"name": "book.epub", "format": "EPUB"}],
}
IA_NO_EPUB = {
    "metadata": {"title": "Scanned Only", "access-restricted-item": "false"},
    "files": [{"name": "book.pdf", "format": "Text PDF"}],
}


def ia_fetch(
    *,
    search: dict = IA_SEARCH,
    meta: dict = IA_META,
    epub: bytes = EPUB_BYTES,
    epub_type: str = "application/epub+zip",
    meta_type: str = "application/json",
    cover: bytes = JPEG_BYTES,
    cover_type: str = "image/jpeg",
):
    calls: list[str] = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url.startswith("https://archive.org/advancedsearch.php"):
            return Fetched(url=url, content_type="application/json", data=json.dumps(search).encode())
        if "/metadata/" in url:
            return Fetched(url=url, content_type=meta_type, data=json.dumps(meta).encode())
        if "/download/" in url:
            return Fetched(url=url, content_type=epub_type, data=epub)
        if "/services/img/" in url:
            return Fetched(url=url, content_type=cover_type, data=cover)
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch, calls


# -- Wikisource ----------------------------------------------------------
def test_wikisource_search_lists_pages_with_downloads():
    fetch, calls = ws_fetch()
    source = WikisourceSource(fetch=fetch, sleep=lambda _seconds: None)

    listings = source.search("pride and prejudice")

    assert [listing.book_id for listing in listings] == ["942", "4185790"]
    first = listings[0]
    assert first.title == "Pride and Prejudice (1817)"
    assert first.formats == ("EPUB",)
    assert first.downloadable is True
    assert first.details_url == "https://en.wikisource.org/?curid=942"
    assert "export service" in first.download_note
    assert calls[0].startswith("https://en.wikisource.org/w/api.php?")
    assert "srsearch=pride+and+prejudice" in calls[0]


def test_wikisource_rejects_a_short_query_without_fetching():
    fetch, calls = ws_fetch()
    source = WikisourceSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SearchQueryError):
        source.search(" a ")
    assert calls == []


def test_wikisource_rejects_unexpected_content_and_json():
    fetch, _calls = ws_fetch(search_type="text/html")
    with pytest.raises(SourceError) as excinfo:
        WikisourceSource(fetch=fetch, sleep=lambda _seconds: None).search("dune")
    assert "content type" in str(excinfo.value)

    def broken(url, **kwargs):
        return Fetched(url=url, content_type="application/json", data=b"{not json")

    with pytest.raises(SourceError) as excinfo:
        WikisourceSource(fetch=broken, sleep=lambda _seconds: None).search("dune")
    assert "unreadable JSON" in str(excinfo.value)


def test_wikisource_download_exports_the_page_as_epub():
    fetch, calls = ws_fetch()
    source = WikisourceSource(fetch=fetch, sleep=lambda _seconds: None)

    result = source.download("942")

    assert result.data == EPUB_BYTES
    assert result.file_name == "Pride and Prejudice 1817.epub"
    export_url = next(url for url in calls if url.startswith("https://ws-export.wmcloud.org/"))
    assert "format=epub" in export_url
    assert "lang=en" in export_url
    assert "page=Pride+and+Prejudice+%281817%29" in export_url
    assert result.origin_url == export_url


def test_wikisource_download_refuses_a_missing_page():
    fetch, calls = ws_fetch(page=WS_MISSING)
    source = WikisourceSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("5")
    assert not any("ws-export" in url for url in calls)


def test_wikisource_download_rejects_bad_identifiers_and_bodies():
    fetch, calls = ws_fetch()
    source = WikisourceSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("../etc/passwd")
    assert calls == []

    html_fetch, _calls = ws_fetch(export_type="text/html")
    with pytest.raises(SourceError) as excinfo:
        WikisourceSource(fetch=html_fetch, sleep=lambda _seconds: None).download("942")
    assert "EPUB" in str(excinfo.value)

    zip_fetch, _calls = ws_fetch(export=b"not a zip", export_type="application/octet-stream")
    with pytest.raises(SourceError) as excinfo:
        WikisourceSource(fetch=zip_fetch, sleep=lambda _seconds: None).download("942")
    assert "ZIP" in str(excinfo.value)


# -- Internet Archive ----------------------------------------------------
def test_archive_search_requests_only_openly_downloadable_epubs():
    fetch, calls = ia_fetch()
    source = InternetArchiveSource(fetch=fetch, sleep=lambda _seconds: None)

    listings = source.search("pride and prejudice")

    assert [listing.book_id for listing in listings] == ["prideandprejudic01342gut"]
    listing = listings[0]
    assert listing.title == "Pride and Prejudice"
    assert listing.authors == ("Austen, Jane",)
    assert listing.languages == ("eng",)
    assert listing.formats == ("EPUB",)
    assert listing.downloadable is True
    assert listing.details_url == "https://archive.org/details/prideandprejudic01342gut"
    assert listing.cover_url and "/services/img/" in listing.cover_url
    query = calls[0]
    assert "format%3AEPUB" in query
    assert "NOT+access-restricted-item%3Atrue" in query


def test_archive_download_picks_the_epub_file():
    fetch, calls = ia_fetch()
    source = InternetArchiveSource(fetch=fetch, sleep=lambda _seconds: None)

    result = source.download("prideandprejudic01342gut")

    assert result.data == EPUB_BYTES
    assert result.file_name == "Pride and Prejudice.epub"
    assert result.origin_url and result.origin_url.endswith("pandp12p2.epub")
    assert any("/metadata/" in url for url in calls)


def test_archive_refuses_lending_items_before_downloading():
    fetch, calls = ia_fetch(meta=IA_RESTRICTED)
    source = InternetArchiveSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused) as excinfo:
        source.download("lentbook")
    assert "lends this item" in str(excinfo.value)
    assert not any("/download/" in url for url in calls)


def test_archive_refuses_items_without_an_epub():
    fetch, calls = ia_fetch(meta=IA_NO_EPUB)
    source = InternetArchiveSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused) as excinfo:
        source.download("scannedonly")
    assert "no EPUB file" in str(excinfo.value)
    assert not any("/download/" in url for url in calls)


def test_archive_rejects_bad_identifiers_and_pages():
    fetch, calls = ia_fetch()
    source = InternetArchiveSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("bad id/../x")
    assert calls == []

    html_fetch, _calls = ia_fetch(epub_type="text/html")
    with pytest.raises(SourceError) as excinfo:
        InternetArchiveSource(fetch=html_fetch, sleep=lambda _seconds: None).download(
            "prideandprejudic01342gut"
        )
    assert "EPUB" in str(excinfo.value)

    json_fetch, _calls = ia_fetch(meta_type="text/html")
    with pytest.raises(SourceError) as excinfo:
        InternetArchiveSource(fetch=json_fetch, sleep=lambda _seconds: None).download(
            "prideandprejudic01342gut"
        )
    assert "content type" in str(excinfo.value)


def test_archive_cover_is_proxied_from_item_services():
    fetch, _calls = ia_fetch()
    source = InternetArchiveSource(fetch=fetch, sleep=lambda _seconds: None)
    cover = source.cover("prideandprejudic01342gut")
    assert cover == (JPEG_BYTES, "image/jpeg")

    html_fetch, _calls = ia_fetch(cover_type="text/html")
    assert InternetArchiveSource(fetch=html_fetch, sleep=lambda _seconds: None).cover(
        "prideandprejudic01342gut"
    ) is None


def test_default_registry_includes_both_new_sources():
    sources = {source.name: source for source in default_sources()}
    assert sources["wikisource"].supports_download is True
    assert sources["internetarchive"].supports_download is True
    assert list(sources)[:3] == ["gutenberg", "wikisource", "internetarchive"]
