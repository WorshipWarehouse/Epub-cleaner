"""Web API for book search and cleaned downloads (skipped without the web extra)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from epub_purifier.sources.base import BookListing, DownloadRefused, DownloadResult  # noqa: E402
from epub_purifier.sources.links import LinkOnlySource  # noqa: E402
from epub_purifier.web.app import create_app  # noqa: E402

#: A source an operator added in the extra-sources file: searchable, never fetched.
EXTERNAL = LinkOnlySource(
    name="external",
    label="External Index",
    search_template="https://example.org/find?q={query}",
    note="Operator-configured external catalogue.",
    limitation="Search link only: EPUB Purifier does not download from it.",
)


class StubSource:
    name = "stub"
    label = "Stub Library"
    supports_download = True
    note = "stub source for tests"

    def __init__(self, data: bytes) -> None:
        self.data = data

    def search(self, query):
        return [
            BookListing(
                source=self.name,
                source_label=self.label,
                book_id="42",
                title=f"{query} volume one",
                authors=("A. Author",),
                languages=("en",),
                formats=("EPUB",),
                isbn=("9780306406157",),
                details_url="https://example.org/books/42",
                cover_url="https://example.org/books/42.jpg",
                downloadable=True,
            )
        ]

    def download(self, book_id):
        return DownloadResult(
            data=self.data,
            file_name="stub-book.epub",
            origin_url="https://example.org/books/42.epub",
        )

    def cover(self, book_id):
        return b"\xff\xd8\xffcover", "image/jpeg"


def make_client(data: bytes) -> TestClient:
    app = create_app(sources=[StubSource(data), EXTERNAL])
    return TestClient(app)


@pytest.fixture
def client(sample_epub):
    with make_client(sample_epub) as test_client:
        yield test_client


def test_sources_endpoint(client):
    payload = client.get("/api/sources").json()
    by_name = {source["name"]: source for source in payload["sources"]}
    assert by_name["stub"]["supports_download"] is True
    assert by_name["external"]["supports_download"] is False
    assert "extra-sources file" in payload["notice"]


def test_search_returns_listings_and_statuses(client):
    response = client.post("/api/search", json={"query": "dune"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "dune"
    assert payload["results"][0]["title"] == "dune volume one"
    assert payload["results"][0]["cover_url"] == "/api/cover/stub/42"
    assert any(source["name"] == "stub" and source["ok"] for source in payload["sources"])
    assert [source["name"] for source in payload["sources"] if not source["ok"]] == []


def test_search_rejects_unknown_source(client):
    response = client.post("/api/search", json={"query": "dune", "sources": ["nope"]})
    assert response.status_code == 400
    assert "Unknown source" in response.json()["detail"]


def test_search_rejects_short_query(client):
    assert client.post("/api/search", json={"query": "x"}).status_code == 422


class NoEpubSource:
    name = "noepub"
    label = "No EPUB Library"
    supports_download = True
    note = "test source without downloadable editions"

    def search(self, query):
        return []

    def download(self, book_id):
        raise DownloadRefused(
            "Project Gutenberg does not list an EPUB edition for this book",
            detail=book_id,
        )


def test_search_exposes_a_security_status_per_listing(client):
    payload = client.post("/api/search", json={"query": "dune"}).json()
    listings = [item for result in payload["results"] for item in result["listings"]]
    by_source = {item["source"]: item["security"] for item in listings}
    assert by_source["stub"]["status"] == "scan_on_fetch"
    assert "not a guarantee" in by_source["stub"]["disclaimer"].lower()
    assert by_source["external"]["status"] == "link_only"
    assert by_source["external"]["headline"]


def test_transfer_without_an_epub_is_a_validation_refusal(sample_epub):
    app = create_app(sources=[NoEpubSource(), EXTERNAL])
    with TestClient(app) as client:
        response = client.post("/api/transfer", json={"source": "noepub", "book_id": "28838"})
    assert response.status_code == 422
    assert "EPUB edition" in response.json()["detail"]


def test_search_returns_link_only_sources(client):
    payload = client.post("/api/search", json={"query": "dune"}).json()
    links = [
        item
        for result in payload["results"]
        for item in result["listings"]
        if item["is_search_link"]
    ]
    assert {item["source"] for item in links} == {"external"}
    assert all(item["downloadable"] is False for item in links)
    assert all(item["details_url"].startswith("https://") for item in links)


def test_search_groups_sources_for_one_book(client):
    payload = client.post("/api/search", json={"query": "dune"}).json()
    result = payload["results"][0]
    assert result["source_count"] == 1
    assert {
        item["source"] for item in result["listings"] if not item["is_search_link"]
    } == {"stub"}


def test_search_filters_out_a_different_author(client):
    payload = client.post(
        "/api/search", json={"query": "dune", "author": "Wrong Author"}
    ).json()
    assert payload["results"] == []
    assert payload["source_links"]


def test_search_filters_by_language_and_format(client):
    payload = client.post("/api/search", json={"query": "dune", "language": "en"}).json()
    assert len(payload["results"]) == 1

    payload = client.post("/api/search", json={"query": "dune", "language": "fr"}).json()
    assert payload["results"] == []

    payload = client.post("/api/search", json={"query": "dune", "format": "epub"}).json()
    assert len(payload["results"]) == 1

    payload = client.post("/api/search", json={"query": "dune", "format": "PDF"}).json()
    assert payload["results"] == []


def test_search_filters_by_title_and_isbn(client):
    payload = client.post(
        "/api/search", json={"query": "dune", "title": "dune volume one"}
    ).json()
    assert len(payload["results"]) == 1

    payload = client.post(
        "/api/search", json={"query": "dune", "isbn": "9780306406157"}
    ).json()
    assert len(payload["results"]) == 1

    payload = client.post(
        "/api/search", json={"query": "dune", "isbn": "9780141439518"}
    ).json()
    assert payload["results"] == []


def test_transfer_stays_available_until_deleted(client):
    response = client.post("/api/transfer", json={"source": "stub", "book_id": "42"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["download_url"] == f"/api/transfer/{payload['transfer_id']}/download"
    assert payload["delete_url"] == f"/api/transfer/{payload['transfer_id']}"
    assert payload["remaining_seconds"] == 5 * 60 * 60
    assert payload["expires_in"] == "5h 00m"
    security = payload["security"]
    assert security["status"] == "cleaned_and_verified"
    assert "not a guarantee" in security["disclaimer"].lower()
    assert payload["file_name"] == "stub-book.cleaned.epub"

    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.content[:2] == b"PK"
    assert "attachment" in download.headers["content-disposition"]

    # The file is not consumed by a download: it lives until its five-hour
    # expiry, a sweep, or an explicit delete.
    again = client.get(payload["download_url"])
    assert again.status_code == 200

    listing = client.get("/api/transfers")
    assert listing.status_code == 200
    body = listing.json()
    assert body["storage"]["jobs"] == 1
    assert body["storage"]["used_bytes"] > 0
    assert body["transfers"][0]["transfer_id"] == payload["transfer_id"]
    assert "five hours" in body["notice"].lower()

    deleted = client.delete(payload["delete_url"])
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["storage"]["used_bytes"] == 0
    assert client.get(payload["download_url"]).status_code == 404
    assert client.delete(payload["delete_url"]).status_code == 404


def test_transfer_refuses_link_only_source(client):
    response = client.post("/api/transfer", json={"source": "external", "book_id": "1"})
    assert response.status_code == 403
    assert "automated downloads" in response.json()["detail"]


def test_transfer_refuses_unknown_source(client):
    assert client.post("/api/transfer", json={"source": "nope", "book_id": "1"}).status_code == 403


def test_transfer_rejects_malicious_book(malicious_epub):
    with make_client(malicious_epub) as client:
        response = client.post("/api/transfer", json={"source": "stub", "book_id": "42"})
    assert response.status_code == 422
    assert "active payloads" in response.json()["detail"]


def test_download_unknown_transfer(client):
    assert client.get("/api/transfer/abcdef0123456789/download").status_code == 404
    assert client.delete("/api/transfer/abcdef0123456789").status_code == 404
    assert client.delete("/api/transfer/not-a-job").status_code == 404


def test_cover_is_proxied(client):
    response = client.get("/api/cover/stub/42")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8\xff")


def test_cover_unknown_source(client):
    assert client.get("/api/cover/nope/1").status_code == 404


def test_existing_upload_flow_still_works(client, sample_epub):
    response = client.post(
        "/api/inspect", files={"file": ("book.epub", sample_epub, "application/epub+zip")}
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    cleaned = client.post("/api/clean", json={"job_id": job_id})
    assert cleaned.status_code == 200
    assert client.get(f"/api/download/{job_id}").status_code == 200
