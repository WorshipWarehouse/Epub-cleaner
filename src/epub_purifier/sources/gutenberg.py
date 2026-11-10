"""Project Gutenberg: search through its official OPDS catalogue, download EPUBs.

Gutenberg publishes an Atom/OPDS feed for search and one feed per book, which is
an official machine-readable interface rather than screen scraping. Books are
public domain in the United States and the site offers direct EPUB downloads,
which is why this is the only source in the registry with a download path.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from .base import (
    BookListing,
    DownloadRefused,
    DownloadResult,
    MinInterval,
    SourceError,
    isbn_candidates,
    normalize_query,
)
from .net import fetch_bytes, looks_like_zip

BASE = "https://www.gutenberg.org"
SEARCH_ENDPOINT = f"{BASE}/ebooks/search.opds/"
BOOK_ENDPOINT = f"{BASE}/ebooks/{{book_id}}.opds"
ALLOWED_HOSTS = ("gutenberg.org", "pglaf.org")

MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_EPUB_BYTES = 64 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024

#: Gutenberg asks clients to stay polite; one request a second per process.
REQUEST_INTERVAL = 1.0

ATOM = "{http://www.w3.org/2005/Atom}"
DCTERMS = "{http://purl.org/dc/terms/}"
OPDS_ACQUISITION = "http://opds-spec.org/acquisition"

FORMAT_LABELS = {
    "application/epub+zip": "EPUB",
    "application/x-mobipocket-ebook": "MOBI",
    "text/html": "HTML",
    "text/plain": "Text",
    "application/pdf": "PDF",
    "audio/mpeg": "Audio",
}

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_BOOK_ID = re.compile(r"^[0-9]{1,9}$")
_BOOK_ENTRY = re.compile(r"/ebooks/([0-9]{1,9})\.opds$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")


class GutenbergSource:
    name = "gutenberg"
    label = "Project Gutenberg"
    supports_download = True
    note = (
        "Public-domain catalogue with an official OPDS feed and direct EPUB "
        "downloads. Files are fetched to this server, scanned and cleaned."
    )

    def __init__(
        self,
        *,
        fetch=fetch_bytes,
        interval: float = REQUEST_INTERVAL,
        sleep=time.sleep,
        cache_ttl: float = 600.0,
    ) -> None:
        self._fetch = fetch
        self._limiter = MinInterval(interval, sleep=sleep)
        self._cache: dict[str, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl = cache_ttl

    # -- search ----------------------------------------------------------
    def search(self, query: str) -> list[BookListing]:
        query = normalize_query(query)
        url = f"{SEARCH_ENDPOINT}?query={urllib.parse.quote_plus(query)}"
        root = self._feed(url, what="search")
        listings: list[BookListing] = []
        for entry in root.findall(f"{ATOM}entry"):
            listing = self._listing(entry)
            if listing is not None:
                listings.append(listing)
        return listings[:32]

    # -- download --------------------------------------------------------
    def download(self, book_id: str) -> DownloadResult:
        book_id = self._clean_id(book_id)
        book = self._book(book_id)
        epub_url = book.get("epub")
        if not epub_url:
            raise DownloadRefused(
                "Project Gutenberg does not list an EPUB edition for this book",
                detail=book_id,
            )

        self._limiter.wait()
        fetched = self._fetch(
            epub_url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=30.0,
            max_bytes=MAX_EPUB_BYTES,
            accept="application/epub+zip,application/zip,application/octet-stream",
        )
        if fetched.content_type in {"text/html", "application/json"}:
            raise SourceError(
                "Project Gutenberg returned a page instead of an EPUB",
                detail=fetched.content_type,
            )
        if not looks_like_zip(fetched.data):
            raise SourceError("The downloaded file is not a ZIP archive")

        return DownloadResult(
            data=fetched.data,
            file_name=_safe_file_name(str(book.get("title") or ""), book_id),
            origin_url=fetched.url,
        )

    # -- cover -----------------------------------------------------------
    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        book_id = self._clean_id(book_id)
        book = self._book(book_id)
        cover_url = book.get("cover") or f"{BASE}/cache/epub/{book_id}/pg{book_id}.cover.medium.jpg"
        self._limiter.wait()
        fetched = self._fetch(
            cover_url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=10.0,
            max_bytes=MAX_COVER_BYTES,
            accept="image/jpeg,image/png,image/webp",
        )
        if fetched.content_type not in IMAGE_TYPES:
            return None
        return fetched.data, fetched.content_type

    # -- internals -------------------------------------------------------
    def _feed(self, url: str, *, what: str):
        self._limiter.wait()
        fetched = self._fetch(
            url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=20.0,
            max_bytes=MAX_FEED_BYTES,
            accept="application/atom+xml",
        )
        if "xml" not in fetched.content_type:
            raise SourceError(
                f"Project Gutenberg {what} returned an unexpected content type",
                detail=fetched.content_type or "unknown",
            )
        try:
            return DefusedET.fromstring(fetched.data)
        except (DefusedXmlException, ParseError, ValueError) as exc:
            raise SourceError(f"Project Gutenberg {what} returned unreadable XML") from exc

    def _book(self, book_id: str) -> dict:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(book_id)
            if cached is not None and now - cached[0] < self._cache_ttl:
                return cached[1]

        try:
            root = self._feed(BOOK_ENDPOINT.format(book_id=book_id), what="book")
        except SourceError as exc:
            if "HTTP 404" in str(exc):
                raise DownloadRefused(
                    "Project Gutenberg has no book with that identifier", detail=book_id
                ) from exc
            raise

        entry = None
        for candidate in root.findall(f"{ATOM}entry"):
            if _entry_book_id(candidate) == book_id:
                entry = candidate
                break
        if entry is None:
            entries = root.findall(f"{ATOM}entry")
            entry = entries[0] if entries else None
        if entry is None:
            raise SourceError("Project Gutenberg returned a book feed with no entries")

        book = {
            "title": _text(entry, f"{ATOM}title") or f"gutenberg-{book_id}",
            "epub": _first_link(entry, "application/epub+zip"),
            "cover": _first_link(entry, "image/jpeg"),
        }
        with self._cache_lock:
            if len(self._cache) >= 128:
                oldest = min(self._cache.items(), key=lambda item: item[1][0])[0]
                self._cache.pop(oldest, None)
            self._cache[book_id] = (time.monotonic(), book)
        return book

    def _listing(self, entry) -> BookListing | None:
        book_id = _entry_book_id(entry)
        if book_id is None:
            return None  # author/subject "subsection" entries in the search feed

        title = _text(entry, f"{ATOM}title") or "Untitled"
        formats: list[str] = []
        epub_url = None
        for link in entry.findall(f"{ATOM}link"):
            media_type = (link.get("type") or "").split(";")[0].strip().lower()
            label = FORMAT_LABELS.get(media_type)
            if label and label not in formats:
                formats.append(label)
            if media_type == "application/epub+zip" and epub_url is None:
                epub_url = _absolute(link.get("href"))

        cover = _first_link(entry, "image/jpeg") or (
            f"{BASE}/cache/epub/{book_id}/pg{book_id}.cover.medium.jpg"
        )

        # The search feed only links to each book's own OPDS entry, so format
        # details usually arrive with the download attempt. Gutenberg's EPUB
        # coverage is near-universal, and download() refuses when a book has no
        # EPUB, so advertise EPUB and confirm it when the file is fetched.
        listed_formats = tuple(formats) or ("EPUB",)
        note = "" if epub_url is not None else "EPUB availability is confirmed when the file is fetched."

        return BookListing(
            source=self.name,
            source_label=self.label,
            book_id=book_id,
            title=title[:300],
            authors=_authors(entry),
            languages=_languages(entry),
            formats=listed_formats,
            isbn=_isbns(entry),
            details_url=f"{BASE}/ebooks/{book_id}",
            cover_url=cover,
            downloadable=True,
            download_note=note,
        )

    @staticmethod
    def _clean_id(book_id: str) -> str:
        candidate = str(book_id or "").strip()
        if not _BOOK_ID.match(candidate):
            raise DownloadRefused("That Project Gutenberg identifier is not valid")
        return candidate


# -- helpers -------------------------------------------------------------
def _text(entry, path: str) -> str:  # noqa: ANN001 - Element
    return (entry.findtext(path) or "").strip()


def _entry_book_id(entry) -> str | None:  # noqa: ANN001 - Element
    match = _BOOK_ENTRY.search(_text(entry, f"{ATOM}id"))
    return match.group(1) if match else None


def _absolute(href: str | None) -> str | None:
    if not href:
        return None
    resolved = urllib.parse.urljoin(BASE + "/", href)
    return resolved if resolved.startswith("https://") else None


def _first_link(entry, media_type: str) -> str | None:  # noqa: ANN001 - Element
    for link in entry.findall(f"{ATOM}link"):
        if (link.get("type") or "").split(";")[0].strip().lower() == media_type:
            absolute = _absolute(link.get("href"))
            if absolute:
                return absolute
    return None


def _authors(entry) -> tuple[str, ...]:  # noqa: ANN001 - Element
    names = [
        _text(author, f"{ATOM}name") for author in entry.findall(f"{ATOM}author")
    ]
    names = [name for name in names if name]
    if names:
        return tuple(names[:8])
    content = entry.find(f"{ATOM}content")
    if content is not None and (content.get("type") or "text") == "text":
        text = (content.text or "").strip()
        if 0 < len(text) <= 120:
            return (text,)
    return ()


def _languages(entry) -> tuple[str, ...]:  # noqa: ANN001 - Element
    codes = [(_text(node, ".") or (node.text or "").strip()) for node in entry.findall(f"{DCTERMS}language")]
    return tuple(code[:12] for code in codes if code)[:8]


def _isbns(entry) -> tuple[str, ...]:  # noqa: ANN001 - Element
    values: list[str] = []
    for tag in ("identifier", "isbn", "source"):
        for node in entry.findall(f"{DCTERMS}{tag}"):
            text = "".join(node.itertext()).strip()
            candidate, _ = isbn_candidates(text)
            if candidate and candidate not in values:
                values.append(candidate)
    return tuple(values)


def _safe_file_name(title: str, book_id: str) -> str:
    slug = _UNSAFE_NAME.sub(" ", title).strip()
    slug = " ".join(slug.split())[:80].strip(" .")
    return f"{slug or f'gutenberg-{book_id}'}.epub"


__all__ = ["GutenbergSource", "REQUEST_INTERVAL", "SEARCH_ENDPOINT"]
