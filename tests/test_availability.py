"""Legal-availability resolution and the external-listings contract.

Anna's Archive and Ocean of PDF take part in every search but never hand this
server a file. A catalogue record without an EPUB has the same shape. These
tests pin both halves of the promise: the two sites stay link-only, and a book
that has no download of its own is looked up by exact title and author in the
openly licensed sources, where a found copy flows through the normal transfer
and cleaning pipeline.
"""

from __future__ import annotations

import pytest

from epub_purifier.sources.availability import MAX_LOOKUPS
from epub_purifier.sources.base import BookListing, DownloadResult, SourceError
from epub_purifier.sources.links import LinkOnlySource
from epub_purifier.sources.registry import search_books

EPUB_BYTES = b"PK\x03\x04fake-epub-body"

#: An operator-added external catalogue: searchable, never fetched.
EXTERNAL = LinkOnlySource(
    name="external",
    label="External Index",
    search_template="https://example.org/find?q={query}",
    note="operator-configured external catalogue",
    limitation="Search link only.",
)
LINK_SOURCES = (EXTERNAL,)


def book(
    source: str,
    label: str,
    title: str,
    authors: tuple[str, ...] = (),
    *,
    book_id: str = "1",
    downloadable: bool = True,
    formats: tuple[str, ...] = ("EPUB",),
) -> BookListing:
    return BookListing(
        source=source,
        source_label=label,
        book_id=book_id,
        title=title,
        authors=authors,
        languages=("en",),
        formats=formats,
        details_url=f"https://example.org/{source}/{book_id}",
        downloadable=downloadable,
    )


class Catalogue:
    """A downloadable source that answers each query from a fixed table."""

    supports_download = True
    note = "test catalogue"

    def __init__(self, name, label, answers=None, *, error=None, data=EPUB_BYTES):
        self.name = name
        self.label = label
        self.answers = answers or {}
        self.error = error
        self.data = data
        self.queries: list[str] = []
        self.downloads: list[str] = []

    def search(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.answers.get(query, []))

    def download(self, book_id):
        self.downloads.append(book_id)
        return DownloadResult(
            data=self.data,
            file_name="Analog Church.epub",
            origin_url=f"https://example.org/{self.name}/{book_id}.epub",
        )


class RecordOnly:
    """Lists books it cannot hand over, like a DOAB record without an EPUB."""

    name = "records"
    label = "Archive Records"
    supports_download = True
    note = "record-only test source"

    def __init__(self, listings):
        self.listings = listings
        self.queries: list[str] = []

    def search(self, query):
        self.queries.append(query)
        return list(self.listings)

    def download(self, book_id):
        raise SourceError("no EPUB edition is attached to that record")


def record(title="Analog Church", authors=("Jay Y. Kim",)):
    return RecordOnly([book("records", "Archive Records", title, authors, downloadable=False)])


# -- unified search ------------------------------------------------------
def test_both_link_sources_take_part_in_one_search():
    catalogue = Catalogue(
        "gutenberg",
        "Project Gutenberg",
        {
            "pride and prejudice": [
                book(
                    "gutenberg",
                    "Project Gutenberg",
                    "Pride and Prejudice",
                    ("Jane Austen",),
                    book_id="1342",
                )
            ]
        },
    )

    response = search_books("pride and prejudice", [catalogue, *LINK_SOURCES])

    assert len(response.results) == 1
    result = response.results[0]
    assert result.downloadable is True
    assert result.formats == ("EPUB",)

    external = {
        listing.source: listing for listing in result.listings if listing.is_search_link
    }
    assert set(external) == {"external"}
    for listing in external.values():
        assert listing.downloadable is False
        assert listing.title == "Pride and Prejudice"
        assert listing.authors == ("Jane Austen",)
        assert listing.details_url.startswith("https://")
        assert "pride+and+prejudice" in listing.details_url.lower()

    statuses = {status.name: status for status in response.sources}
    assert statuses["external"].ok is True
    assert statuses["external"].supports_download is False


def test_exact_title_and_author_win_over_loose_hits():
    catalogue = Catalogue(
        "gutenberg",
        "Project Gutenberg",
        {
            "pride and prejudice jane austen": [
                book(
                    "gutenberg",
                    "Project Gutenberg",
                    "Pride and Prejudice and Zombies",
                    ("Seth Grahame-Smith",),
                    book_id="zombies",
                ),
                book(
                    "gutenberg",
                    "Project Gutenberg",
                    "The Girl on the Train",
                    ("Paula Hawkins",),
                    book_id="girl",
                ),
                book(
                    "gutenberg",
                    "Project Gutenberg",
                    "Pride and Prejudice",
                    ("Jane Austen",),
                    book_id="pp",
                ),
            ]
        },
    )

    response = search_books("pride and prejudice jane austen", [catalogue, *LINK_SOURCES])

    assert [result.title for result in response.results] == ["Pride and Prejudice"]
    assert response.results[0].primary.book_id == "pp"
    assert response.results[0].match == "title_author"


# -- downloadable vs external --------------------------------------------
def test_a_record_without_a_download_stays_external():
    response = search_books(
        "analog church",
        [record(), Catalogue("standardebooks", "Standard Ebooks"), *LINK_SOURCES],
    )

    result = response.results[0]
    assert result.downloadable is False
    assert result.primary.source == "records"
    assert all(listing.downloadable is False for listing in result.listings)
    assert {listing.source for listing in result.listings} == {
        "records",
        "external",
    }


