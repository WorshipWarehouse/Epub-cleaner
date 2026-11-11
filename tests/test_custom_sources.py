"""Operator-defined extra sources: loading, validation and the download path."""

from __future__ import annotations

import json
import sys

import pytest

from epub_purifier.sources.base import DownloadRefused
from epub_purifier.sources.custom import (
    ENV_VAR,
    SourceConfigError,
    extra_sources_path,
    load_extra_sources,
)
from epub_purifier.sources.registry import default_sources, search_books

MODULE_SOURCE = '''
from epub_purifier.sources.base import BookListing, DownloadResult


class ExtraLibrary:
    name = "extralib"
    label = "Extra Library"
    supports_download = True
    note = "operator adapter"

    def search(self, query):
        return [
            BookListing(
                source=self.name,
                source_label=self.label,
                book_id="1",
                title="Analog Church",
                authors=("Jay Y. Kim",),
                formats=("EPUB",),
                downloadable=True,
            )
        ]

    def download(self, book_id):
        return DownloadResult(data=b"PK\\x03\\x04extra", file_name="Analog Church.epub")
'''


def write(tmp_path, payload, name="sources.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def link_entry(**overrides):
    entry = {
        "name": "monoskop",
        "label": "Monoskop",
        "kind": "link",
        "search_template": "https://monoskop.org/index.php?search={query}",
    }
    entry.update(overrides)
    return entry


def install_module(tmp_path, monkeypatch):
    (tmp_path / "extra_source_module.py").write_text(MODULE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "extra_source_module", raising=False)


# -- loading -------------------------------------------------------------
def test_a_link_source_takes_part_in_search(tmp_path):
    sources = load_extra_sources(write(tmp_path, {"sources": [link_entry()]}))

    assert [source.name for source in sources] == ["monoskop"]
    assert sources[0].supports_download is False

    response = search_books("dune messiah", sources)

    assert response.results == ()
    assert [listing.source for listing in response.source_links] == ["monoskop"]
    link = response.source_links[0]
    assert link.is_search_link is True
    assert link.downloadable is False
    assert link.details_url == "https://monoskop.org/index.php?search=dune+messiah"

    with pytest.raises(DownloadRefused):
        sources[0].download("1")


def test_the_environment_variable_selects_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, {"sources": [link_entry()]})
    monkeypatch.setenv(ENV_VAR, str(path))

    assert extra_sources_path() == path
    assert [source.name for source in load_extra_sources()] == ["monoskop"]


def test_the_default_file_is_used_when_the_variable_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    assert extra_sources_path() == tmp_path / "sources.json"

    write(tmp_path, {"sources": [link_entry()]})
    assert [source.name for source in load_extra_sources()] == ["monoskop"]


def test_a_missing_file_means_no_extra_sources(tmp_path):
    assert load_extra_sources(tmp_path / "absent.json") == []


def test_default_sources_include_the_servers_own_file(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(write(tmp_path, {"sources": [link_entry()]})))

    names = [source.name for source in default_sources()]

    assert names[-1] == "monoskop"
    assert names[:3] == ["gutenberg", "wikisource", "internetarchive"]


# -- validation ----------------------------------------------------------
@pytest.mark.parametrize(
    "entry",
    [
        link_entry(name="Monoskop"),
        link_entry(name=""),
        link_entry(name="a" * 40),
        link_entry(label=""),
        link_entry(kind="scrape"),
        link_entry(search_template="https://example.org/find"),
        link_entry(search_template="http://example.org/find?q={query}"),
        {"name": "x", "label": "X", "kind": "module", "module": "no-colon"},
        {"name": "x", "label": "X", "kind": "module", "module": "no_such_module:Thing"},
        "not an object",
    ],
)
def test_a_bad_entry_is_refused(tmp_path, entry):
    with pytest.raises(SourceConfigError):
        load_extra_sources(write(tmp_path, {"sources": [entry]}))


def test_names_must_be_unique_and_must_not_shadow_a_built_in(tmp_path):
    duplicate = write(tmp_path, {"sources": [link_entry(), link_entry()]}, name="dupe.json")
    with pytest.raises(SourceConfigError) as excinfo:
        load_extra_sources(duplicate)
    assert "already in use" in str(excinfo.value)

    shadow = write(tmp_path, {"sources": [link_entry(name="gutenberg")]}, name="shadow.json")
    with pytest.raises(SourceConfigError):
        load_extra_sources(shadow, reserved=["gutenberg"])


def test_a_broken_file_is_reported_not_ignored(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SourceConfigError) as excinfo:
        load_extra_sources(path)
    assert "not valid JSON" in str(excinfo.value)

    wrong_shape = write(tmp_path, {"sources": {"name": "x"}}, name="shape.json")
    with pytest.raises(SourceConfigError):
        load_extra_sources(wrong_shape)


# -- module sources ------------------------------------------------------
def test_a_module_source_is_loaded_and_can_download(tmp_path, monkeypatch):
    install_module(tmp_path, monkeypatch)
    path = write(
        tmp_path,
        {"sources": [{"kind": "module", "module": "extra_source_module:ExtraLibrary"}]},
    )

    sources = load_extra_sources(path)

    assert [source.name for source in sources] == ["extralib"]
    assert sources[0].supports_download is True

    response = search_books("analog church", sources)
    assert response.results[0].downloadable is True
    assert response.results[0].primary.source == "extralib"

    result = sources[0].download("1")
    assert result.file_name == "Analog Church.epub"
    assert result.data.startswith(b"PK")


def test_a_module_source_cannot_claim_a_different_name(tmp_path, monkeypatch):
    install_module(tmp_path, monkeypatch)
    path = write(
        tmp_path,
        {
            "sources": [
                {
                    "kind": "module",
                    "module": "extra_source_module:ExtraLibrary",
                    "name": "something-else",
                }
            ]
        },
    )

    with pytest.raises(SourceConfigError) as excinfo:
        load_extra_sources(path)
    assert "something-else" in str(excinfo.value)


# -- the web API ---------------------------------------------------------
def test_an_extra_source_appears_in_the_web_api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from epub_purifier.web.app import create_app

    path = write(tmp_path, {"sources": [link_entry()]})
    with TestClient(create_app(sources=load_extra_sources(path))) as client:
        payload = client.post("/api/search", json={"query": "dune messiah"}).json()

        sources = {item["name"]: item for item in payload["sources"]}
        assert sources["monoskop"]["supports_download"] is False
        assert [listing["source"] for listing in payload["source_links"]] == ["monoskop"]
        assert payload["source_links"][0]["downloadable"] is False

        refused = client.post("/api/transfer", json={"source": "monoskop", "book_id": "1"})
        assert refused.status_code == 403
