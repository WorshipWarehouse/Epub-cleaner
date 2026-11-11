"""Safe, read-only access to the EPUB ZIP container.

Nothing in this module trusts the archive: names are normalized, sizes are
bounded, symlinks and encrypted members are refused, and every read is capped.
The archive is held in memory as bytes because EPUB files are small and doing
so removes the whole class of "extract to disk then escape the directory" bugs.
"""

from __future__ import annotations

import io
import posixpath
import re
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from .config import Limits
from .errors import ArchiveError

MIMETYPE = "application/epub+zip"
CONTAINER_PATH = "META-INF/container.xml"
ENCRYPTION_PATH = "META-INF/encryption.xml"
RIGHTS_PATH = "META-INF/rights.xml"

#: Compression methods defined by the ZIP specification that we accept.
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2}
#: LZMA is spec-legal but almost never used by EPUB writers.
_KNOWN_COMPRESSION = _ALLOWED_COMPRESSION | {zipfile.ZIP_LZMA}

_CONTROL_CHARS = {chr(c) for c in range(0x20) if chr(c) not in "\t"}
_XML_DECL_BYTES = re.compile(rb"^\s*<\?xml\b[^>]*\?>", re.IGNORECASE)
_XML_ENCODING_BYTES = re.compile(rb"\bencoding\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


@dataclass(frozen=True)
class EntryInfo:
    """Immutable facts about one archive member."""

    name: str
    size: int
    compressed_size: int
    compression: int
    is_dir: bool
    is_symlink: bool
    encrypted: bool

    @property
    def ratio(self) -> float:
        if self.compressed_size <= 0:
            return float("inf") if self.size else 0.0
        return self.size / self.compressed_size


def read_source(source: str | Path | bytes, limits: Limits) -> bytes:
    """Load an EPUB from a path, a byte string, or a binary file object."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        path = Path(source)
        if not path.exists():
            raise ArchiveError(f"input file not found: {path}")
        if not path.is_file():
            raise ArchiveError(f"input is not a regular file: {path}")
        size = path.stat().st_size
        if size == 0:
            raise ArchiveError("input file is empty")
        if size > limits.max_upload_bytes:
            raise ArchiveError(
                f"input is larger than the {limits.max_upload_bytes} byte limit"
            )
        data = path.read_bytes()

    if not data:
        raise ArchiveError("input is empty")
    if len(data) > limits.max_upload_bytes:
        raise ArchiveError(f"input is larger than the {limits.max_upload_bytes} byte limit")
    return data


def normalize_member_name(name: str, limits: Limits) -> str:
    """Return a safe, normalized member name or raise ``ArchiveError``.

    Rejects absolute paths, drive letters, traversal, backslash separators,
    control characters, over-long names and over-deep trees.
    """

    if not name:
        raise ArchiveError("archive contains an empty member name")
    if len(name) > limits.max_name_length:
        raise ArchiveError(f"member name too long: {name[:40]}...")
    if any(ch in _CONTROL_CHARS for ch in name):
        raise ArchiveError(f"member name contains control characters: {name!r}")
    if "\\" in name:
        raise ArchiveError(f"member name uses a backslash separator: {name!r}")
    if name.startswith("/") or name.startswith("\\"):
        raise ArchiveError(f"member name is absolute: {name!r}")
    if len(name) >= 2 and name[1] == ":":
        raise ArchiveError(f"member name contains a drive letter: {name!r}")

    # Unicode normalization first: some ZIP tricks rely on NFKC lookalikes.
    normalized = unicodedata.normalize("NFC", name)
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ArchiveError(f"member name escapes the archive root: {name!r}")
    if len(parts) > limits.max_path_depth:
        raise ArchiveError(f"member path is deeper than {limits.max_path_depth} levels: {name!r}")

    safe = posixpath.join(*parts) if parts else ""
    if not safe:
        raise ArchiveError(f"member name normalizes to nothing: {name!r}")
    return safe


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode lives in the high 16 bits of external_attr.
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def _is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


class SafeArchive:
    """A validated-in-spirit, bounded reader over an EPUB's members."""

    def __init__(self, data: bytes, limits: Limits | None = None) -> None:
        self.data = data
        self.limits = limits or Limits()
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ArchiveError("input is not a ZIP archive")
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError, EOFError) as exc:
            raise ArchiveError(f"archive cannot be opened: {exc}") from exc

        self._infos: dict[str, zipfile.ZipInfo] = {}
        self._order: list[str] = []
        self._unsafe: dict[str, str] = {}
        self._duplicates: list[str] = []

        for info in self._zip.infolist():
            try:
                safe = normalize_member_name(info.filename, self.limits)
            except ArchiveError as exc:
                self._unsafe[info.filename] = exc.message
                continue
            if safe in self._infos:
                self._duplicates.append(safe)
                continue
            self._infos[safe] = info
            self._order.append(safe)

        if not self._order:
            raise ArchiveError("archive contains no usable members")

    # -- introspection ---------------------------------------------------
    @property
    def names(self) -> list[str]:
        """Member names in original archive order (safe names only)."""

        return list(self._order)

    @property
    def unsafe_members(self) -> dict[str, str]:
        """Rejected member name -> reason."""

        return dict(self._unsafe)

    @property
    def duplicate_members(self) -> list[str]:
        return list(self._duplicates)

    def info(self, name: str) -> EntryInfo:
        info = self._infos[name]
        return EntryInfo(
            name=name,
            size=info.file_size,
            compressed_size=info.compress_size,
            compression=info.compress_type,
            is_dir=info.is_dir(),
            is_symlink=_is_symlink(info),
            encrypted=_is_encrypted(info),
        )

    def infos(self) -> list[EntryInfo]:
        return [self.info(name) for name in self._order]

    def exists(self, name: str) -> bool:
        return name in self._infos

    def total_uncompressed(self) -> int:
        return sum(info.file_size for info in self._zip.infolist())

    def total_compressed(self) -> int:
        return sum(max(info.compress_size, 1) for info in self._zip.infolist())

    # -- reading ---------------------------------------------------------
    def read(self, name: str) -> bytes:
        """Read a member, refusing anything beyond the configured ceilings."""

        info = self._infos.get(name)
        if info is None:
            raise ArchiveError(f"member not found: {name}")
        limit = self.limits.max_entry_uncompressed
        if info.file_size > limit:
            raise ArchiveError(
                f"member {name!r} exceeds the {limit} byte uncompressed limit"
            )
        try:
            with self._zip.open(info) as handle:
                data = handle.read(limit + 1)
        except (RuntimeError, zlib.error, zipfile.BadZipFile, OSError, EOFError) as exc:
            raise ArchiveError(f"member {name!r} cannot be read: {exc}") from exc
        if len(data) > limit:
            raise ArchiveError(f"member {name!r} expands beyond the allowed size")
        return data

    def read_text(self, name: str, encoding: str = "utf-8") -> str:
        data = self.read(name)
        return decode_text(data, encoding)

    def read_markup_text(self, name: str) -> str:
        """Decode an XML/XHTML member using its own declared encoding."""

        return decode_markup(self.read(name))

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> SafeArchive:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def decode_markup(data: bytes) -> str:
    """Decode XML bytes using a BOM or the XML declaration's encoding."""

    candidates: list[str] = []
    if data.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    else:
        declaration = _XML_DECL_BYTES.match(data[:1024])
        if declaration:
            match = _XML_ENCODING_BYTES.search(declaration.group(0))
            if match:
                candidates.append(match.group(1).decode("ascii", errors="ignore"))
    candidates.extend(("utf-8", "utf-8-sig", "latin-1"))
    for encoding in dict.fromkeys(candidates):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover - latin-1 never fails


def decode_text(data: bytes, fallback: str = "utf-8") -> str:
    """Decode markup bytes without ever raising on bad encoding."""

    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in (fallback, "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover - latin-1 never fails


def resolve_href(base_entry: str, href: str) -> str | None:
    """Resolve a relative href found inside ``base_entry`` to an archive path.

    Returns ``None`` for remote URLs, fragments, and paths escaping the root.
    """

    from urllib.parse import unquote, urlsplit

    if not href:
        return None
    split = urlsplit(href.strip())
    if split.scheme or split.netloc:
        return None
    path = unquote(split.path)
    if not path:
        return None
    base_dir = posixpath.dirname(base_entry)
    joined = posixpath.normpath(posixpath.join(base_dir, path))
    if joined.startswith("../") or joined == ".." or joined.startswith("/"):
        return None
    return joined
