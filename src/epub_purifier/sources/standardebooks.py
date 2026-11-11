"""Standard Ebooks: official OPDS search with open-access EPUB downloads.

Standard Ebooks gates its *browse* feeds behind Patrons Circle membership, but the
same OPDS feed exposes public search results at
``https://standardebooks.org/feeds/opds/all?query=...`` and advertises that search
through an OpenSearch description. This adapter uses only that query endpoint,
never the membership-gated catalogue feeds and never the site's HTML.

Entries are parsed with defusedxml. Each edition becomes one listing built from
its Atom metadata, and the EPUB is taken from the entry's own
``application/epub+zip`` open-access acquisition link. Downloads are restricted
to standardebooks.org, checked for content type and ZIP magic, and size-capped
before the body is accepted.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from .base import (
    BookListing,
    DownloadRefused,
    DownloadResult,
    MinInterval,
    SourceError,
    isbn_candidates,
    normalize_query,
)
from .net import fetch_bytes, host_allowed, looks_like_zip

BASE = "https://standardebooks.org"
SEARCH = f"{BASE}/feeds/opds/all"
ALLOWED_HOSTS = ("standardebooks.org",)

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/elements/1.1/"
OPDS_IMAGE = "http://opds-spec.org/image"
OPDS_THUMBNAIL = "http://opds-spec.org/image/thumbnail"

MAX_API_BYTES = 4 * 1024 * 1024
MAX_EPUB_BYTES = 64 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024
REQUEST_INTERVAL = 1.0
PAGE_SIZE = 20

EPUB_TYPE = "application/epub+zip"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class StandardEbooksSource:
    name = "standardebooks"
    label = "Standard Ebooks"
    supports_download = True
    note = (
        "Public-domain editions from Standard Ebooks' official OPDS search feed. "
        "Downloads use each entry's open-access EPUB link and are scanned and "
        "cleaned here."
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
        params = urllib.parse.urlencode(
            [("query", query), ("per-page", str(PAGE_SIZE))]
        )
        payload = self._feed(f"{SEARCH}?{params}")

        listings: list[BookListing] = []
        seen: set[str] = set()
        for entry in payload.findall(f"{{{ATOM}}}entry"):
            title = _text(entry.find(f"{{{ATOM}}}title"))
            acquisition = _acquisition_href(entry)
            if not title or acquisition is None:
                continue
            segments = _segments(acquisition)
            if not segments:
                continue
            book_id = "_".join(segments)
            if book_id in seen:
                continue
            seen.add(book_id)
            listings.append(
                BookListing(
                    source=self.name,
                    source_label=self.label,
                    book_id=book_id,
                    title=title,
                    authors=_names(entry.findall(f"{{{ATOM}}}author")),
                    languages=_texts(entry.findall(f"{{{DC}}}language")),
                    formats=("EPUB",),
                    isbn=_isbns(entry),
                    details_url=_details_url(entry, segments),
                    cover_url=_cover_url(entry),
                    downloadable=True,
                    download_note=(
                        "Open-access EPUB from Standard Ebooks; fetched under a "
                        "standardebooks.org-only allowlist and scanned here."
                    ),
                )
            )
            if len(listings) == PAGE_SIZE:
                break
        return listings

    # -- download --------------------------------------------------------
    def download(self, book_id: str) -> DownloadResult:
        segments = _decode_id(book_id)
        url = _download_url(segments)
        self._limiter.wait()
        fetched = self._fetch(
            url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=60.0,
            max_bytes=MAX_EPUB_BYTES,
            accept=EPUB_TYPE,
        )
        if fetched.content_type != EPUB_TYPE:
            raise DownloadRefused(
                "Standard Ebooks returned a page instead of an EPUB",
                detail=fetched.content_type or "unknown",
            )
        if not looks_like_zip(fetched.data):
            raise DownloadRefused("The downloaded file is not a ZIP archive")
        return DownloadResult(
            data=fetched.data,
            file_name=f"{segments[1]}.epub",
            origin_url=fetched.url,
        )

    # -- cover -----------------------------------------------------------
    def cover(self, book_id: str) -> tuple[bytes, str] | None:
        segments = _decode_id(book_id)
        path = "/".join(segments)
        self._limiter.wait()
        fetched = self._fetch(
            f"{BASE}/ebooks/{path}/downloads/cover.jpg",
            allowed_hosts=ALLOWED_HOSTS,
            timeout=15.0,
            max_bytes=MAX_COVER_BYTES,
            accept="image/jpeg,image/png,image/webp",
        )
        if fetched.content_type not in IMAGE_TYPES:
            return None
        return fetched.data, fetched.content_type

    # -- internals -------------------------------------------------------
    def _feed(self, url: str):
        self._limiter.wait()
        fetched = self._fetch(
            url,
            allowed_hosts=ALLOWED_HOSTS,
            timeout=30.0,
            max_bytes=MAX_API_BYTES,
            accept="application/atom+xml;profile=opds-catalog;kind=acquisition",
        )
        if "atom+xml" not in fetched.content_type:
            raise SourceError(
                "Standard Ebooks search returned an unexpected content type",
                detail=fetched.content_type or "unknown",
            )
        try:
            root = fromstring(fetched.data)
        except (ParseError, DefusedXmlException, ValueError) as exc:
            raise SourceError("Standard Ebooks search returned unreadable XML") from exc
        if root.tag != f"{{{ATOM}}}feed":
            raise SourceError("Standard Ebooks search returned an unexpected payload")
        return root


def _text(element) -> str:  # noqa: ANN001 - Element or None
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())[:300]


def _texts(elements: list) -> tuple[str, ...]:
    values = [_text(element) for element in elements]
    return tuple(value for value in values if value)[:8]


def _names(authors: list) -> tuple[str, ...]:
    names: list[str] = []
    for author in authors:
        name = _text(author.find(f"{{{ATOM}}}name"))
        if name:
            names.append(name[:120])
    return tuple(names)[:8]


def _isbns(entry) -> tuple[str, ...]:  # noqa: ANN001 - Element
    values: list[str] = []
    for tag in ("identifier", "isbn"):
        for node in entry.findall(f"{{{DC}}}{tag}"):
            candidate, _ = isbn_candidates(_text(node))
            if candidate and candidate not in values:
                values.append(candidate)
    return tuple(values)


def _trusted(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    return host_allowed(parsed.hostname or "", ALLOWED_HOSTS)


def _segments(url: str) -> tuple[str, ...]:
    """Extract the ``author/title[/translator]`` path segments of an EPUB link."""

    if not _trusted(url):
        return ()
    path = urllib.parse.urlsplit(url).path
    prefix = "/ebooks/"
    if not path.startswith(prefix) or "/downloads/" not in path:
        return ()
    page = path[len(prefix) :].split("/downloads/", 1)[0].strip("/")
    segments = tuple(page.split("/"))
    if not 2 <= len(segments) <= 4:
        return ()
    if any(not _SEGMENT.match(segment) for segment in segments):
        return ()
    return segments


def _decode_id(book_id: str) -> tuple[str, ...]:
    segments = tuple(str(book_id or "").split("_"))
    if not 2 <= len(segments) <= 4 or any(
        not _SEGMENT.match(segment) for segment in segments
    ):
        raise DownloadRefused(
            "That Standard Ebooks identifier is not valid", detail=str(book_id)[:60]
        )
    return segments


def _download_url(segments: tuple[str, ...]) -> str:
    # The feed's acquisition href carries ?source=feed; without it the same
    # path answers with the ebook's HTML page instead of the EPUB.
    return (
        f"{BASE}/ebooks/{'/'.join(segments)}"
        f"/downloads/{'_'.join(segments)}.epub?source=feed"
    )


def _acquisition_href(entry) -> str | None:  # noqa: ANN001 - Element
    """Pick the entry's own EPUB link, preferring the compatible edition."""

    best: tuple[tuple[bool, int], str] | None = None
    for link in entry.findall(f"{{{ATOM}}}link"):
        media_type = (link.get("type") or "").split(";")[0].strip().lower()
        if media_type != EPUB_TYPE:
            continue
        href = (link.get("href") or "").strip()
        if not _segments(href):
            continue
        path = urllib.parse.urlsplit(href).path
        score = ("_advanced" in path, len(path))
        if best is None or score < best[0]:
            best = (score, href)
    return best[1] if best is not None else None


def _details_url(entry, segments: tuple[str, ...]) -> str:  # noqa: ANN001 - Element
    for link in entry.findall(f"{{{ATOM}}}link"):
        if (link.get("rel") or "") != "alternate":
            continue
        href = (link.get("href") or "").strip()
        if _trusted(href):
            return href
    return f"{BASE}/ebooks/{'/'.join(segments)}"


def _cover_url(entry) -> str | None:  # noqa: ANN001 - Element
    found: dict[str, str] = {}
    for link in entry.findall(f"{{{ATOM}}}link"):
        rel = (link.get("rel") or "").strip()
        if rel not in {OPDS_IMAGE, OPDS_THUMBNAIL}:
            continue
        href = (link.get("href") or "").strip()
        if not _trusted(href):
            continue
        if not (link.get("type") or "").startswith("image/"):
            continue
        found.setdefault(rel, href)
    return found.get(OPDS_IMAGE) or found.get(OPDS_THUMBNAIL)


__all__ = ["StandardEbooksSource", "REQUEST_INTERVAL", "SEARCH"]
