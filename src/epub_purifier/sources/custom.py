"""Sources an operator adds on the server without patching this package.

The built-in registry is deliberately small: it only holds catalogues whose
download path this project can vouch for. An operator who finds another source
that distributes its books legally adds it in a file instead of editing code, by
pointing ``EPUB_PURIFIER_EXTRA_SOURCES`` at it (default: ``sources.json`` in the
working directory):

    {
      "sources": [
        {
          "name": "monoskop",
          "label": "Monoskop",
          "kind": "link",
          "search_template": "https://monoskop.org/index.php?search={query}"
        }
      ]
    }

Two kinds exist:

``kind: "link"``
    Takes part in every search and opens the site in the browser. Nothing is ever
    fetched from it, and the transfer endpoint refuses it. ``search_template``
    must be an https URL containing ``{query}``; ``note`` and ``limitation`` are
    optional labels shown in the UI.

``kind: "module"``
    Loads an adapter you wrote, named ``"package.module:ClassName"``, which is
    constructed with no arguments. It is trusted exactly as much as the rest of
    the code on the server, so it may set ``supports_download = True`` and use
    the normal transfer, scanning and cleaning pipeline. ``name`` and ``label``
    are optional for module entries; when given they must match the adapter.

A ``.toml`` file with the same shape (``[[source]]`` tables) is accepted too,
which needs Python 3.11+ or the ``tomli`` package. A missing file simply means
no extra sources. A file that exists but is wrong raises :class:`SourceConfigError`
naming the offending entry, so a typo can never silently drop a source.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .base import BookSource, SourceError
from .links import LinkOnlySource

#: Environment variable holding the path to the extra-sources file.
ENV_VAR = "EPUB_PURIFIER_EXTRA_SOURCES"

#: Where the file is read from when the environment variable is not set.
DEFAULT_FILE = "sources.json"

#: A source name has to survive the transfer request schema and URL paths.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_KINDS = {"link", "module"}
_OPTIONAL_TEXT = {"note", "limitation"}


class SourceConfigError(SourceError):
    """The extra-sources file exists but cannot be used."""


def extra_sources_path() -> Path:
    """Resolve the configured file, falling back to the working directory."""

    configured = os.environ.get(ENV_VAR, "").strip()
    return Path(configured) if configured else Path.cwd() / DEFAULT_FILE


def load_extra_sources(
    path: Path | str | None = None,
    *,
    reserved: Iterable[str] = (),
) -> list[BookSource]:
    """Read the extra-sources file and return ready-to-use adapters.

    ``reserved`` holds names already taken by built-in sources, so an entry
    cannot shadow one. Every entry is validated before it is used.
    """

    target = Path(path) if path is not None else extra_sources_path()
    try:
        exists = target.is_file()
    except OSError as exc:  # pragma: no cover - unreadable parent directory
        raise SourceConfigError("The extra-sources path cannot be read", detail=str(target)) from exc
    if not exists:
        return []

    payload = _read(target)
    entries = payload.get("sources", [])
    if not isinstance(entries, list):
        raise SourceConfigError(
            "The extra-sources file needs a \"sources\" list", detail=str(target)
        )

    taken = set(reserved)
    sources: list[BookSource] = []
    for index, entry in enumerate(entries):
        source = _build(entry, index, target)
        name = source.name
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            raise SourceConfigError(
                f"Extra source #{index} has an unusable name",
                detail=repr(name),
            )
        if name in taken:
            raise SourceConfigError(
                f"Extra source {name!r} is already in use",
                detail="names must be unique and must not shadow a built-in source",
            )
        taken.add(name)
        sources.append(source)
    return sources


# -- internals -----------------------------------------------------------
def _read(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SourceConfigError("The extra-sources file could not be read", detail=str(path)) from exc

    if suffix in {".toml", ".tml"}:
        loader = _toml_loader()
        try:
            return loader(raw)
        except SourceConfigError:
            raise
        except Exception as exc:
            raise SourceConfigError(
                "The extra-sources file is not valid TOML", detail=str(path)
            ) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceConfigError(
            "The extra-sources file is not valid JSON", detail=str(path)
        ) from exc
    if not isinstance(data, dict):
        raise SourceConfigError("The extra-sources file must hold an object", detail=str(path))
    return data


def _toml_loader():
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:  # pragma: no cover - Python 3.10
            raise SourceConfigError(
                "TOML extra sources require Python 3.11+ or the 'tomli' package; "
                "use a .json file instead"
            ) from exc
    return lambda raw: tomllib.loads(raw.decode("utf-8"))


def _build(entry: object, index: int, path: Path) -> BookSource:
    if not isinstance(entry, dict):
        raise SourceConfigError(f"Extra source #{index} must be an object", detail=str(path))

    kind = entry.get("kind", "link")
    if kind not in _KINDS:
        raise SourceConfigError(
            f"Extra source #{index} has an unknown kind", detail=repr(kind)
        )
    if kind == "module":
        return _build_module(entry, index)

    name = entry.get("name")
    label = entry.get("label")
    if not isinstance(name, str) or not name:
        raise SourceConfigError(f"Extra source #{index} needs a name")
    if not isinstance(label, str) or not label.strip():
        raise SourceConfigError(f"Extra source {name!r} needs a label")

    template = entry.get("search_template")
    if not isinstance(template, str) or "{query}" not in template:
        raise SourceConfigError(
            f"Extra source {name!r} needs search_template containing {{query}}"
        )
    if not template.startswith("https://"):
        raise SourceConfigError(f"Extra source {name!r} needs an https search_template")

    return LinkOnlySource(
        name=name,
        label=label.strip(),
        search_template=template,
        note=_optional_text(entry, "note", f"{label.strip()} is an external catalogue added by this server."),
        limitation=_optional_text(
            entry,
            "limitation",
            f"Search link only: EPUB Purifier does not download from {label.strip()}.",
        ),
    )


def _build_module(entry: dict[str, Any], index: int) -> BookSource:
    target = entry.get("module")
    if not isinstance(target, str) or ":" not in target:
        raise SourceConfigError(
            f"Extra source #{index} needs module = \"package.module:ClassName\"",
            detail=repr(target),
        )
    module_name, _, attribute = target.partition(":")

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - surfaced with context below
        raise SourceConfigError(
            f"Extra source #{index} could not import {module_name!r}", detail=str(exc)
        ) from exc
    factory = getattr(module, attribute, None)
    if factory is None:
        raise SourceConfigError(
            f"Extra source #{index} has no {attribute!r} in {module_name!r}"
        )
    try:
        source = factory()
    except Exception as exc:  # noqa: BLE001 - surfaced with context below
        raise SourceConfigError(
            f"Extra source #{index} could not be constructed", detail=str(exc)
        ) from exc

    for attribute_name in ("name", "label", "supports_download", "note"):
        if not hasattr(source, attribute_name):
            raise SourceConfigError(
                f"Extra source #{index} is not a book source",
                detail=f"missing {attribute_name}",
            )
    for method in ("search", "download"):
        if not callable(getattr(source, method, None)):
            raise SourceConfigError(
                f"Extra source #{index} is not a book source", detail=f"missing {method}()"
            )
    if not isinstance(source.supports_download, bool):
        raise SourceConfigError(
            f"Extra source #{index} is not a book source",
            detail="supports_download must be a bool",
        )

    for attribute_name in ("name", "label"):
        declared = entry.get(attribute_name)
        if declared is not None and declared != getattr(source, attribute_name):
            raise SourceConfigError(
                f"Extra source #{index} declares {attribute_name} {declared!r} but the "
                f"adapter reports {getattr(source, attribute_name)!r}"
            )
    return source


def _optional_text(entry: dict[str, Any], key: str, default: str) -> str:
    value = entry.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


__all__ = [
    "DEFAULT_FILE",
    "ENV_VAR",
    "NAME_PATTERN",
    "SourceConfigError",
    "extra_sources_path",
    "load_extra_sources",
]