# -- legal availability --------------------------------------------------
def test_missing_download_is_resolved_from_an_authorised_source():
    holder = Catalogue(
        "standardebooks",
        "Standard Ebooks",
        {
            "Analog Church Jay Y. Kim": [
                book(
                    "standardebooks",
                    "Standard Ebooks",
                    "Analog Church",
                    ("Jay Y. Kim",),
                    book_id="analog-church",
                )
            ]
        },
    )

    response = search_books("analog church", [record(), holder, *LINK_SOURCES])

    result = response.results[0]
    assert result.downloadable is True
    assert result.primary.source == "standardebooks"
    assert result.primary.book_id == "analog-church"
    assert result.match  # the card keeps the ranking verdict it already had
    # The original external listing is kept alongside the new download.
    assert any(listing.source == "records" for listing in result.listings)
    # The plain query first, then the precise title-and-author lookup.
    assert holder.queries == ["analog church", "Analog Church Jay Y. Kim"]


def test_a_source_already_on_the_card_is_not_asked_again():
    records = record()
    holder = Catalogue("standardebooks", "Standard Ebooks")

    search_books("analog church", [records, holder])

    assert records.queries == ["analog church"]
    assert holder.queries == ["analog church", "Analog Church Jay Y. Kim"]


def test_a_failed_lookup_degrades_to_available_elsewhere():
    broken = Catalogue(
        "standardebooks",
        "Standard Ebooks",
        error=SourceError("The source answered HTTP 429"),
    )

    response = search_books("analog church", [record(), broken, *LINK_SOURCES])

    result = response.results[0]
    assert result.downloadable is False
    assert result.primary.source == "records"
    statuses = {status.name: status for status in response.sources}
    assert statuses["standardebooks"].ok is False
    assert "429" in (statuses["standardebooks"].error or "")
    assert statuses["records"].ok is True


def test_lookups_are_bounded_per_search():
    records = RecordOnly(
        [
            book(
                "records",
                "Archive Records",
                f"Analog Church Study {number}",
                (f"Author {number}",),
                book_id=str(number),
                downloadable=False,
            )
            for number in range(1, 4)
        ]
    )
    holder = Catalogue("standardebooks", "Standard Ebooks")

    response = search_books("analog church study", [records, holder])

    assert len(response.results) == 3
    # One fan-out query plus one lookup per card, capped by MAX_LOOKUPS.
    assert len(holder.queries) == 1 + MAX_LOOKUPS


def test_a_wrong_book_is_never_attached_to_the_card():
    holder = Catalogue(
        "standardebooks",
        "Standard Ebooks",
        {
            "Analog Church Jay Y. Kim": [
                book(
                    "standardebooks",
                    "Standard Ebooks",
                    "Analog Church Study Guide",
                    ("Someone Else",),
                    book_id="study-guide",
                )
            ]
        },
    )

    response = search_books("analog church", [record(), holder])

    assert response.results[0].downloadable is False
    assert all(listing.book_id != "study-guide" for listing in response.results[0].listings)


# -- end-to-end through the transfer pipeline ---------------------------
def _client(sources):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from epub_purifier.web.app import create_app

    return TestClient(create_app(sources=list(sources)))


def _resolving_set(sample_epub, *, data=None):
    holder = Catalogue(
        "standardebooks",
        "Standard Ebooks",
        {
            "Analog Church Jay Y. Kim": [
                book(
                    "standardebooks",
                    "Standard Ebooks",
                    "Analog Church",
                    ("Jay Y. Kim",),
                    book_id="analog-church",
                )
            ]
        },
        data=sample_epub if data is None else data,
    )
    return [record(), holder, *LINK_SOURCES]


def test_resolved_copy_downloads_through_the_cleaning_pipeline(sample_epub):
    with _client(_resolving_set(sample_epub)) as client:
        payload = client.post("/api/search", json={"query": "analog church"}).json()
        result = payload["results"][0]
        assert result["downloadable"] is True
        assert result["security"]["status"] == "scan_on_fetch"

        chosen = next(
            listing
            for listing in result["listings"]
            if listing["source"] == "standardebooks" and not listing["is_search_link"]
        )
        assert chosen["downloadable"] is True

        transfer = client.post(
            "/api/transfer",
            json={"source": chosen["source"], "book_id": chosen["book_id"]},
        )
        assert transfer.status_code == 200
        body = transfer.json()
        assert body["security"]["status"] == "cleaned_and_verified"
        assert body["remaining_seconds"] == 5 * 60 * 60
        assert body["ttl_seconds"] == 5 * 60 * 60
        assert body["report"]["summary"] is not None

        cleaned = client.get(body["download_url"])
        assert cleaned.status_code == 200
        assert cleaned.content[:2] == b"PK"


def test_external_listings_are_refused_by_the_download_endpoint(sample_epub):
    with _client(_resolving_set(sample_epub)) as client:
        payload = client.post("/api/search", json={"query": "analog church"}).json()
        external = [
            listing
            for listing in payload["results"][0]["listings"]
            if listing["is_search_link"]
        ]
        assert {listing["source"] for listing in external} == {"external"}

        for listing in external:
            assert listing["downloadable"] is False
            # A search link carries no book id, so it cannot even name a book
            # to fetch; and the source itself refuses regardless.
            assert listing["book_id"] == ""
            malformed = client.post(
                "/api/transfer",
                json={"source": listing["source"], "book_id": listing["book_id"]},
            )
            assert malformed.status_code == 422
            refused = client.post(
                "/api/transfer",
                json={"source": listing["source"], "book_id": "1"},
            )
            assert refused.status_code == 403
            assert "automated downloads" in refused.json()["detail"]


def test_a_failed_resolved_download_leaves_no_temporary_files(tmp_path):
    with _client(_resolving_set(b"", data=b"not an epub")) as client:
        response = client.post(
            "/api/transfer",
            json={"source": "standardebooks", "book_id": "analog-church"},
        )
        assert response.status_code == 422

    store = tmp_path / "transfer-store"
    assert store.exists()
    assert list(store.iterdir()) == []
