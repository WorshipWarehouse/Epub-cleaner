"""Standard Ebooks adapter: OPDS parsing, refusals, covers and downloads."""

from __future__ import annotations

import pytest

from epub_purifier.sources.base import (
    DownloadRefused,
    SearchQueryError,
    SourceError,
)
from epub_purifier.sources.net import Fetched
from epub_purifier.sources.registry import default_sources
from epub_purifier.sources.standardebooks import StandardEbooksSource

EPUB_BYTES = b"PK\x03\x04fake-epub-body"
JPEG_BYTES = b"\xff\xd8\xffcover"

FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
  <title>Search Results</title>
  <entry>
    <id>https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales</id>
    <title>Indian Fairy Tales</title>
    <author><name>Joseph Jacobs</name></author>
    <dc:language>en-GB</dc:language>
    <dc:issued>2021-03-25T17:02:30Z</dc:issued>
    <link href="https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales/downloads/cover.jpg"
          rel="http://opds-spec.org/image" type="image/jpeg"/>
    <link href="https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales/downloads/cover-thumbnail.jpg"
          rel="http://opds-spec.org/image/thumbnail" type="image/jpeg"/>
    <link href="https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales"
          rel="alternate" type="application/xhtml+xml"/>
    <link href="https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales/downloads/joseph-jacobs_indian-fairy-tales.epub?source=feed"
          rel="http://opds-spec.org/acquisition/open-access" type="application/epub+zip"/>
    <link href="https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales/downloads/joseph-jacobs_indian-fairy-tales_advanced.epub?source=feed"
          rel="http://opds-spec.org/acquisition/open-access" type="application/epub+zip"/>
    <link href="https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales/downloads/joseph-jacobs_indian-fairy-tales.kepub.epub?source=feed"
          rel="http://opds-spec.org/acquisition/open-access" type="application/kepub+zip"/>
  </entry>
  <entry>
    <id>https://standardebooks.org/ebooks/a-n-afanasyev/russian-folktales/leonard-a-magnus</id>
    <title>Russian Folktales</title>
    <author><name>A. N. Afanasyev</name></author>
    <dc:language>en</dc:language>
    <link href="https://standardebooks.org/ebooks/a-n-afanasyev/russian-folktales/leonard-a-magnus"
          rel="alternate" type="application/xhtml+xml"/>
    <link href="https://standardebooks.org/ebooks/a-n-afanasyev/russian-folktales/leonard-a-magnus/downloads/a-n-afanasyev_russian-folktales_leonard-a-magnus.epub?source=feed"
          rel="http://opds-spec.org/acquisition/open-access" type="application/epub+zip"/>
  </entry>
  <entry>
    <id>https://standardebooks.org/ebooks/evil/evil-book</id>
    <title>Hostile Link</title>
    <link href="https://evil.example/ebooks/evil/evil-book/downloads/evil_evil-book.epub"
          rel="http://opds-spec.org/acquisition/open-access" type="application/epub+zip"/>
  </entry>
  <entry>
    <id>https://standardebooks.org/ebooks/no/epub-here</id>
    <title>No EPUB</title>
    <link href="https://standardebooks.org/ebooks/no/epub-here"
          rel="alternate" type="application/xhtml+xml"/>
  </entry>
</feed>
"""

EMPTY_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Search Results</title></feed>
"""


def se_fetch(
    *,
    feed: str = FEED,
    feed_type: str = "application/atom+xml;profile=opds-catalog;kind=acquisition",
    epub: bytes = EPUB_BYTES,
    epub_type: str = "application/epub+zip",
    cover: bytes = JPEG_BYTES,
    cover_type: str = "image/jpeg",
):
    calls: list[tuple[str, dict]] = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if "/downloads/cover" in url:
            return Fetched(url=url, content_type=cover_type, data=cover)
        if "/downloads/" in url:
            return Fetched(url=url, content_type=epub_type, data=epub)
        return Fetched(url=url, content_type=feed_type, data=feed.encode())

    return fetch, calls


