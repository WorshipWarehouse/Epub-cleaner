"""End-to-end proof on a third-party-branded EPUB, through the real entry points.

The fixture is built in code: three chapters of ordinary prose, an illustration,
a stylesheet and working navigation, plus the layer a re-packaging site adds —
promotional footers and a header, oceanofpdf.com links, a dedicated promo page, a
tracking pixel, an injected script, CSS branding and metadata branding. It also
contains legitimate discussion and a footnote citing the same site, which must
survive.

The CLI test runs the installed console module in a subprocess and the web test
starts a real uvicorn server and speaks HTTP, so neither bypasses the entry points
users actually touch.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import httpx

from epub_purifier.repackage import verify_epub

from .converted_book import LEGIT_MARKERS, PROMO_MARKERS, converted_book

EPUB_TYPE = "application/epub+zip"
CONTAINER_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _markup(entries: dict[str, bytes]) -> str:
    return "\n".join(
        data.decode("utf-8", "replace")
        for name, data in entries.items()
        if name.endswith((".xhtml", ".opf", ".css", ".ncx", ".xml"))
    )


def _assert_structurally_valid(data: bytes) -> dict[str, bytes]:
    """Reopen the EPUB and check the structure a reader will actually follow."""

    assert verify_epub(data) == []

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"

        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", CONTAINER_NS)
        assert rootfile is not None
        opf_path = rootfile.get("full-path")
        assert opf_path

        opf = ElementTree.fromstring(archive.read(opf_path))
        manifest = {
            item.get("id"): item
            for item in opf.findall(".//opf:manifest/opf:item", OPF_NS)
        }
        assert manifest
        base = Path(opf_path).parent
        names = set(archive.namelist())
        for item in manifest.values():
            href = item.get("href")
            assert href
            assert (base / href).as_posix() in names, f"manifest item missing: {href}"

        spine = [ref.get("idref") for ref in opf.findall(".//opf:spine/opf:itemref", OPF_NS)]
        assert spine
        assert all(ref in manifest for ref in spine)

        nav_items = [
            item for item in manifest.values() if "nav" in (item.get("properties") or "").split()
        ]
        assert len(nav_items) == 1
        nav_path = (base / nav_items[0].get("href")).as_posix()
        nav = archive.read(nav_path).decode("utf-8")
        for chapter in ("chapter1.xhtml", "chapter2.xhtml", "chapter3.xhtml"):
            assert chapter in nav
        assert "free-library" not in nav

        assert "OEBPS/images/plate.png" in names
        assert archive.read("OEBPS/images/plate.png").startswith(b"\x89PNG")
        assert "OEBPS/styles.css" in names

    return _entries(data)


def _assert_clean_result(data: bytes) -> None:
    entries = _assert_structurally_valid(data)
    text = _markup(entries)

    for marker in PROMO_MARKERS:
        assert marker.lower() not in text.lower(), f"promotional marker survived: {marker}"
    for marker in LEGIT_MARKERS:
        assert marker in text, f"legitimate content was removed: {marker}"

    assert "OEBPS/free-library.xhtml" not in entries
    assert "Chapter One: The Room" in entries["OEBPS/chapter1.xhtml"].decode()
    assert 'src="images/plate.png"' in entries["OEBPS/chapter1.xhtml"].decode()
    assert "font-family: Georgia, serif" in entries["OEBPS/styles.css"].decode()


def _assert_report_matches_fixture(report: dict) -> None:
    rules = {finding["rule"] for finding in report["findings"]}
    assert "security.script" in rules
    assert "tracking.tracker_resource" in rules
    assert "branding.promo_page" in rules
    assert "branding.metadata" in rules
    assert "branding.navigation_entry" in rules
    assert report["summary"]["counts"]["critical"] >= 1
    assert report["summary"]["counts"]["high"] >= 1


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "epub_purifier.cli", *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_cli_cleans_the_branded_fixture(tmp_path):
    source = tmp_path / "branded.epub"
    source.write_bytes(converted_book())
    cleaned = tmp_path / "branded.cleaned.epub"
    report_path = tmp_path / "clean-report.json"

    inspected = _run_cli("inspect", str(source), "--format", "json")
    assert inspected.returncode == 0, inspected.stderr
    inspect_report = json.loads(inspected.stdout)
    _assert_report_matches_fixture(inspect_report)

    result = _run_cli(
        "clean",
        str(source),
        "-o",
        str(cleaned),
        "--approve-all",
        "--report",
        str(report_path),
    )
    assert result.returncode == 0, result.stderr
    assert cleaned.exists()

    clean_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert clean_report["summary"]["removed_count"] > 0
    assert "OEBPS/free-library.xhtml" in clean_report["stats"]["dropped_entries"]
    assert any(item["action"] == "drop_entry" for item in clean_report["removed"])

    _assert_clean_result(cleaned.read_bytes())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_web_upload_inspect_clean_download(tmp_path):
    source = tmp_path / "branded.epub"
    source.write_bytes(converted_book())
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **dict(os.environ),
        "EPUB_PURIFIER_HOST": "127.0.0.1",
        "EPUB_PURIFIER_PORT": str(port),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "epub_purifier.web.app"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            output = server.stdout.read() if server.stdout and server.poll() is not None else ""
            raise AssertionError(f"web server did not become healthy\n{output}")

        with httpx.Client(base_url=base, timeout=120) as client:
            inspected = client.post(
                "/api/inspect",
                files={"file": ("branded.epub", source.read_bytes(), EPUB_TYPE)},
            )
            assert inspected.status_code == 200, inspected.text
            payload = inspected.json()
            job_id = payload["job_id"]
            _assert_report_matches_fixture(payload["report"])

            clean = client.post("/api/clean", json={"job_id": job_id, "approve_all": True})
            assert clean.status_code == 200, clean.text
            clean_report = clean.json()["report"]
            assert clean_report["summary"]["removed_count"] > 0
            download_url = clean_report["output"]["download_url"]
            assert download_url == f"/api/download/{job_id}"

            download = client.get(download_url)
            assert download.status_code == 200
            assert download.headers["content-type"].startswith(EPUB_TYPE)
            assert download.content[:2] == b"PK"
            _assert_clean_result(download.content)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
