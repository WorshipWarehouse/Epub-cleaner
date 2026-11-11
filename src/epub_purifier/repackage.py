"""EPUB repackaging and output verification."""

from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from collections.abc import Iterable
from datetime import datetime, timezone

from defusedxml import ElementTree as DefusedET
from lxml import etree

from .archive import CONTAINER_PATH, MIMETYPE, SafeArchive
from .config import Limits
from .opf import (
    PackageDocument,
    find_local,
    is_valid_modified_timestamp,
    iter_local,
    modified_timestamps,
)

FIXED_DATE = (1980, 1, 1, 0, 0, 0)
_XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"
_XHTML_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_XHTML_EXTENSIONS = frozenset({".xhtml", ".html", ".htm"})
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def build_epub(
    entries: Iterable[tuple[str, bytes]],
    *,
    moment: datetime | None = None,
    compress_level: int = 9,
) -> bytes:
    """Write a spec-compliant EPUB: stored mimetype first, then the rest.

    ``mimetype`` is written exactly as required (first member, no compression, no
    extra field). Everything else is deflated; the input order is otherwise kept.
    """

    moment = moment or datetime.now(timezone.utc)
    date_time = (
        moment.year,
        moment.month,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
    )

    ordered: list[tuple[str, bytes]] = []
    container: list[tuple[str, bytes]] = []
    rest: list[tuple[str, bytes]] = []
    for name, data in entries:
        if name == "mimetype":
            continue
        if name == CONTAINER_PATH:
            container.append((name, data))
        else:
            rest.append((name, data))
    ordered.extend(container)
    ordered.extend(rest)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("mimetype", date_time=date_time)
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, MIMETYPE.encode("ascii"))

        for name, data in ordered:
            member = zipfile.ZipInfo(name, date_time=date_time)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            archive.writestr(member, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=compress_level)

    return buffer.getvalue()


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _text(element) -> str:
    return " ".join(element.get_text(" ").split())


def _parse_xml(data: bytes):
    parser = etree.XMLParser(
        recover=False,
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
    )
    return etree.fromstring(data, parser)


def _check_content_documents(archive: SafeArchive, package: PackageDocument, problems: list[str]) -> None:
    """Reject malformed XHTML and non-empty XHTML void elements."""

    for item in package.items():
        if not item.path:
            continue
        suffix = posixpath.splitext(item.path)[1].lower()
        if item.media_type not in _XHTML_MEDIA_TYPES and suffix not in _XHTML_EXTENSIONS:
            continue
        if not archive.exists(item.path):
            continue
        try:
            root = _parse_xml(archive.read(item.path))
        except Exception as exc:  # noqa: BLE001 - report any reader/parser failure
            problems.append(f"content document is not well-formed XML: {item.href} ({exc})")
            continue
        if not isinstance(root.tag, str) or _local_name(root.tag) != "html":
            problems.append(f"content document has no <html> root: {item.href}")
            continue
        namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        if namespace != _XHTML_NAMESPACE:
            problems.append(f"content document is not in the XHTML namespace: {item.href}")
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            name = _local_name(element.tag)
            if name in _VOID_ELEMENTS and (element.text or len(element)):
                problems.append(f"content document has non-empty <{name}>: {item.href}")
                break


def _metadata_values(metadata, name: str) -> list[str]:
    return [value for element in iter_local(metadata, name) if (value := _text(element))]


def _check_epub3_package(archive: SafeArchive, package: PackageDocument, problems: list[str]) -> None:
    """Check the package-level requirements Apple Books and EPUB 3 enforce."""

    if not package.version.startswith("3."):
        return

    nav_items = [item for item in package.items() if "nav" in item.properties]
    if len(nav_items) != 1:
        problems.append("EPUB 3 package must declare exactly one navigation document")
    else:
        nav = nav_items[0]
        if not nav.path:
            problems.append("navigation document has no local path")
        elif not archive.exists(nav.path):
            problems.append(f"navigation document missing from archive: {nav.href}")
        else:
            try:
                root = _parse_xml(archive.read(nav.path))
            except Exception as exc:  # noqa: BLE001 - report any reader/parser failure
                problems.append(f"navigation document is not well-formed XML: {nav.href} ({exc})")
            else:
                if not isinstance(root.tag, str) or _local_name(root.tag) != "html":
                    problems.append(f"navigation document has no <html> root: {nav.href}")
                if not any(
                    "toc" in element.get("{http://www.idpf.org/2007/ops}type", "").split()
                    for element in root.iter(f"{{{_XHTML_NAMESPACE}}}nav")
                ):
                    problems.append(f"navigation document has no table of contents: {nav.href}")

    modified = modified_timestamps(package)
    if len(modified) != 1:
        problems.append("EPUB 3 package must have exactly one dcterms:modified")
    elif not is_valid_modified_timestamp(modified[0]):
        problems.append("dcterms:modified is not a valid UTC timestamp")

    metadata = package.metadata
    if metadata is None:
        problems.append("EPUB 3 package has no metadata")
        return

    package_tag = find_local(package.soup, "package")
    unique_id = str(package_tag.get("unique-identifier") or "").strip() if package_tag else ""
    if not unique_id:
        problems.append("EPUB 3 package has no unique-identifier")
    else:
        identifiers = [
            element
            for element in iter_local(metadata, "identifier")
            if str(element.get("id") or "") == unique_id and _text(element)
        ]
        if not identifiers:
            problems.append("EPUB 3 package has no identifier matching unique-identifier")

    if not _metadata_values(metadata, "title"):
        problems.append("EPUB 3 package has no dc:title")
    if not _metadata_values(metadata, "language"):
        problems.append("EPUB 3 package has no dc:language")


