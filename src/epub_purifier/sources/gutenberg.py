"""Project Gutenberg: search through Gutendex, download EPUB editions.

Gutenberg books are public domain in the United States and the site publishes
EPUB editions for direct download, which is why this is the only source in the
registry with an automated download path.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse

from .base import (
    BookListing,
    DownloadRefused,
    DownloadResult,
    MinInterval,
    SourceError,
    normalize_query,
)
from .net import fetch_bytes, looks_like_zip

SEARCH_ENDPOINT = "https://gutendex.com/books"
ALLOWED_HOSTS = ("gutenberg.org", "pglaf.org", "gutendex.com")

MAX_SEARCH_BYTES = 4 * 1024 * 1024
MAX_EPUB_BYTES = 64 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024

#: Gutendex asks clients to stay around one request per second per client.
REQUEST_INTERVAL = 1.0

FORMAT_LABELS = {
    "application/epub+zip": "EPUB",
    "application/x-mobipocket-ebook": "MOBI",
    "text/html": "HTML",
    "text/plain": "Text",
    "application/pdf": "PDF",
    "audio/mpeg": "Audio",
}

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")
_BOOK_ID = re.compile(r"^[0-9]{1,9}$")


class GutenbergSource:
    name = "gutenberg"
    label = "Project Gutenberg"
    supports_download = True
    note = (
        "Public-domain catalogue with direct EPUB downloads. "
        "Files are fetched to this server, scanned and cleaned before you get them."
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
        url = f"{SEARCH_ENDPOINT}?search={urllib.parse.quote(query)}"
        payload = self._json(url, max_bytes=MAX_SEARCH_BYTES, what="search")
        entries = payload.get("results")
        if not isinstance(entries, list):
            raise SourceError("Project Gutenberg search response has no results list")
        listings: list[BookListing] = []
        for entry in entries[:32]:
            if isinstance(entry, dict):
                listing = self._listing(entry)
                if listing is not None:
                    listings.append(listing)
        return listings

    # -- download --------------------------------------------------------
    def download(self, book_id: str) -> DownloadResult:
        book_id = self._clean_id(book_id)
        payload = self._metadata(book_id)
        formats = payload.get("formats") if isinstance(payload.get("formats"), dict) else {}
        epub_url = self._epub_url(formats)
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

        title = str(payload.get("title") or f"gutenberg-{book_id}")
        return DownloadResult(
            data=fetched.data,
            file_name=_safe_file_name(title, book_id),
            origin_url=fetched.url,
        )

    # -- cover -----------------------------------------------------------
    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        book_id = self._clean_id(book_id)
        payload = self._metadata(book_id)
        formats = payload.get("formats") if isinstance(payload.get("formats"), dict) else {}
        cover_url = formats.get("image/jpeg")
        if not isinstance(cover_url, str) or not cover_url.startswith("https://"):
            return None
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
    def _json(self, url: str, *, max_bytes: int, what: str) -> dict:
        self._limiter.wait()
        fetched = self._fetch(
            url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=15.0,
            max_bytes=max_bytes,
            accept="application/json",
        )
        if fetched.content_type != "application/json":
            raise SourceError(
                f"Project Gutenberg {what} returned an unexpected content type",
                detail=fetched.content_type or "unknown",
            )
        try:
            payload = json.loads(fetched.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"Project Gutenberg {what} returned unreadable JSON") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"Project Gutenberg {what} returned an unexpected document")
        return payload

    def _metadata(self, book_id: str) -> dict:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(book_id)
            if cached is not None and now - cached[0] < self._cache_ttl:
                return cached[1]
        payload = self._json(f"{SEARCH_ENDPOINT}/{book_id}", max_bytes=MAX_SEARCH_BYTES, what="book")
        with self._cache_lock:
            if len(self._cache) >= 128:
                oldest = min(self._cache.items(), key=lambda item: item[1][0])[0]
                self._cache.pop(oldest, None)
            self._cache[book_id] = (time.monotonic(), payload)
        return payload

    @staticmethod
    def _epub_url(formats: dict) -> str | None:
        for media_type, url in formats.items():
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            if str(media_type).split(";")[0].strip().lower() == "application/epub+zip":
                return url
        return None

    @staticmethod
    def _clean_id(book_id: str) -> str:
        candidate = str(book_id or "").strip()
        if not _BOOK_ID.match(candidate):
            raise DownloadRefused("That Project Gutenberg identifier is not valid")
        return candidate

    def _listing(self, entry: dict) -> BookListing | None:
        try:
            book_id = str(int(entry.get("id")))
        except (TypeError, ValueError):
            return None
        title = str(entry.get("title") or "Untitled").strip()[:300] or "Untitled"
        authors = tuple(
            str(author.get("name")).strip()[:120]
            for author in entry.get("authors") or []
            if isinstance(author, dict) and author.get("name")
        )[:8]
        languages = tuple(str(code)[:12] for code in entry.get("languages") or [])[:8]

        formats = entry.get("formats") if isinstance(entry.get("formats"), dict) else {}
        labels: list[str] = []
        for media_type in formats:
            label = FORMAT_LABELS.get(str(media_type).split(";")[0].strip().lower())
            if label and label not in labels:
                labels.append(label)
        epub_url = self._epub_url(formats)
        cover = formats.get("image/jpeg")
        cover_url = cover if isinstance(cover, str) and cover.startswith("https://") else None

        return BookListing(
            source=self.name,
            source_label=self.label,
            book_id=book_id,
            title=title,
            authors=authors,
            languages=languages,
            formats=tuple(labels),
            details_url=f"https://www.gutenberg.org/ebooks/{book_id}",
            cover_url=cover_url,
            downloadable=bool(epub_url),
            download_note="" if epub_url else "No EPUB edition is listed for this book.",
        )


def _safe_file_name(title: str, book_id: str) -> str:
    slug = _UNSAFE_NAME.sub(" ", title).strip()
    slug = " ".join(slug.split())[:80].strip(" .")
    return f"{slug or f'gutenberg-{book_id}'}.epub"


__all__ = ["GutenbergSource", "REQUEST_INTERVAL", "SEARCH_ENDPOINT"]
