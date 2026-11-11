"""Shared DSpace 6 REST adapter for open-access book repositories.

The OAPEN Library and the Directory of Open Access Books (DOAB) both run DSpace
6 and publish the documented ``/rest/search`` API. Search requests expand
metadata and bitstreams, so a listing knows whether the record actually carries
an EPUB before a download is offered. Files are fetched from the repository's
own bitstream endpoint, never from an arbitrary link in a response, and only
from the repository's host allowlist.

The API needs no registration but applies temporary anti-scraping blocks to
addresses that make too many requests, so every call is serialized to at most
one per second and a 403 is surfaced as a clear per-source error rather than a
crash.
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

MAX_API_BYTES = 8 * 1024 * 1024
MAX_EPUB_BYTES = 64 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024

#: These repositories ask automated clients to stay polite; one a second is ample.
REQUEST_INTERVAL = 1.0
PAGE_SIZE = 20

EPUB_TYPES = {"application/epub+zip", "application/zip", "application/octet-stream"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HANDLE = re.compile(r"^[0-9]{1,5}(?:\.[0-9]{1,10})+/[0-9]{1,12}$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._-]+")


class DSpaceBookSource:
    """A DSpace 6 repository with an official REST search and bitstream API."""

    name = ""
    label = ""
    base = ""
    allowed_hosts: tuple[str, ...] = ()
    supports_download = True
    note = ""
    download_note = ""

    def __init__(
        self,
        *,
        fetch=fetch_bytes,
        interval: float = REQUEST_INTERVAL,
        sleep=time.sleep,
    ) -> None:
        if not self.name or not self.label or not self.base or not self.allowed_hosts:
            raise ValueError("A DSpace source needs a name, label, base URL and allowlist")
        self._fetch = fetch
        self._limiter = MinInterval(interval, sleep=sleep)

    # -- search ----------------------------------------------------------
    def search(self, query: str) -> list[BookListing]:
        query = normalize_query(query)
        params = urllib.parse.urlencode(
            {
                "query": query,
                "expand": "metadata,bitstreams",
                "limit": str(PAGE_SIZE),
            }
        )
        payload = self._json(f"{self.base}/rest/search?{params}", what="search")
        if not isinstance(payload, list):
            raise SourceError(f"{self.label} search returned an unexpected payload")

        listings: list[BookListing] = []
        seen: set[str] = set()
        for item in payload:
            listing = self._listing(item)
            if listing is None or listing.book_id in seen:
                continue
            seen.add(listing.book_id)
            listings.append(listing)
            if len(listings) == PAGE_SIZE:
                break
        return listings

    # -- download --------------------------------------------------------
    def download(self, book_id: str) -> DownloadResult:
        item = self._item(book_id)
        bitstreams = item.get("bitstreams")
        epub = _pick_epub(bitstreams)
        if epub is None:
            raise DownloadRefused(
                "This open-access record has no EPUB attached", detail=str(book_id)[:40]
            )

        self._limiter.wait()
        fetched = self._fetch(
            self._bitstream_url(str(epub["uuid"])),
            allowed_hosts=self.allowed_hosts,
            timeout=60.0,
            max_bytes=MAX_EPUB_BYTES,
            accept="application/epub+zip,application/zip,application/octet-stream",
        )
        if fetched.content_type not in EPUB_TYPES:
            raise SourceError(
                f"{self.label} returned a page instead of an EPUB",
                detail=fetched.content_type or "unknown",
            )
        if not looks_like_zip(fetched.data):
            raise SourceError("The downloaded file is not a ZIP archive")

        title = _first(_metadata(item), "dc.title") or str(item.get("name") or "")
        return DownloadResult(
            data=fetched.data,
            file_name=_safe_file_name(title, str(book_id)),
            origin_url=fetched.url,
        )

    # -- cover -----------------------------------------------------------
    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        item = self._item(book_id)
        image = _pick_image(item.get("bitstreams"))
        if image is None:
            return None

        self._limiter.wait()
        fetched = self._fetch(
            self._bitstream_url(str(image["uuid"])),
            allowed_hosts=self.allowed_hosts,
            timeout=15.0,
            max_bytes=MAX_COVER_BYTES,
            accept="image/jpeg,image/png,image/webp",
        )
        if fetched.content_type not in IMAGE_TYPES:
            return None
        return fetched.data, fetched.content_type

    # -- internals -------------------------------------------------------
    def _listing(self, item: object) -> BookListing | None:
        if not isinstance(item, dict):
            return None
        if str(item.get("type") or "item").lower() != "item":
            return None

        book_id = str(item.get("uuid") or "").strip()
        if not _UUID.fullmatch(book_id):
            return None

        metadata = _metadata(item)
        title = _first(metadata, "dc.title") or str(item.get("name") or "").strip()
        if not title:
            return None

        bitstreams = item.get("bitstreams")
        epub = _pick_epub(bitstreams)
        image = _pick_image(bitstreams)
        has_pdf = _has_pdf(bitstreams)
        formats = tuple(
            name for name, present in (("EPUB", epub is not None), ("PDF", has_pdf)) if present
        )
        handle = str(item.get("handle") or "").strip()
        details_url = (
            f"{self.base}/handle/{handle}"
            if _HANDLE.match(handle)
            else f"{self.base}/handle/{book_id}"
        )
        note = (
            self.download_note
            if epub is not None
            else "No EPUB is attached to this open-access record; open the record page instead."
        )

        return BookListing(
            source=self.name,
            source_label=self.label,
            book_id=book_id.lower(),
            title=title[:300],
            authors=_first_many(metadata, "dc.contributor.author", "dc.creator"),
            languages=_first_many(metadata, "dc.language.iso", "dc.language"),
            formats=formats,
            isbn=_isbns(metadata),
            details_url=details_url,
            cover_url=self._bitstream_url(str(image["uuid"])) if image is not None else None,
            downloadable=epub is not None,
            download_note=note,
        )

    def _item(self, book_id: str) -> dict:
        item_id = self._clean_id(book_id)
        try:
            payload = self._json(
                f"{self.base}/rest/items/{item_id}?expand=metadata,bitstreams",
                what="record",
            )
        except SourceError as exc:
            if "HTTP 404" in str(exc):
                raise DownloadRefused(
                    "The repository has no record with that identifier", detail=item_id
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise SourceError(f"{self.label} returned an unexpected record payload")
        return payload

    def _json(self, url: str, *, what: str) -> object:
        self._limiter.wait()
        try:
            fetched = self._fetch(
                url,
                allowed_hosts=self.allowed_hosts,
                timeout=30.0,
                max_bytes=MAX_API_BYTES,
                accept="application/json",
            )
        except SourceError as exc:
            if "HTTP 403" in str(exc):
                raise SourceError(
                    f"{self.label} refused this server's address",
                    detail=(
                        "The API applies temporary anti-scraping limits to busy "
                        "addresses; try again later."
                    ),
                ) from exc
            raise
        if "json" not in fetched.content_type:
            raise SourceError(
                f"{self.label} {what} returned an unexpected content type",
                detail=fetched.content_type or "unknown",
            )
        try:
            payload = json.loads(fetched.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"{self.label} {what} returned unreadable JSON") from exc
        return payload

    def _bitstream_url(self, bitstream_id: str) -> str:
        if not _UUID.fullmatch(bitstream_id):
            raise SourceError("The repository returned an invalid bitstream identifier")
        return f"{self.base}/rest/bitstreams/{bitstream_id.lower()}/retrieve"

    @staticmethod
    def _clean_id(book_id: str) -> str:
        candidate = str(book_id or "").strip()
        if not _UUID.fullmatch(candidate):
            raise DownloadRefused(
                "That repository identifier is not valid", detail=candidate[:40]
            )
        return candidate.lower()


def _metadata(item: dict) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    entries = item.get("metadata")
    if not isinstance(entries, list):
        return values
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        value = entry.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        text = " ".join(value.split())[:1000]
        if text:
            values.setdefault(key.strip(), []).append(text)
    return values


def _first(values: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        for value in values.get(key, []):
            if value:
                return value
    return ""


def _first_many(values: dict[str, list[str]], *keys: str) -> tuple[str, ...]:
    names: list[str] = []
    for key in keys:
        for value in values.get(key, []):
            name = value[:120]
            if name and name not in names:
                names.append(name)
    return tuple(names[:8])


def _isbns(values: dict[str, list[str]]) -> tuple[str, ...]:
    found: list[str] = []
    for value in values.get("dc.identifier.isbn", []):
        candidate, _ = isbn_candidates(value)
        if candidate and candidate not in found:
            found.append(candidate)
    return tuple(found[:8])


def _size(entry: dict) -> int | None:
    value = entry.get("sizeBytes")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _entries(bitstreams: object) -> list[dict]:
    if not isinstance(bitstreams, list):
        return []
    return [
        entry
        for entry in bitstreams
        if isinstance(entry, dict) and _UUID.fullmatch(str(entry.get("uuid") or ""))
    ]


def _pick_epub(bitstreams: object) -> dict | None:
    candidates = []
    for entry in _entries(bitstreams):
        name = str(entry.get("name") or "").strip()
        mime = str(entry.get("mimeType") or "").split(";")[0].strip().lower()
        bundle = str(entry.get("bundleName") or "").strip().upper()
        is_epub = mime == "application/epub+zip" or name.lower().endswith(".epub")
        if not is_epub or bundle == "LICENSE":
            continue
        size = _size(entry)
        if size is not None and size > MAX_EPUB_BYTES:
            continue
        candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(
        key=lambda entry: (
            str(entry.get("bundleName") or "").strip().upper() != "ORIGINAL",
            "kepub" in str(entry.get("name") or "").lower(),
            len(str(entry.get("name") or "")),
        )
    )
    return candidates[0]


def _pick_image(bitstreams: object) -> dict | None:
    candidates = []
    for entry in _entries(bitstreams):
        mime = str(entry.get("mimeType") or "").split(";")[0].strip().lower()
        if not mime.startswith("image/") or mime not in IMAGE_TYPES:
            continue
        size = _size(entry)
        if size is not None and size > MAX_COVER_BYTES:
            continue
        candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(
        key=lambda entry: (
            str(entry.get("bundleName") or "").strip().upper() != "THUMBNAIL",
            len(str(entry.get("name") or "")),
        )
    )
    return candidates[0]


def _has_pdf(bitstreams: object) -> bool:
    for entry in _entries(bitstreams):
        name = str(entry.get("name") or "").lower()
        mime = str(entry.get("mimeType") or "").split(";")[0].strip().lower()
        if mime == "application/pdf" or name.endswith(".pdf"):
            return True
    return False


def _safe_file_name(title: str, fallback: str) -> str:
    slug = _UNSAFE_NAME.sub(" ", title).strip()
    slug = " ".join(slug.split())[:80].strip(" .")
    return f"{slug or fallback}.epub"


__all__ = [
    "DSpaceBookSource",
    "PAGE_SIZE",
    "REQUEST_INTERVAL",
]
