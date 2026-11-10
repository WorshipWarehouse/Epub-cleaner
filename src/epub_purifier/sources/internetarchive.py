"""Internet Archive: openly downloadable texts, including Gutenberg mirrors.

The search endpoint is archive.org's public advanced-search API, restricted to
items that already expose an EPUB and are not lent rather than downloaded. A
download re-checks the item metadata and refuses anything marked
``access-restricted-item`` before a single byte is requested, so borrowing
copies are never smuggled through the cleaner.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

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

BASE = "https://archive.org"
SEARCH = f"{BASE}/advancedsearch.php"
METADATA = f"{BASE}/metadata/{{identifier}}"
DOWNLOAD = f"{BASE}/download/{{identifier}}/{{name}}"
COVER = f"{BASE}/services/img/{{identifier}}"
ALLOWED_HOSTS = ("archive.org",)

MAX_API_BYTES = 4 * 1024 * 1024
MAX_EPUB_BYTES = 64 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024

REQUEST_INTERVAL = 1.0
EPUB_TYPES = {"application/epub+zip", "application/zip", "application/octet-stream"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")

#: Only items that are downloadable rather than lent, and that carry an EPUB.
QUERY_SUFFIX = " AND mediatype:texts AND format:EPUB AND NOT access-restricted-item:true"


class InternetArchiveSource:
    name = "internetarchive"
    label = "Internet Archive"
    supports_download = True
    note = (
        "Public-domain and openly licensed texts, including Project Gutenberg "
        "mirrors. Only items offered for download are listed; lending copies are "
        "refused, and files are scanned and cleaned here."
    )

    def __init__(
        self,
        *,
        fetch=fetch_bytes,
        interval: float = REQUEST_INTERVAL,
        sleep=time.sleep,
    ) -> None:
        self._fetch = fetch
        self._limiter = MinInterval(interval, sleep=sleep)

    # -- search ----------------------------------------------------------
    def search(self, query: str) -> list[BookListing]:
        query = normalize_query(query)
        expression = f"({query}){QUERY_SUFFIX}"
        params = [
            ("q", expression),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "creator"),
            ("fl[]", "language"),
            ("fl[]", "isbn"),
            ("rows", "20"),
            ("page", "1"),
            ("output", "json"),
        ]
        payload = self._json(f"{SEARCH}?{urllib.parse.urlencode(params)}", what="search")
        docs = (payload.get("response") or {}).get("docs") or []
        if not isinstance(docs, list):
            raise SourceError("Internet Archive search returned an unexpected payload")

        listings: list[BookListing] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            identifier = str(doc.get("identifier") or "").strip()
            title = _first_text(doc.get("title"))
            if not _IDENTIFIER.match(identifier) or not title:
                continue
            listings.append(
                BookListing(
                    source=self.name,
                    source_label=self.label,
                    book_id=identifier,
                    title=title[:300],
                    authors=_names(doc.get("creator")),
                    languages=_names(doc.get("language")),
                    formats=("EPUB",),
                    isbn=_isbns(doc.get("isbn")),
                    details_url=f"{BASE}/details/{urllib.parse.quote(identifier)}",
                    cover_url=COVER.format(identifier=urllib.parse.quote(identifier)),
                    downloadable=True,
                    download_note="Openly downloadable items only; lending copies are refused.",
                )
            )
        return listings[:20]

    # -- download --------------------------------------------------------
    def download(self, book_id: str) -> DownloadResult:
        identifier = self._clean_id(book_id)
        payload = self._json(METADATA.format(identifier=urllib.parse.quote(identifier)), what="metadata")
        metadata = payload.get("metadata") or {}
        if str(metadata.get("access-restricted-item", "")).strip().lower() == "true":
            raise DownloadRefused(
                "Internet Archive lends this item rather than allowing a download",
                detail=identifier,
            )

        name = _pick_epub_file(payload.get("files"))
        if name is None:
            raise DownloadRefused(
                "Internet Archive has no EPUB file for this item", detail=identifier
            )

        self._limiter.wait()
        fetched = self._fetch(
            DOWNLOAD.format(identifier=urllib.parse.quote(identifier), name=urllib.parse.quote(name)),
            allowed_hosts=ALLOWED_HOSTS,
            timeout=60.0,
            max_bytes=MAX_EPUB_BYTES,
            accept="application/epub+zip,application/zip,application/octet-stream",
        )
        if fetched.content_type not in EPUB_TYPES:
            raise SourceError(
                "Internet Archive returned a page instead of an EPUB",
                detail=fetched.content_type or "unknown",
            )
        if not looks_like_zip(fetched.data):
            raise SourceError("The downloaded file is not a ZIP archive")

        title = _first_text(metadata.get("title")) or identifier
        return DownloadResult(
            data=fetched.data,
            file_name=_safe_file_name(title, identifier),
            origin_url=fetched.url,
        )

    # -- cover -----------------------------------------------------------
    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        identifier = self._clean_id(book_id)
        self._limiter.wait()
        fetched = self._fetch(
            COVER.format(identifier=urllib.parse.quote(identifier)),
            allowed_hosts=ALLOWED_HOSTS,
            timeout=15.0,
            max_bytes=MAX_COVER_BYTES,
            accept="image/jpeg,image/png,image/webp",
        )
        if fetched.content_type not in IMAGE_TYPES:
            return None
        return fetched.data, fetched.content_type

    # -- internals -------------------------------------------------------
    def _json(self, url: str, *, what: str) -> dict:
        self._limiter.wait()
        fetched = self._fetch(
            url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=30.0,
            max_bytes=MAX_API_BYTES,
            accept="application/json",
        )
        if "json" not in fetched.content_type:
            raise SourceError(
                f"Internet Archive {what} returned an unexpected content type",
                detail=fetched.content_type or "unknown",
            )
        try:
            payload = json.loads(fetched.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"Internet Archive {what} returned unreadable JSON") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"Internet Archive {what} returned an unexpected payload")
        return payload

    @staticmethod
    def _clean_id(book_id: str) -> str:
        candidate = str(book_id or "").strip()
        if not _IDENTIFIER.match(candidate):
            raise DownloadRefused(
                "That Internet Archive identifier is not valid", detail=candidate[:40]
            )
        return candidate


def _first_text(value: object) -> str:
    if isinstance(value, str):
        return " ".join(value.split())[:300]
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return ""


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return ()
    names = [" ".join(item.split())[:120] for item in candidates]
    return tuple(name for name in names if name)[:8]


def _isbns(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return ()
    values: list[str] = []
    for candidate in candidates:
        normalized, _ = isbn_candidates(candidate)
        if normalized and normalized not in values:
            values.append(normalized)
    return tuple(values)[:8]


def _pick_epub_file(files: object) -> str | None:
    if not isinstance(files, list):
        return None
    candidates: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name.lower().endswith(".epub"):
            continue
        if str(entry.get("private", "")).strip().lower() == "true":
            continue
        candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(
        key=lambda entry: (
            str(entry.get("format", "")).strip().upper() != "EPUB",
            len(str(entry.get("name", ""))),
        )
    )
    return str(candidates[0]["name"])


def _safe_file_name(title: str, identifier: str) -> str:
    slug = _UNSAFE_NAME.sub(" ", title).strip()
    slug = " ".join(slug.split())[:80].strip(" .")
    return f"{slug or identifier}.epub"


__all__ = ["InternetArchiveSource", "REQUEST_INTERVAL", "SEARCH"]
