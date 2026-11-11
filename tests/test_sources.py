"""Source adapters: OPDS parsing, rate limits, downloads, and link-only refusal."""

from __future__ import annotations

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
from epub_purifier.sources.links import LinkOnlySource
from epub_purifier.sources.net import Fetched, check_url, host_allowed
from epub_purifier.sources.registry import search_books

SEARCH_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Search results</title>
  <entry>
    <id>https://www.gutenberg.org/ebooks/authors/search.opds/?query=austen</id>
    <title>Authors</title>
    <content type="text">10 author names match your search.</content>
  </entry>
  <entry>
    <id>https://www.gutenberg.org/ebooks/1342.opds</id>
    <title>Pride and Prejudice</title>
    <content type="text">Jane Austen</content>
    <link type="application/epub+zip" rel="http://opds-spec.org/acquisition"
          href="https://www.gutenberg.org/ebooks/1342.epub3.images"/>
    <link type="text/html" rel="alternate" href="https://www.gutenberg.org/ebooks/1342.html.images"/>
    <link type="image/jpeg" rel="http://opds-spec.org/image/thumbnail"
          href="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.small.jpg"/>
  </entry>
  <entry>
    <id>https://www.gutenberg.org/ebooks/11.opds</id>
    <title>Alice's Adventures in Wonderland</title>
    <content type="text">Lewis Carroll</content>
    <link type="text/html" rel="alternate" href="https://www.gutenberg.org/ebooks/11.html.images"/>
  </entry>
</feed>"""

BOOK_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Pride and Prejudice by Jane Austen</title>
  <entry>
    <id>https://www.gutenberg.org/ebooks/1342.opds</id>
    <title>Pride and Prejudice</title>
    <link type="application/epub+zip" rel="http://opds-spec.org/acquisition"
          href="https://www.gutenberg.org/ebooks/1342.epub.noimages"/>
    <link type="image/jpeg" rel="http://opds-spec.org/image/thumbnail"
          href="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.small.jpg"/>
  </entry>
</feed>"""

BOOK_FEED_WITHOUT_EPUB = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://www.gutenberg.org/ebooks/11.opds</id>
    <title>Alice's Adventures in Wonderland</title>
    <link type="text/html" rel="alternate" href="https://www.gutenberg.org/ebooks/11.html.images"/>
  </entry>
</feed>"""

EPUB_BYTES = b"PK\x03\x04fake-epub-body"
JPEG_BYTES = b"\xff\xd8\xffeight"


def make_fetch(
    *,
    search_feed: str = SEARCH_FEED,
    book_feed: str = BOOK_FEED,
    epub: bytes = EPUB_BYTES,
    cover: bytes | None = JPEG_BYTES,
    feed_type: str = "application/atom+xml",
    epub_type: str = "application/epub+zip",
):
    calls: list[tuple[str, dict]] = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if "/ebooks/search.opds/" in url:
            return Fetched(url=url, content_type=feed_type, data=search_feed.encode())
        if url.rstrip("/").endswith(".opds"):
            return Fetched(url=url, content_type="application/atom+xml", data=book_feed.encode())
        if ".epub" in url:
            return Fetched(url=url, content_type=epub_type, data=epub)
        if url.endswith(".jpg"):
            if cover is None:
                raise SourceError("The source answered HTTP 404", detail=url)
            return Fetched(url=url, content_type="image/jpeg", data=cover)
        raise AssertionError(f"unexpected fetch: {url}")

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


def test_search_parses_opds_books_and_skips_subsections():
    fetch, _calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    listings = source.search("austen")

    assert [listing.book_id for listing in listings] == ["1342", "11"]
    first, second = listings
    assert first.title == "Pride and Prejudice"
    assert first.authors == ("Jane Austen",)
    assert first.formats == ("EPUB", "HTML")
    assert first.downloadable is True
    assert first.details_url == "https://www.gutenberg.org/ebooks/1342"
    assert first.cover_url and first.cover_url.endswith(".jpg")
    # Only linked formats are listed; the EPUB itself is confirmed on fetch.
    assert second.formats == ("HTML",)
    assert second.downloadable is True
    assert "confirmed when the file is fetched" in second.download_note


def test_search_rejects_short_query_without_fetching():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SearchQueryError):
        source.search(" a ")
    assert calls == []


def test_search_rejects_unexpected_content_type():
    fetch, _calls = make_fetch(feed_type="text/html")
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.search("austen")
    assert "content type" in str(excinfo.value)


def test_search_rejects_unreadable_xml():
    fetch, _calls = make_fetch(search_feed="<feed><entry>")
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.search("austen")
    assert "unreadable XML" in str(excinfo.value)


def test_download_uses_the_book_feed_epub_link():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    result = source.download("1342")

    assert result.data == EPUB_BYTES
    assert result.file_name == "Pride and Prejudice.epub"
    assert result.origin_url == "https://www.gutenberg.org/ebooks/1342.epub.noimages"
    assert any(url.endswith("1342.epub.noimages") for url, _ in calls)


def test_download_uses_cached_book_metadata():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    source.download("1342")
    source.download("1342")

    book_calls = [url for url, _ in calls if url == "https://www.gutenberg.org/ebooks/1342.opds"]
    assert len(book_calls) == 1


def test_download_without_an_epub_link_is_refused():
    fetch, _calls = make_fetch(book_feed=BOOK_FEED_WITHOUT_EPUB)
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("11")


def test_download_rejects_invalid_identifier():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused):
        source.download("../../etc/passwd")
    assert calls == []


def test_download_maps_missing_book_to_refusal():
    def fetch(url, **kwargs):
        raise SourceError("The source answered HTTP 404", detail=url)

    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(DownloadRefused) as excinfo:
        source.download("99999999")
    assert "no book" in str(excinfo.value)


def test_download_rejects_html_page():
    fetch, _calls = make_fetch(epub_type="text/html")
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.download("1342")
    assert "EPUB" in str(excinfo.value)


def test_download_rejects_non_zip_body():
    fetch, _calls = make_fetch(epub=b"not a zip", epub_type="application/octet-stream")
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError) as excinfo:
        source.download("1342")
    assert "ZIP" in str(excinfo.value)


def test_cover_comes_from_the_book_feed():
    fetch, calls = make_fetch()
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    cover = source.cover("1342")

    assert cover is not None
    data, media_type = cover
    assert data == JPEG_BYTES
    assert media_type == "image/jpeg"
    assert any(url.endswith("pg1342.cover.small.jpg") for url, _ in calls)


def test_cover_falls_back_to_the_standard_cache_path():
    feed = BOOK_FEED.replace(
        """    <link type="image/jpeg" rel="http://opds-spec.org/image/thumbnail"
          href="https://www.gutenberg.org/cache/epub/1342/pg1342.cover.small.jpg"/>\n""",
        "",
    )
    fetch, calls = make_fetch(book_feed=feed)
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)

    assert source.cover("1342") is not None
    assert any(
        url == "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg"
        for url, _ in calls
    )


def test_cover_returns_none_when_image_is_missing():
    fetch, _calls = make_fetch(cover=None)
    source = GutenbergSource(fetch=fetch, sleep=lambda _seconds: None)
    with pytest.raises(SourceError):
        source.cover("1342")


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
    source = LinkOnlySource(
        name="external",
        label="External Index",
        search_template="https://example.org/find?q={query}",
        note="test",
        limitation="Search link only.",
    )
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
