"""Open Library: public-scan holdings, downloaded from Internet Archive.

Open Library's search API marks each work with an ``ebook_access`` value; only
``public`` works are listed, so every result is offered for download rather than
borrowing. The scan itself lives in Internet Archive, so the download and cover
are delegated to :class:`InternetArchiveSource`, which validates the identifier,
re-checks the item metadata and refuses lending copies before any file request.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse

from .base import (
    BookListing,
    DownloadResult,
    MinInterval,
    SourceError,
    normalize_query,
)
from .internetarchive import COVER, InternetArchiveSource
from .net import fetch_bytes

BASE = "https://openlibrary.org"
SEARCH = f"{BASE}/search.json"
ALLOWED_HOSTS = ("openlibrary.org",)

MAX_API_BYTES = 4 * 1024 * 1024
REQUEST_INTERVAL = 1.0
FIELDS = "key,title,author_name,language,ia,ebook_access"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")
#: Audio derivatives carry no EPUB, so another scan is preferred when offered.
_AUDIO_PREFIX = "synapseml_"


class OpenLibrarySource:
    name = "openlibrary"
    label = "Open Library"
    supports_download = True
    note = (
        "Public-scan holdings indexed by Open Library. Only works marked public are "
        "listed; the EPUB is fetched from Internet Archive after its metadata is "
        "re-checked, and lending copies are refused."
    )

    def __init__(
        self,
        *,
        fetch=fetch_bytes,
        interval: float = REQUEST_INTERVAL,
        sleep=time.sleep,
        archive: InternetArchiveSource | None = None,
    ) -> None:
        self._fetch = fetch
        self._limiter = MinInterval(interval, sleep=sleep)
        self._archive = archive or InternetArchiveSource(
            fetch=fetch, interval=interval, sleep=sleep
        )

    # -- search ----------------------------------------------------------
    def search(self, query: str) -> list[BookListing]:
        query = normalize_query(query)
        params = [
            ("q", query),
            ("fields", FIELDS),
            ("limit", "40"),
            ("ebook_access", "public"),
        ]
        payload = self._json(f"{SEARCH}?{urllib.parse.urlencode(params)}")
        docs = payload.get("docs")
        if not isinstance(docs, list):
            raise SourceError("Open Library search returned an unexpected payload")

        listings: list[BookListing] = []
        seen: set[str] = set()
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if str(doc.get("ebook_access") or "").strip().lower() != "public":
                continue
            identifier = _pick_identifier(doc.get("ia"))
            title = _first_text(doc.get("title"))
            if not identifier or not title or identifier in seen:
                continue
            seen.add(identifier)
            listings.append(
                BookListing(
                    source=self.name,
                    source_label=self.label,
                    book_id=identifier,
                    title=title,
                    authors=_names(doc.get("author_name")),
                    languages=_names(doc.get("language")),
                    formats=("EPUB",),
                    details_url=_details_url(doc.get("key")),
                    cover_url=COVER.format(identifier=urllib.parse.quote(identifier)),
                    downloadable=True,
                    download_note=(
                        "Public scan; the EPUB comes from Internet Archive and its "
                        "metadata is re-checked before the fetch."
                    ),
                )
            )
            if len(listings) == 20:
                break
        return listings

    # -- download --------------------------------------------------------
    def download(self, book_id: str) -> DownloadResult:
        return self._archive.download(book_id)

    # -- cover -----------------------------------------------------------
    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        return self._archive.cover(book_id)

    # -- internals -------------------------------------------------------
    def _json(self, url: str) -> dict:
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
                "Open Library search returned an unexpected content type",
                detail=fetched.content_type or "unknown",
            )
        try:
            payload = json.loads(fetched.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError("Open Library search returned unreadable JSON") from exc
        if not isinstance(payload, dict):
            raise SourceError("Open Library search returned an unexpected payload")
        return payload


def _pick_identifier(value: object) -> str:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        return ""
    valid = [item.strip() for item in candidates if _IDENTIFIER.match(item.strip())]
    for identifier in valid:
        if not identifier.lower().startswith(_AUDIO_PREFIX):
            return identifier
    return valid[0] if valid else ""


def _details_url(key: object) -> str | None:
    if not isinstance(key, str) or not key.startswith("/") or key.startswith("//"):
        return None
    return f"{BASE}{key}"


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


__all__ = ["OpenLibrarySource", "REQUEST_INTERVAL", "SEARCH"]
