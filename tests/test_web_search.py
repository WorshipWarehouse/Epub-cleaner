"""Web API for book search and cleaned downloads (skipped without the web extra)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from epub_purifier.sources.base import BookListing, DownloadResult  # noqa: E402
from epub_purifier.sources.links import ANNAS_ARCHIVE, OCEAN_OF_PDF  # noqa: E402
from epub_purifier.web.app import create_app  # noqa: E402


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
    app = create_app(sources=[StubSource(data), ANNAS_ARCHIVE, OCEAN_OF_PDF])
    return TestClient(app)


@pytest.fixture
def client(sample_epub):
    with make_client(sample_epub) as test_client:
        yield test_client


def test_sources_endpoint(client):
    payload = client.get("/api/sources").json()
    by_name = {source["name"]: source for source in payload["sources"]}
    assert by_name["stub"]["supports_download"] is True
    assert by_name["annas"]["supports_download"] is False
    assert by_name["oceanofpdf"]["supports_download"] is False
    assert "never downloads from them" in payload["notice"]


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


def test_search_returns_link_only_sources(client):
    payload = client.post("/api/search", json={"query": "dune"}).json()
    links = [item for item in payload["results"] if item["is_search_link"]]
    assert {item["source"] for item in links} == {"annas", "oceanofpdf"}
    assert all(item["downloadable"] is False for item in links)
    assert all(item["details_url"].startswith("https://") for item in links)


def test_transfer_then_single_use_download(client):
    response = client.post("/api/transfer", json={"source": "stub", "book_id": "42"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["download_url"] == f"/api/transfer/{payload['transfer_id']}/download"
    security = payload["security"]
    assert security["status"] == "cleaned_and_verified"
    assert "not a guarantee" in security["disclaimer"].lower()
    assert payload["file_name"] == "stub-book.cleaned.epub"

    download = client.get(payload["download_url"])
    assert download.status_code == 200
    assert download.content[:2] == b"PK"
    assert "attachment" in download.headers["content-disposition"]

    again = client.get(payload["download_url"])
    assert again.status_code == 404


def test_transfer_refuses_link_only_source(client):
    response = client.post("/api/transfer", json={"source": "annas", "book_id": "1"})
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
