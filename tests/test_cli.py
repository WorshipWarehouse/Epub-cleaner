"""Command line behaviour and exit codes."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from epub_purifier.cli import main
from epub_purifier.repackage import verify_epub


def _write(tmp_path, data: bytes, name: str = "book.epub"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_inspect_prints_report(sample_epub, tmp_path, capsys):
    path = _write(tmp_path, sample_epub)
    assert main(["inspect", str(path)]) == 0
    output = capsys.readouterr().out
    assert "EPUB Purifier inspection report" in output
    assert "Findings" in output
    assert "OceanofPDF" in output or "oceanofpdf" in output.lower()


def test_inspect_json(sample_epub, tmp_path, capsys):
    path = _write(tmp_path, sample_epub)
    assert main(["inspect", str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "inspect"
    assert payload["file"]["name"] == "book.epub"


def test_inspect_json_file(sample_epub, tmp_path, capsys):
    path = _write(tmp_path, sample_epub)
    report_path = tmp_path / "report.json"
    assert main(["inspect", str(path), "--json", str(report_path)]) == 0
    payload = json.loads(report_path.read_text())
    assert payload["summary"]["counts"]["critical"] >= 1


def test_missing_file_exits_with_validation_code(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "nope.epub")]) == 3
    assert "not found" in capsys.readouterr().err


def test_fail_on_critical(sample_epub, tmp_path):
    path = _write(tmp_path, sample_epub)
    assert main(["inspect", str(path), "--fail-on", "critical"]) == 1


def test_fail_on_never_is_zero(sample_epub, tmp_path):
    path = _write(tmp_path, sample_epub)
    assert main(["inspect", str(path)]) == 0


def test_clean_writes_valid_output(sample_epub, tmp_path):
    path = _write(tmp_path, sample_epub)
    target = tmp_path / "clean.epub"
    assert main(["clean", str(path), "-o", str(target), "--approve-all"]) == 0
    assert target.exists()
    assert verify_epub(target.read_bytes()) == []

    with zipfile.ZipFile(io.BytesIO(target.read_bytes())) as archive:
        assert "OEBPS/promo.xhtml" not in archive.namelist()


def test_clean_dry_run(sample_epub, tmp_path, capsys):
    path = _write(tmp_path, sample_epub)
    assert main(["clean", str(path), "--dry-run", "--approve-all"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_clean_leaves_ambiguous_without_approval(review_epub, tmp_path):
    path = _write(tmp_path, review_epub, "review.epub")
    target = tmp_path / "out.epub"
    assert main(["clean", str(path), "-o", str(target)]) == 0
    with zipfile.ZipFile(target) as archive:
        chapter = archive.read("OEBPS/chapter1.xhtml").decode()
    assert "OceanofPDF" in chapter


def test_clean_approve_single_id(review_epub, tmp_path, capsys):
    path = _write(tmp_path, review_epub, "review.epub")
    assert main(["inspect", str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    reviewable = [f for f in payload["findings"] if f["remediation"] == "review"]
    assert reviewable

    target = tmp_path / "approved.epub"
    assert main(["clean", str(path), "-o", str(target), "--approve", reviewable[0]["id"]]) == 0
    with zipfile.ZipFile(target) as archive:
        chapter = archive.read("OEBPS/chapter1.xhtml").decode()
    assert "OceanofPDF" not in chapter


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_invalid_thresholds_are_usage_error(sample_epub, tmp_path, capsys):
    path = _write(tmp_path, sample_epub)
    code = main(["inspect", str(path), "--min-confidence", "0.2", "--review-confidence", "0.9"])
    assert code == 2
    assert "confidence" in capsys.readouterr().err


def test_drm_exit_code(drm_epub, tmp_path, capsys):
    path = _write(tmp_path, drm_epub, "drm.epub")
    assert main(["inspect", str(path)]) == 4
    assert "DRM" in capsys.readouterr().err


def test_help_is_available(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "inspect" in capsys.readouterr().out
