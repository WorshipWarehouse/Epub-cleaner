"""Web API behaviour (skipped when the optional web extra is absent)."""

from __future__ import annotations

import io
import unicodedata
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from epub_purifier.web.app import create_app  # noqa: E402


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "EPUB Purifier" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_inspect_clean_download(client, sample_epub):
    response = client.post(
        "/api/inspect",
        files={"file": ("book.epub", sample_epub, "application/epub+zip")},
    )
    assert response.status_code == 200
    payload = response.json()
    job_id = payload["job_id"]
    assert payload["report"]["summary"]["counts"]["critical"] >= 1
    assert payload["file_name"] == "book.epub"
    assert payload["reviewable"] == [
        finding for finding in payload["report"]["findings"] if finding["remediation"] == "review"
    ]

    cleaned = client.post("/api/clean", json={"job_id": job_id, "approve_all": True})
    assert cleaned.status_code == 200
    report = cleaned.json()["report"]
    assert report["output"]["download_url"] == f"/api/download/{job_id}"
    assert report["summary"]["removed_count"] > 0

    download = client.get(f"/api/download/{job_id}")
    assert download.status_code == 200
    assert download.content[:2] == b"PK"
    assert "attachment" in download.headers["content-disposition"]


def test_batch_download_returns_all_cleaned_epubs(client, sample_epub):
    """The batch endpoint returns every cleaned EPUB in one ZIP archive."""

    job_ids = []
    for name in ("first.epub", "second.epub"):
        inspected = client.post(
            "/api/inspect",
            files={"file": (name, sample_epub, "application/epub+zip")},
        )
        assert inspected.status_code == 200
        job_id = inspected.json()["job_id"]
        cleaned = client.post("/api/clean", json={"job_id": job_id, "approve_all": True})
        assert cleaned.status_code == 200
        job_ids.append(job_id)

    response = client.get("/api/download/batch", params={"job_ids": ",".join(job_ids)})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "epub-purifier-cleaned.zip" in response.headers["content-disposition"]

    cleaned_contents = {}
    for job_id, expected_name in zip(
        job_ids, ("first.cleaned.epub", "second.cleaned.epub"), strict=True
    ):
        download = client.get(f"/api/download/{job_id}")
        assert download.status_code == 200
        cleaned_contents[expected_name] = download.content

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert names == set(cleaned_contents)
        for name in names:
            assert archive.read(name) == cleaned_contents[name]


def test_batch_download_rejects_unknown_jobs(client):
    """A batch containing an unknown job does not produce a partial archive."""

    response = client.get("/api/download/batch", params={"job_ids": "0123456789abcdef"})
    assert response.status_code == 404


def _clean_named_jobs(client, sample_epub, names):
    """Clean one sample EPUB per display name and return the job ids."""

    job_ids = []
    for name in names:
        inspected = client.post(
            "/api/inspect",
            files={"file": (name, sample_epub, "application/epub+zip")},
        )
        assert inspected.status_code == 200
        job_id = inspected.json()["job_id"]
        cleaned = client.post("/api/clean", json={"job_id": job_id, "approve_all": True})
        assert cleaned.status_code == 200
        job_ids.append(job_id)
    return job_ids


def test_batch_download_avoids_case_insensitive_name_collisions(client, sample_epub):
    """Names that differ only by case still produce distinct ZIP entries."""

    job_ids = _clean_named_jobs(client, sample_epub, ("Book.epub", "book.epub"))

    response = client.get("/api/download/batch", params={"job_ids": ",".join(job_ids)})
    assert response.status_code == 200

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 2
        assert len({name.casefold() for name in names}) == 2
        for name in names:
            assert archive.read(name)[:2] == b"PK"


def test_batch_download_normalizes_unicode_name_collisions(client, sample_epub):
    """Composed and decomposed ZIP names are treated as the same filename."""

    job_ids = _clean_named_jobs(client, sample_epub, ("Café.epub", "Cafe\u0301.epub"))

    response = client.get("/api/download/batch", params={"job_ids": ",".join(job_ids)})
    assert response.status_code == 200

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 2
        assert len({unicodedata.normalize("NFC", name).casefold() for name in names}) == 2
        for name in names:
            assert archive.read(name)[:2] == b"PK"


def test_rejects_non_epub(client):
    response = client.post(
        "/api/inspect", files={"file": ("notes.epub", b"not a zip", "application/epub+zip")}
    )
    assert response.status_code == 422
    assert "ZIP" in response.json()["detail"] or "archive" in response.json()["detail"]


def test_drm_upload_is_refused(client, drm_epub):
    response = client.post(
        "/api/inspect", files={"file": ("drm.epub", drm_epub, "application/epub+zip")}
    )
    assert response.status_code == 422
    assert "DRM" in response.json()["detail"]


def test_unknown_job(client):
    assert client.get("/api/download/0123456789abcdef").status_code == 404
    response = client.post("/api/clean", json={"job_id": "0123456789abcdef"})
    assert response.status_code == 404


def test_empty_upload(client):
    response = client.post("/api/inspect", files={"file": ("empty.epub", b"", "application/epub+zip")})
    assert response.status_code == 400


def test_path_traversal_in_filename_is_flattened(client, sample_epub):
    response = client.post(
        "/api/inspect",
        files={"file": ("../../etc/passwd.epub", sample_epub, "application/epub+zip")},
    )
    assert response.status_code == 200
    assert response.json()["file_name"] == "passwd.epub"
