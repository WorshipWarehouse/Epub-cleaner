"""OPF package document access and structural pruning.

Manifest items, spine references, guide references and metadata edits all live
here, so the cleaning stage never has to hand-assemble XML.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import unquote

from bs4 import BeautifulSoup
from bs4.element import Tag

from .archive import resolve_href
from .markup import local_name

OPF_MEDIA_TYPE = "application/oebps-package+xml"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"


@dataclass
class ManifestItem:
    item_id: str
    href: str
    media_type: str
    properties: tuple[str, ...]
    path: str | None
    element: Tag


@dataclass
class PackageDocument:
    """A parsed OPF, with convenience accessors for its main sections."""

    entry: str
    soup: BeautifulSoup
    version: str

    @property
    def opf_dir(self) -> str:
        return posixpath.dirname(self.entry)

    # -- sections --------------------------------------------------------
    def _section(self, name: str) -> Tag | None:
        return find_local(self.soup, name)

    @property
    def metadata(self) -> Tag | None:
        return self._section("metadata")

    @property
    def manifest(self) -> Tag | None:
        return self._section("manifest")

    @property
    def spine(self) -> Tag | None:
        return self._section("spine")

    @property
    def guide(self) -> Tag | None:
        return self._section("guide")

    # -- items -----------------------------------------------------------
    def items(self) -> list[ManifestItem]:
        manifest = self.manifest
        if manifest is None:
            return []
        items: list[ManifestItem] = []
        for element in iter_local(manifest, "item"):
            href = (element.get("href") or "").strip()
            properties = tuple(str(element.get("properties") or "").split())
            items.append(
                ManifestItem(
                    item_id=str(element.get("id") or ""),
                    href=href,
                    media_type=str(element.get("media-type") or "").strip().lower(),
                    properties=properties,
                    path=resolve_href(self.entry, href),
                    element=element,
                )
            )
        return items

    def item_by_id(self, item_id: str) -> ManifestItem | None:
        for item in self.items():
            if item.item_id == item_id:
                return item
        return None

    def item_by_path(self, path: str) -> ManifestItem | None:
        for item in self.items():
            if item.path == path:
                return item
        return None

    def spine_items(self) -> list[ManifestItem]:
        spine = self.spine
        if spine is None:
            return []
        by_id = {item.item_id: item for item in self.items()}
        result: list[ManifestItem] = []
        for ref in iter_local(spine, "itemref"):
            item = by_id.get(str(ref.get("idref") or ""))
            if item is not None:
                result.append(item)
        return result

    def nav_entry(self) -> str | None:
        for item in self.items():
            if "nav" in item.properties and item.path:
                return item.path
        return None

    def ncx_entry(self) -> str | None:
        for item in self.items():
            if item.media_type == NCX_MEDIA_TYPE and item.path:
                return item.path
        return None

    def unique_identifier(self) -> str | None:
        package = find_local(self.soup, "package")
        value = package.get("unique-identifier") if package is not None else None
        return str(value).strip() or None if value else None


# -- namespace-tolerant traversal ----------------------------------------
def find_local(parent: Tag | BeautifulSoup, name: str) -> Tag | None:
    wanted = name.lower()
    for element in parent.find_all(True):
        if local_name(element.name) == wanted:
            return element
    return None


def iter_local(parent: Tag | BeautifulSoup, name: str) -> Iterator[Tag]:
    wanted = name.lower()
    for element in parent.find_all(True):
        if local_name(element.name) == wanted:
            yield element


# -- structural edits ----------------------------------------------------
def remove_manifest_items(pkg: PackageDocument, item_ids: Iterable[str]) -> list[str]:
    """Remove manifest items by id; returns the ids actually removed."""

    wanted = {item_id for item_id in item_ids if item_id}
    if not wanted:
        return []
    removed: list[str] = []
    for item in pkg.items():
        if item.item_id in wanted:
            item.element.decompose()
            removed.append(item.item_id)
    return removed


def remove_manifest_paths(pkg: PackageDocument, paths: Iterable[str]) -> list[str]:
    wanted = {path for path in paths if path}
    if not wanted:
        return []
    ids = [item.item_id for item in pkg.items() if item.path in wanted]
    return remove_manifest_items(pkg, ids)


def prune_spine(pkg: PackageDocument, removed_ids: Iterable[str]) -> int:
    """Drop spine itemrefs whose manifest item is gone or dangling."""

    spine = pkg.spine
    if spine is None:
        return 0
    known = {item.item_id for item in pkg.items()}
    removed = set(removed_ids)
    count = 0
    for ref in list(iter_local(spine, "itemref")):
        idref = str(ref.get("idref") or "")
        if idref not in known or idref in removed:
            ref.decompose()
            count += 1
    return count


def prune_guide(pkg: PackageDocument, dropped_paths: Iterable[str]) -> int:
    guide = pkg.guide
    if guide is None:
        return 0
    dropped = set(dropped_paths)
    count = 0
    for ref in list(iter_local(guide, "reference")):
        href = str(ref.get("href") or "")
        target = resolve_href(pkg.entry, href)
        if target in dropped:
            ref.decompose()
            count += 1
    return count


def prune_empty_sections(pkg: PackageDocument) -> None:
    """Remove manifest/spine/guide elements left with no children."""

    for name in ("manifest", "spine", "guide"):
        section = find_local(pkg.soup, name)
        if section is not None and not section.find(True):
            # A spine with no items is invalid; only prune empty manifest/guide.
            if name == "spine":
                continue
            section.decompose()


def remove_remote_manifest_items(pkg: PackageDocument) -> list[str]:
    """Drop manifest items pointing outside the container (invalid in EPUB)."""

    ids: list[str] = []
    for item in pkg.items():
        if item.path is None and item.href and "://" in item.href:
            ids.append(item.item_id)
    return remove_manifest_items(pkg, ids)


def set_modified_timestamp(pkg: PackageDocument, timestamp: str) -> bool:
    """Update (or add) the dcterms:modified meta used by EPUB 3."""

    if pkg.metadata is None:
        return False
    for meta in iter_local(pkg.metadata, "meta"):
        prop = str(meta.get("property") or "").lower()
        if prop == "dcterms:modified":
            _set_meta_value(meta, timestamp)
            return True
    if pkg.version.startswith("2."):
        # OPF 2.0 has no dcterms:modified; adding one would make the package invalid.
        return False
    meta = pkg.soup.new_tag("meta")
    meta["property"] = "dcterms:modified"
    meta.string = timestamp
    pkg.metadata.append(meta)
    return True


def _set_meta_value(meta: Tag, value: str) -> None:
    """Write the meta value as EPUB 3 element text and nothing else."""

    if meta.get("content") is not None:
        del meta["content"]
    meta.string = value


_MODIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def modified_timestamps(pkg: PackageDocument) -> list[str]:
    """Return the dcterms:modified values in package order."""

    if pkg.metadata is None:
        return []
    values: list[str] = []
    for meta in iter_local(pkg.metadata, "meta"):
        if str(meta.get("property") or "").lower() != "dcterms:modified":
            continue
        value = (meta.get_text() or "").strip()
        if value:
            values.append(value)
    return values


def has_valid_modified_timestamp(pkg: PackageDocument) -> bool:
    values = modified_timestamps(pkg)
    return len(values) == 1 and bool(_MODIFIED_RE.fullmatch(values[0]))


def manifest_paths(pkg: PackageDocument) -> dict[str, str]:
    """Map manifest id -> archive path (only local items)."""

    return {item.item_id: item.path for item in pkg.items() if item.path}


def relative_href(from_entry: str, target_entry: str) -> str:
    """Compute an href from one archive entry to another."""

    from_dir = posixpath.dirname(from_entry)
    rel = posixpath.relpath(target_entry, from_dir or ".")
    return unquote(rel)
