"""Stage 1: validation.

Establishes that we are looking at a real, readable EPUB before any parsing
happens, and rejects the classes of input we must never process: ZIP bombs,
path-traversal members, encrypted/DRM-protected books and containers that do not
match the EPUB specification.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from .archive import (
    CONTAINER_PATH,
    ENCRYPTION_PATH,
    MIMETYPE,
    RIGHTS_PATH,
    SafeArchive,
    decode_text,
    read_source,
)
from .config import (
    EXECUTABLE_EXTENSIONS,
    JUNK_NAMES,
    MARKUP_MEDIA_TYPES,
    PurifierConfig,
)
from .errors import ArchiveError, DrmProtectedError, ValidationError
from .markup import parse_markup
from .models import Category, Finding, Remediation, Severity
from .opf import OPF_MEDIA_TYPE, PackageDocument, find_local

#: Encryption algorithms that only obfuscate fonts; these are not DRM.
FONT_OBFUSCATION_ALGORITHMS = frozenset(
    {
        "http://www.idpf.org/2008/embedding",
        "http://ns.adobe.com/pdf/enc#RC",
    }
)

#: Nested archives are not usable inside an EPUB and are worth a look.
NESTED_ARCHIVE_EXTENSIONS = frozenset({".zip", ".epub", ".rar", ".7z", ".tar", ".gz", ".bz2"})

#: Signed EPUBs break when modified; worth telling the user about.
SIGNATURE_PATHS = ("META-INF/signatures.xml", "META-INF/signature.xml")


@dataclass
class ValidationResult:
    archive: SafeArchive
    package: PackageDocument
    version: str
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def entry_count(self) -> int:
        return len(self.archive.names)

    @property
    def markup_entries(self) -> list[str]:
        paths = []
        for item in self.package.items():
            if item.path and item.media_type in MARKUP_MEDIA_TYPES:
                paths.append(item.path)
        return paths


def validate(data: bytes, config: PurifierConfig | None = None) -> ValidationResult:
    """Run structural validation and return the parsed package document."""

    config = config or PurifierConfig()
    archive = SafeArchive(data, config.limits)
    findings: list[Finding] = []
    notes: list[str] = []

    _check_budget(archive, config)

    findings.extend(_check_members(archive, config))
    findings.extend(_check_mimetype(archive))
    findings.extend(_check_drm(archive, notes))
    findings.extend(_check_junk_files(archive, config))

    package_path = _container_rootfile(archive)
    if not archive.exists(package_path):
        raise ValidationError(f"package document is missing: {package_path}")

    package, version, opf_findings = _parse_package(archive, package_path)
    findings.extend(opf_findings)

    if not package.spine_items():
        raise ValidationError("package document has an empty spine")

    metadata, ebooklib_note = _crosscheck_with_ebooklib(data)
    if ebooklib_note:
        notes.append(ebooklib_note)
    if not metadata.get("title"):
        findings.append(
            Finding(
                rule="metadata.missing_title",
                severity=Severity.LOW,
                category=Category.METADATA,
                message="The package has no dc:title; some readers will show a filename.",
                remediation=Remediation.REPORT,
            )
        )

    result = ValidationResult(
        archive=archive,
        package=package,
        version=version,
        findings=findings,
        notes=notes,
        metadata=metadata,
    )
    return result


def validate_file(
    source: str | bytes,
    config: PurifierConfig | None = None,
) -> ValidationResult:
    config = config or PurifierConfig()
    data = read_source(source, config.limits)
    return validate(data, config)


# -- budget --------------------------------------------------------------
def _check_budget(archive: SafeArchive, config: PurifierConfig) -> None:
    limits = config.limits
    infos = archive.infos()
    if len(archive.names) > limits.max_entries:
        raise ArchiveError(
            f"archive holds {len(archive.names)} members, above the "
            f"{limits.max_entries} member limit"
        )
    total = sum(info.size for info in infos)
    if total > limits.max_total_uncompressed:
        raise ArchiveError(
            f"archive expands to {total} bytes, above the "
            f"{limits.max_total_uncompressed} byte limit (possible ZIP bomb)"
        )
    compressed = sum(max(info.compressed_size, 1) for info in infos)
    if compressed and total / compressed > limits.max_compression_ratio:
        raise ArchiveError(
            "archive compression ratio of "
            f"{total / compressed:.0f}:1 exceeds the limit "
            f"({limits.max_compression_ratio:.0f}:1); refusing to expand it"
        )
    for info in infos:
        if info.size > limits.max_entry_uncompressed:
            raise ArchiveError(
                f"member {info.name!r} expands to {info.size} bytes, above the per-member limit"
            )
        if info.size > 1_000_000 and info.ratio > limits.max_entry_ratio:
            raise ArchiveError(
                f"member {info.name!r} has a suspicious {info.ratio:.0f}:1 compression ratio"
            )


# -- member hygiene ------------------------------------------------------
def _check_members(archive: SafeArchive, config: PurifierConfig) -> list[Finding]:
    findings: list[Finding] = []

    for raw_name, reason in archive.unsafe_members.items():
        findings.append(
            Finding(
                rule="archive.unsafe_member",
                severity=Severity.CRITICAL,
                category=Category.ARCHIVE,
                message="Archive member name is unsafe and will be discarded.",
                remediation=Remediation.AUTO,
                evidence=f"{raw_name}: {reason}",
                target=raw_name,
                drops_entry=raw_name,
            )
        )

    for name in archive.duplicate_members:
        findings.append(
            Finding(
                rule="archive.duplicate_member",
                severity=Severity.MEDIUM,
                category=Category.ARCHIVE,
                message="Duplicate archive member; only the first copy is kept.",
                remediation=Remediation.AUTO,
                target=name,
                drops_entry=name,
            )
        )

    for info in archive.infos():
        if info.encrypted:
            raise DrmProtectedError(
                f"member {info.name!r} is encrypted; EPUB Purifier does not bypass DRM"
            )
        if info.is_symlink:
            findings.append(
                Finding(
                    rule="archive.symlink_member",
                    severity=Severity.CRITICAL,
                    category=Category.ARCHIVE,
                    message="Archive member is a symbolic link and will be discarded.",
                    remediation=Remediation.AUTO,
                    target=info.name,
                    drops_entry=info.name,
                )
            )
        if info.compression not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            findings.append(
                Finding(
                    rule="archive.unusual_compression",
                    severity=Severity.LOW,
                    category=Category.ARCHIVE,
                    message="Member uses an uncommon compression method; it is recompressed.",
                    remediation=Remediation.AUTO,
                    target=info.name,
                    evidence=f"compression method {info.compression}",
                )
            )
        if info.is_dir and not info.name.endswith("/"):
            findings.append(
                Finding(
                    rule="archive.odd_directory",
                    severity=Severity.INFO,
                    category=Category.ARCHIVE,
                    message="Directory entry without a trailing slash.",
                    target=info.name,
                )
            )

    if archive.duplicate_members or archive.unsafe_members:
        findings.append(
            Finding(
                rule="archive.structure_anomaly",
                severity=Severity.HIGH,
                category=Category.MALFORMED,
                message=(
                    "The ZIP container has malformed member names; a hand-crafted archive "
                    "is a common delivery vector for attacks."
                ),
                remediation=Remediation.REPORT,
            )
        )
    return findings


def _check_mimetype(archive: SafeArchive) -> list[Finding]:
    findings: list[Finding] = []
    if not archive.exists("mimetype"):
        findings.append(
            Finding(
                rule="epub.mimetype_missing",
                severity=Severity.MEDIUM,
                category=Category.MALFORMED,
                message="The required 'mimetype' member is missing; it is restored.",
                remediation=Remediation.AUTO,
            )
        )
        return findings

    raw = archive.read("mimetype")
    value = decode_text(raw).strip()
    if value != MIMETYPE:
        findings.append(
            Finding(
                rule="epub.mimetype_incorrect",
                severity=Severity.MEDIUM,
                category=Category.MALFORMED,
                message=f"'mimetype' contains {value!r} instead of {MIMETYPE!r}; it is corrected.",
                remediation=Remediation.AUTO,
            )
        )
    positions = archive.names
    if positions and positions[0] != "mimetype":
        findings.append(
            Finding(
                rule="epub.mimetype_not_first",
                severity=Severity.INFO,
                category=Category.MALFORMED,
                message="'mimetype' is not the first archive member; it is rewritten first.",
                remediation=Remediation.AUTO,
            )
        )
    return findings


def _check_drm(archive: SafeArchive, notes: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    if archive.exists(RIGHTS_PATH):
        raise DrmProtectedError(
            "the book carries an Adobe DRM rights file; EPUB Purifier does not remove DRM"
        )

    for signature in SIGNATURE_PATHS:
        if archive.exists(signature):
            notes.append(
                "The EPUB is digitally signed; any modification invalidates the signature."
            )
            findings.append(
                Finding(
                    rule="epub.digital_signature",
                    severity=Severity.INFO,
                    category=Category.METADATA,
                    message="Book is digitally signed; cleaning invalidates that signature.",
                    remediation=Remediation.REPORT,
                    target=signature,
                )
            )

    if not archive.exists(ENCRYPTION_PATH):
        return findings

    raw = archive.read(ENCRYPTION_PATH)
    try:
        root = DefusedET.fromstring(raw)
    except (DefusedXmlException, SyntaxError) as exc:
        findings.append(
            Finding(
                rule="epub.encryption_unreadable",
                severity=Severity.HIGH,
                category=Category.MALFORMED,
                message="encryption.xml cannot be parsed safely.",
                remediation=Remediation.REPORT,
                target=ENCRYPTION_PATH,
                evidence=str(exc)[:160],
            )
        )
        return findings

    algorithms: set[str] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "EncryptionMethod":
            algorithm = element.get("Algorithm")
            if algorithm:
                algorithms.add(algorithm.strip())

    unknown = {a for a in algorithms if a not in FONT_OBFUSCATION_ALGORITHMS}
    if unknown:
        raise DrmProtectedError(
            "the book is encrypted with "
            + ", ".join(sorted(unknown))
            + "; EPUB Purifier refuses to modify DRM-protected content"
        )
    if algorithms:
        findings.append(
            Finding(
                rule="epub.font_obfuscation",
                severity=Severity.INFO,
                category=Category.METADATA,
                message="Fonts are obfuscated (not DRM); they are left untouched.",
                target=ENCRYPTION_PATH,
            )
        )
    return findings


def _check_junk_files(archive: SafeArchive, config: PurifierConfig) -> list[Finding]:
    findings: list[Finding] = []
    for name in archive.names:
        lowered = name.lower()
        basename = posixpath.basename(lowered)
        extension = posixpath.splitext(basename)[1]

        if extension in EXECUTABLE_EXTENSIONS:
            findings.append(
                Finding(
                    rule="archive.executable",
                    severity=Severity.CRITICAL,
                    category=Category.EXECUTABLE,
                    message=f"Executable or active-content file inside the book: {basename}",
                    remediation=Remediation.AUTO if config.drop_unsafe_files else Remediation.REPORT,
                    target=name,
                    drops_entry=name,
                    evidence=f"extension {extension}",
                )
            )
        elif basename in JUNK_NAMES or lowered.startswith("__macosx/") or "/.git/" in f"/{lowered}":
            findings.append(
                Finding(
                    rule="archive.junk_file",
                    severity=Severity.LOW,
                    category=Category.ARCHIVE,
                    message="Packaging junk file will be removed.",
                    remediation=Remediation.AUTO if config.drop_unsafe_files else Remediation.REPORT,
                    target=name,
                    drops_entry=name,
                )
            )
        elif extension in NESTED_ARCHIVE_EXTENSIONS:
            findings.append(
                Finding(
                    rule="archive.nested_archive",
                    severity=Severity.MEDIUM,
                    category=Category.ARCHIVE,
                    message="Nested archive inside the book; needs human review.",
                    remediation=Remediation.REVIEW,
                    target=name,
                    drops_entry=name,
                )
            )
        elif extension in {".pdf", ".doc", ".docx"}:
            findings.append(
                Finding(
                    rule="archive.extra_document",
                    severity=Severity.LOW,
                    category=Category.ARCHIVE,
                    message="Extra document embedded in the book (sample chapter/commercial insert).",
                    remediation=Remediation.REVIEW,
                    target=name,
                    drops_entry=name,
                )
            )
    return findings


# -- container / package -------------------------------------------------
def _container_rootfile(archive: SafeArchive) -> str:
    if not archive.exists(CONTAINER_PATH):
        raise ValidationError("META-INF/container.xml is missing")
    raw = archive.read(CONTAINER_PATH)
    try:
        root = DefusedET.fromstring(raw)
    except (DefusedXmlException, SyntaxError) as exc:
        raise ValidationError(f"META-INF/container.xml cannot be parsed: {exc}") from exc

    rootfile = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "rootfile":
            continue
        path = element.get("full-path")
        media_type = (element.get("media-type") or "").strip().lower()
        if not path:
            continue
        if media_type and media_type != OPF_MEDIA_TYPE:
            continue
        rootfile = path
        break
    if rootfile is None:
        raise ValidationError("container.xml declares no usable package document")
    # Normalize through the archive so ../ and absolute paths cannot sneak in.
    from .archive import normalize_member_name

    try:
        return normalize_member_name(rootfile, archive.limits)
    except ArchiveError as exc:
        raise ValidationError(f"invalid package path in container.xml: {exc}") from exc


def _parse_package(
    archive: SafeArchive, package_path: str
) -> tuple[PackageDocument, str, list[Finding]]:
    findings: list[Finding] = []
    raw = archive.read(package_path)
    text = decode_text(raw)
    soup, parser = parse_markup(text)

    package_tag = None
    for candidate in soup.find_all(True):
        if candidate.name.rsplit(":", 1)[-1].lower() == "package":
            package_tag = candidate
            break
    if package_tag is None:
        raise ValidationError("package document has no <package> element")
    if parser != "xml":
        findings.append(
            Finding(
                rule="opf.malformed_xml",
                severity=Severity.MEDIUM,
                category=Category.MALFORMED,
                message="The package document is not well-formed XML; it was parsed leniently.",
                remediation=Remediation.REPORT,
                target=package_path,
            )
        )

    version = str(package_tag.get("version") or "").strip() or "unknown"
    if not version.startswith(("2.", "3.")):
        findings.append(
            Finding(
                rule="opf.unknown_version",
                severity=Severity.MEDIUM,
                category=Category.MALFORMED,
                message=f"Unrecognised EPUB version {version!r}; processing best-effort.",
                remediation=Remediation.REPORT,
                target=package_path,
            )
        )

    package = PackageDocument(entry=package_path, soup=soup, version=version)
    if package.manifest is None:
        raise ValidationError("package document has no <manifest>")
    if package.spine is None:
        raise ValidationError("package document has no <spine>")

    if find_local(package.soup, "metadata") is None:
        findings.append(
            Finding(
                rule="opf.missing_metadata",
                severity=Severity.MEDIUM,
                category=Category.MALFORMED,
                message="Package document has no <metadata> section.",
                remediation=Remediation.REPORT,
                target=package_path,
            )
        )
    return package, version, findings


def _crosscheck_with_ebooklib(data: bytes) -> tuple[dict[str, str], str | None]:
    """Read the book with EbookLib as an independent sanity check.

    A failure here is informational: EbookLib is stricter than most readers and
    we already parsed the package ourselves.
    """

    metadata: dict[str, str] = {}
    try:
        import warnings

        from ebooklib import epub

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = epub.read_epub(io.BytesIO(data))
            titles = book.get_metadata("DC", "title")
            creators = book.get_metadata("DC", "creator")
            publishers = book.get_metadata("DC", "publisher")
        if titles:
            metadata["title"] = str(titles[0][0])
        if creators:
            metadata["creator"] = str(creators[0][0])
        if publishers:
            metadata["publisher"] = str(publishers[0][0])
        metadata["ebooklib_items"] = str(len(list(book.get_items())))
    except Exception as exc:  # noqa: BLE001 - third-party parser, any failure is a note
        return metadata, (
            "EbookLib could not read this book "
            f"({type(exc).__name__}); the lenient parser was used instead."
        )
    return metadata, None


def utc_timestamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
