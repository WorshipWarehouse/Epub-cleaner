"""Unzipped book trees.

Books sometimes arrive extracted rather than zipped: a ``mimetype`` file and
``META-INF/container.xml`` sitting in a folder next to the markup. Those trees
are packed back into an EPUB here, in memory, so an extracted book and the
archive it came from are validated, scanned and cleaned by exactly the same
code.

Packing is deliberately faithful. Member names go through the same
normalisation as the archive reader (no traversal, no absolute paths, no
backslashes), the tree's own ``mimetype`` content is preserved so a wrong one is
still reported, and the ordinary entry and size budgets are enforced while
reading, before anything is deflated.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

from .archive import CONTAINER_PATH, normalize_member_name
from .config import Limits
from .errors import ArchiveError
from .repackage import build_epub

#: A folder is treated as a book tree only when both markers are present.
TREE_MARKERS: tuple[str, ...] = ("mimetype", CONTAINER_PATH)

#: Filesystem litter that is not part of the book. The archive detectors would
#: drop these anyway; skipping them while packing keeps them out of the report.
_SKIP_DIRS = frozenset({"__MACOSX", ".git", ".svn"})
_SKIP_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def is_epub_tree(path: str | Path) -> bool:
    """True when ``path`` is a directory that looks like an unzipped EPUB."""

    root = Path(path)
    if not root.is_dir():
        return False
    return all((root / marker).is_file() for marker in TREE_MARKERS)


def pack_tree(root: str | Path, limits: Limits | None = None) -> bytes:
    """Pack an unzipped book folder into EPUB bytes."""

    limits = limits or Limits()
    root = Path(root)
    if not is_epub_tree(root):
        raise ArchiveError(
            "that directory is not an unzipped EPUB: it needs a 'mimetype' file "
            f"and {CONTAINER_PATH}"
        )

    entries: list[tuple[str, bytes]] = []
    total = 0
    for base, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        for name in sorted(filenames):
            if name in _SKIP_FILES:
                continue
            full = Path(base) / name
            try:
                info = full.lstat()
            except OSError as exc:
                raise ArchiveError(f"cannot read {name!r} in the book tree: {exc}") from exc
            if not stat.S_ISREG(info.st_mode):
                # Symlinks and special files are never followed, so a tree
                # cannot pull in anything from outside itself.
                continue
            if info.st_size > limits.max_entry_uncompressed:
                raise ArchiveError(
                    f"{name!r} is larger than the {limits.max_entry_uncompressed} byte "
                    "per-file limit"
                )
            total += info.st_size
            if total > limits.max_upload_bytes:
                raise ArchiveError(
                    f"the book tree is larger than the {limits.max_upload_bytes} byte limit"
                )
            entries.append((full.relative_to(root).as_posix(), full.read_bytes()))
    return pack_entries(entries, limits)


def pack_entries(
    entries: Iterable[tuple[str, bytes]],
    limits: Limits | None = None,
) -> bytes:
    """Pack ``(relative name, bytes)`` pairs into an EPUB.

    This is the path a browser uses: each file arrives with its path inside the
    chosen folder, and browsers prefix the folder's own name to every file. That
    one shared top-level folder is stripped, so uploading ``My Book/`` produces
    the same archive as packing the folder itself.
    """

    limits = limits or Limits()
    names: list[str] = []
    payloads: list[bytes] = []
    for raw_name, data in entries:
        name = (raw_name or "").replace("\\", "/").lstrip("/")
        while name.startswith("./"):
            name = name[2:]
        if not name or name.endswith("/"):
            continue
        names.append(name)
        payloads.append(bytes(data))
    names = _strip_shared_root(names)

    prepared: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    mimetype: bytes | None = None
    total = 0
    for name, data in zip(names, payloads, strict=True):
        parts = name.split("/")
        if parts[-1] in _SKIP_FILES or any(part in _SKIP_DIRS for part in parts[:-1]):
            continue
        safe = normalize_member_name(name, limits)
        if safe in seen:
            continue
        seen.add(safe)
        if len(data) > limits.max_entry_uncompressed:
            raise ArchiveError(
                f"{name!r} is larger than the {limits.max_entry_uncompressed} byte "
                "per-file limit"
            )
        total += len(data)
        if total > limits.max_upload_bytes:
            raise ArchiveError(
                f"the book tree is larger than the {limits.max_upload_bytes} byte limit"
            )
        if safe == "mimetype":
            mimetype = data
        prepared.append((safe, data))

    if not prepared:
        raise ArchiveError("the folder contains no files to pack")
    if len(prepared) > limits.max_entries:
        raise ArchiveError(
            f"the folder holds {len(prepared)} files, above the {limits.max_entries} file limit"
        )
    missing = [marker for marker in TREE_MARKERS if marker not in seen]
    if missing:
        raise ArchiveError(
            "the folder is not an unzipped EPUB: it is missing " + " and ".join(missing)
        )
    return build_epub(prepared, mimetype=mimetype)


def _strip_shared_root(names: list[str]) -> list[str]:
    """Drop the single folder a browser prefixes to every file in an upload."""

    if not names:
        return names
    parts = [name.split("/") for name in names]
    if any(len(part) < 2 for part in parts):
        return names
    root = parts[0][0]
    if any(part[0] != root for part in parts):
        return names
    return ["/".join(part[1:]) for part in parts]
