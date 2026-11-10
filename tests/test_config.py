"""Configuration loading and customisation."""

from __future__ import annotations

import json

import pytest

from epub_purifier import PurifierConfig, inspect_epub
from epub_purifier.config import Limits, load_config_file
from epub_purifier.errors import UsageError

from .conftest import simple_book

CUSTOM_BOOK = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head>
<body><h1>One</h1><p>Prose that stays.</p>
<div class="footer"><p>Visit ExampleBooks.com for more free downloads.</p></div>
</body></html>"""


def test_json_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "auto_remove_confidence": 0.8,
                "brand_patterns": ["oceanofpdf", "examplebooks"],
                "limits": {"max_entries": 100},
            }
        )
    )
    config = load_config_file(path)
    assert config.auto_remove_confidence == 0.8
    assert config.limits.max_entries == 100
    assert "examplebooks" in config.brand_patterns


def test_toml_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
auto_remove_confidence = 0.7
remove_remote_resources = true
promo_domains = ["oceanofpdf.com", "examplebooks.com"]

[limits]
max_entries = 42
"""
    )
    config = load_config_file(path)
    assert config.remove_remote_resources is True
    assert config.limits.max_entries == 42
    assert "examplebooks.com" in config.promo_domains


def test_tool_section_wrapper(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[tool.epub-purifier]
review_confidence = 0.4
"""
    )
    assert load_config_file(path).review_confidence == 0.4


def test_unknown_key_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"nonsense": True}))
    with pytest.raises(UsageError):
        load_config_file(path)


def test_invalid_regex_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"brand_patterns": ["("]}))
    with pytest.raises(UsageError):
        load_config_file(path)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(UsageError):
        load_config_file(tmp_path / "absent.toml")


def test_threshold_ordering_enforced():
    with pytest.raises(UsageError):
        PurifierConfig.from_dict({"review_confidence": 0.9, "auto_remove_confidence": 0.5})


def test_custom_brand_pattern_is_used():
    config = PurifierConfig.from_dict({"brand_patterns": ["examplebooks"]})
    report = inspect_epub(simple_book(CUSTOM_BOOK), config)
    assert any("ExampleBooks" in (finding.evidence or "") for finding in report.findings)

    from epub_purifier import clean_epub

    cleaned = clean_epub(simple_book(CUSTOM_BOOK), config)
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(cleaned.data)) as archive:
        chapter = archive.read("OEBPS/chapter1.xhtml").decode()
    assert "ExampleBooks" not in chapter
    assert "Prose that stays" in chapter


def test_limits_defaults_are_sane():
    limits = Limits()
    assert limits.max_upload_bytes < limits.max_total_uncompressed
    assert limits.max_entry_uncompressed < limits.max_total_uncompressed
    assert limits.max_compression_ratio > 1