def test_search_parses_opds_entries_into_listings():
    fetch, calls = se_fetch()
    listings = StandardEbooksSource(fetch=fetch).search("pride and prejudice")

    assert [listing.book_id for listing in listings] == [
        "joseph-jacobs_indian-fairy-tales",
        "a-n-afanasyev_russian-folktales_leonard-a-magnus",
    ]
    first = listings[0]
    assert first.source == "standardebooks"
    assert first.source_label == "Standard Ebooks"
    assert first.title == "Indian Fairy Tales"
    assert first.authors == ("Joseph Jacobs",)
    assert first.languages == ("en-GB",)
    assert first.formats == ("EPUB",)
    assert first.downloadable is True
    assert first.details_url == "https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales"
    assert (
        first.cover_url
        == "https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales/downloads/cover.jpg"
    )

    assert listings[1].cover_url is None
    assert "query=pride+and+prejudice" in calls[0][0]
    assert calls[0][1]["allowed_hosts"] == ("standardebooks.org",)


def test_search_skips_foreign_or_missing_acquisition_links():
    fetch, _ = se_fetch()
    titles = [listing.title for listing in StandardEbooksSource(fetch=fetch).search("pride")]
    assert "Hostile Link" not in titles
    assert "No EPUB" not in titles


def test_search_rejects_short_queries_without_fetching():
    fetch, calls = se_fetch()
    with pytest.raises(SearchQueryError):
        StandardEbooksSource(fetch=fetch).search(" ")
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"feed_type": "text/html"},
        {"feed": "<feed"},
        {"feed": '<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>'},
    ],
)
def test_search_rejects_unusable_payloads(kwargs):
    fetch, _ = se_fetch(**kwargs)
    with pytest.raises(SourceError):
        StandardEbooksSource(fetch=fetch).search("pride")


def test_search_returns_no_listings_for_an_empty_feed():
    fetch, _ = se_fetch(feed=EMPTY_FEED)
    assert StandardEbooksSource(fetch=fetch).search("pride") == []


def test_download_fetches_the_entry_epub():
    fetch, calls = se_fetch()
    result = StandardEbooksSource(fetch=fetch).download("joseph-jacobs_indian-fairy-tales")

    assert result.data == EPUB_BYTES
    assert result.file_name == "indian-fairy-tales.epub"
    url, kwargs = calls[-1]
    assert url == (
        "https://standardebooks.org/ebooks/joseph-jacobs/indian-fairy-tales"
        "/downloads/joseph-jacobs_indian-fairy-tales.epub"
    )
    assert kwargs["allowed_hosts"] == ("standardebooks.org",)
    assert kwargs["max_bytes"] > 0


def test_download_refuses_invalid_identifiers_without_fetching():
    fetch, calls = se_fetch()
    for bad in ("", "single", "evil.example_x", "Author_Title", "../../etc"):
        with pytest.raises(DownloadRefused):
            StandardEbooksSource(fetch=fetch).download(bad)
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"epub_type": "text/html"},
        {"epub": b"<html>not a zip</html>"},
    ],
)
def test_download_refuses_non_epub_bodies(kwargs):
    fetch, _ = se_fetch(**kwargs)
    with pytest.raises(DownloadRefused):
        StandardEbooksSource(fetch=fetch).download("joseph-jacobs_indian-fairy-tales")


def test_cover_uses_the_available_image_and_rejects_others():
    fetch, calls = se_fetch()
    source = StandardEbooksSource(fetch=fetch)
    assert source.cover("joseph-jacobs_indian-fairy-tales") == (JPEG_BYTES, "image/jpeg")
    assert calls[-1][0].endswith("/downloads/cover.jpg")

    plain, _ = se_fetch(cover_type="text/html")
    assert StandardEbooksSource(fetch=plain).cover("joseph-jacobs_indian-fairy-tales") is None


def test_registry_places_standard_ebooks_among_download_sources():
    sources = default_sources()
    assert [source.name for source in sources] == [
        "gutenberg",
        "wikisource",
        "internetarchive",
        "openlibrary",
        "standardebooks",
        "annas",
        "oceanofpdf",
    ]
    ebooks = next(source for source in sources if source.name == "standardebooks")
    assert ebooks.label == "Standard Ebooks"
    assert ebooks.supports_download is True
    assert sum(source.supports_download for source in sources) == 5
