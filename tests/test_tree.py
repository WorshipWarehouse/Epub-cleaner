"""Unzipped book trees are packed before anything is scanned."""

from __future__ import annotations

import io
import zipfile

import pytest

from epub_purifier.errors import ArchiveError
from epub_purifier.pipeline import clean_epub, inspect_epub
from epub_purifier.repackage import verify_epub
from epub_purifier.tree import is_epub_tree, pack_entries, pack_tree


def extract(source: bytes, destination):
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        archive.extractall(destination)
    return destination


def members(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.namelist()


def test_an_extracted_book_is_recognised_and_packed(tmp_path, sample_epub):
    root = extract(sample_epub, tmp_path / "book")

    assert is_epub_tree(root)
    packed = pack_tree(root)

    names = members(packed)
    assert names[0] == "mimetype"
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"
        assert archive.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert "META-INF/container.xml" in names


def test_a_tree_reports_the_same_findings_as_its_archive(tmp_path, sample_epub):
    root = extract(sample_epub, tmp_path / "book")

    from_archive = inspect_epub(sample_epub).to_dict()
    from_tree = inspect_epub(root).to_dict()

    assert from_tree["summary"]["counts"] == from_archive["summary"]["counts"]
    assert sorted(item["rule"] for item in from_tree["findings"]) == sorted(
        item["rule"] for item in from_archive["findings"]
    )


def test_cleaning_a_tree_writes_a_valid_epub(tmp_path, sample_epub):
    root = extract(sample_epub, tmp_path / "book")

    result = clean_epub(root, approve_all=True)

    assert result.data
    assert verify_epub(result.data) == []
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"


def test_a_folder_that_is_not_a_book_is_refused(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "chapter.xhtml").write_text("<html><body/></html>", encoding="utf-8")

    assert not is_epub_tree(folder)
    with pytest.raises(ArchiveError) as excinfo:
        pack_tree(folder)
    assert "mimetype" in str(excinfo.value)
    with pytest.raises(ArchiveError):
        inspect_epub(folder)


def test_a_symlink_inside_the_tree_is_never_followed(tmp_path, sample_epub):
    root = extract(sample_epub, tmp_path / "book")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the book", encoding="utf-8")
    (root / "chain.txt").symlink_to(secret)

    packed = pack_tree(root)

    assert "chain.txt" not in members(packed)


def test_uploaded_paths_lose_their_shared_root():
    packed = pack_entries(
        [
            ("My Book/mimetype", b"application/epub+zip"),
            ("My Book/META-INF/container.xml", b"<container/>"),
            ("My Book/OEBPS/chapter1.xhtml", b"<html/>"),
        ]
    )

    names = members(packed)
    assert names[0] == "mimetype"
    assert "META-INF/container.xml" in names
    assert "OEBPS/chapter1.xhtml" in names
    assert not any(name.startswith("My Book/") for name in names)


def test_a_tree_without_markers_is_rejected_on_upload():
    with pytest.raises(ArchiveError) as excinfo:
        pack_entries([("Book/OEBPS/chapter1.xhtml", b"<html/>")])
    assert "container.xml" in str(excinfo.value)


def test_traversal_names_from_an_upload_are_rejected():
    with pytest.raises(ArchiveError):
        pack_entries(
            [
                ("Book/mimetype", b"application/epub+zip"),
                ("Book/META-INF/container.xml", b"<container/>"),
                ("Book/../../../etc/passwd", b"root"),
            ]
        )


def test_a_wrong_mimetype_is_preserved_for_the_report(tmp_path):
    folder = tmp_path / "book"
    (folder / "META-INF").mkdir(parents=True)
    (folder / "mimetype").write_text("text/plain", encoding="utf-8")
    (folder / "META-INF" / "container.xml").write_text("<container/>", encoding="utf-8")

    packed = pack_tree(folder)

    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.read("mimetype") == b"text/plain"