def verify_epub(
    data: bytes,
    limits: Limits | None = None,
    *,
    check_references: bool = False,
) -> list[str]:
    """Cheap structural check of a freshly written EPUB.

    Returns a list of problems; an empty list means the container shape is valid.
    Missing manifest targets are only an error when ``check_references`` is set:
    plenty of real books ship with a broken image reference that has nothing to
    do with cleaning, and repairing that is not our job.

    This is not a replacement for EPUBCheck and says nothing about the *content*
    being safe.
    """

    problems: list[str] = []
    archive: SafeArchive | None = None
    try:
        archive = SafeArchive(data, limits or Limits())
    except Exception as exc:  # noqa: BLE001 - surface any reader failure as a problem
        return [f"output is not a readable ZIP archive: {exc}"]

    try:
        names = archive.names
        if not names or names[0] != "mimetype":
            problems.append("mimetype is not the first archive member")

        with zipfile.ZipFile(io.BytesIO(data)) as raw:
            first = raw.infolist()[0]
            if first.filename != "mimetype":
                problems.append("mimetype is not the first physical member")
            elif first.compress_type != zipfile.ZIP_STORED:
                problems.append("mimetype is compressed")
            elif raw.read("mimetype").strip() != MIMETYPE.encode("ascii"):
                problems.append("mimetype content is wrong")

        if not archive.exists(CONTAINER_PATH):
            problems.append("META-INF/container.xml is missing")
        else:
            try:
                root = DefusedET.fromstring(archive.read(CONTAINER_PATH))
                rootfiles = [
                    element.get("full-path")
                    for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "rootfile"
                ]
                if not rootfiles or not rootfiles[0]:
                    problems.append("container.xml declares no rootfile")
                elif not archive.exists(rootfiles[0]):
                    problems.append(f"package document missing: {rootfiles[0]}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"container.xml is unreadable: {exc}")

        # Every relative reference from the OPF must resolve to a real member.
        from .markup import parse_markup

        if archive.exists(CONTAINER_PATH):
            package_path = None
            try:
                root = DefusedET.fromstring(archive.read(CONTAINER_PATH))
                for element in root.iter():
                    if element.tag.rsplit("}", 1)[-1] == "rootfile":
                        package_path = element.get("full-path")
                        break
            except Exception:  # noqa: BLE001
                package_path = None
            if package_path and archive.exists(package_path):
                soup, _ = parse_markup(archive.read_text(package_path))
                package_tag = find_local(soup, "package")
                version = (
                    str(package_tag.get("version") or "").strip()
                    if package_tag is not None
                    else ""
                )
                package = PackageDocument(entry=package_path, soup=soup, version=version)
                _check_content_documents(archive, package, problems)
                _check_epub3_package(archive, package, problems)
                if check_references:
                    for item in package.items():
                        if item.path and "://" not in item.href and not archive.exists(item.path):
                            problems.append(f"manifest item missing from archive: {item.href}")
                if not package.spine_items():
                    problems.append("spine has no usable items")
    finally:
        archive.close()
    return problems


def missing_manifest_targets(data: bytes) -> list[str]:
    """List manifest hrefs whose files are absent (reported, never fatal)."""

    missing: list[str] = []
    try:
        archive = SafeArchive(data)
    except Exception:  # noqa: BLE001
        return missing
    try:
        from .markup import parse_markup
        from .opf import PackageDocument

        root = DefusedET.fromstring(archive.read(CONTAINER_PATH))
        package_path = None
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "rootfile":
                package_path = element.get("full-path")
                break
        if not package_path or not archive.exists(package_path):
            return missing
        soup, _ = parse_markup(archive.read_text(package_path))
        package = PackageDocument(entry=package_path, soup=soup, version="")
        for item in package.items():
            if item.path and "://" not in item.href and not archive.exists(item.path):
                missing.append(item.href)
    except Exception:  # noqa: BLE001
        return missing
    finally:
        archive.close()
    return missing


def describe_sizes(before: int, after: int) -> str:
    def human(value: int) -> str:
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{value} B"
            value /= 1024.0
        return f"{value:.1f} GiB"  # pragma: no cover

    delta = before - after
    return f"{human(before)} -> {human(after)} ({delta:+d} bytes)"


def normalize_entry_name(name: str) -> str:
    return posixpath.normpath(name)
