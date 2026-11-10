"""Public pipeline API: inspect, then clean."""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from pathlib import Path

from .analysis import Analysis, analyze
from .archive import read_source
from .cleaning import clean_book
from .config import PurifierConfig
from .errors import CleaningError, PurifierError, ValidationError
from .models import Severity
from .repackage import missing_manifest_targets, verify_epub
from .report import Report
from .validation import validate

__version__ = "0.1.0"


@dataclass
class CleaningResult:
    report: Report
    data: bytes | None
    output_path: str | None = None

    def save(self, path: str | Path) -> Path:
        if self.data is None:
            raise CleaningError("nothing to save: no output was produced")
        destination = Path(path)
        if destination.parent and not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.data)
        self.output_path = str(destination)
        return destination


def inspect_epub(
    source: str | Path | bytes,
    config: PurifierConfig | None = None,
    *,
    file_name: str | None = None,
) -> Report:
    """Run validation + inspection and return a report. Never modifies anything."""

    config = config or PurifierConfig()
    data = read_source(source, config.limits)
    validation = validate(data, config)
    analysis = analyze(validation, config)

    report = Report(
        mode="inspect",
        file_name=file_name or _display_name(source),
        file_size=len(data),
        epub_version=validation.version,
        package_path=validation.package.entry,
        metadata=validation.metadata,
        findings=analysis.findings,
        notes=validation.notes,
        stats={
            "entries": validation.entry_count,
            "markup_documents": len(validation.markup_entries),
            "spine_items": len(validation.package.spine_items()),
            "actionable": sum(
                1 for finding in analysis.findings if finding.remediation.value in {"auto", "review"}
            ),
            "needs_review": len(analysis.result.ambiguous()),
        },
    )
    report.findings.extend(_coverage_findings(validation, analysis))
    return report


def clean_epub(
    source: str | Path | bytes,
    config: PurifierConfig | None = None,
    *,
    approved: set[str] | frozenset[str] = frozenset(),
    approve_all: bool = False,
    output: str | Path | None = None,
    dry_run: bool = False,
    file_name: str | None = None,
) -> CleaningResult:
    """Clean the input and return the new EPUB bytes plus a report."""

    config = config or PurifierConfig()
    data = read_source(source, config.limits)
    validation = validate(data, config)
    outcome = clean_book(validation, config, approved=approved, approve_all=approve_all)

    problems = verify_epub(outcome.data, config.limits)
    if problems:
        raise CleaningError(
            "the cleaned EPUB failed structural verification", detail="; ".join(problems[:3])
        )
    missing = missing_manifest_targets(outcome.data)
    if missing:
        outcome.warnings.append(
            "Manifest entries missing from the archive (pre-existing, left untouched): "
            + ", ".join(missing[:6])
        )

    digest = hashlib.sha256(outcome.data).hexdigest()
    display_name = file_name or _display_name(source)
    output_name = None
    if output is not None:
        output_name = posixpath.basename(str(output))
    elif not dry_run:
        output_name = f"{posixpath.splitext(display_name)[0]}.cleaned.epub"

    report = Report(
        mode="clean",
        file_name=display_name,
        file_size=len(data),
        epub_version=validation.version,
        package_path=validation.package.entry,
        metadata=validation.metadata,
        findings=outcome.analysis.findings,
        removed=outcome.applied,
        notes=validation.notes,
        warnings=list(outcome.warnings),
        output_name=output_name,
        output_size=len(outcome.data),
        output_sha256=digest,
        stats={
            "entries_before": validation.entry_count,
            "entries_after": validation.entry_count - len(outcome.dropped),
            "dropped_entries": sorted(outcome.dropped),
            "documents_changed": sorted(outcome.changed),
            "bytes_before": len(data),
            "bytes_after": len(outcome.data),
            "removed": len(outcome.applied),
            "needs_review": len(outcome.analysis.result.ambiguous()),
            "auto_applied": sum(1 for item in outcome.applied),
        },
    )

    if dry_run:
        return CleaningResult(report=report, data=None)

    if output is not None:
        destination = Path(output)
        if destination.parent and not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(outcome.data)
        return CleaningResult(report=report, data=outcome.data, output_path=str(destination))

    return CleaningResult(report=report, data=outcome.data)


def _display_name(source: str | Path | bytes) -> str:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return "upload.epub"
    return Path(source).name


def _coverage_findings(validation, analysis: Analysis) -> list:
    """Report what we *did not* look at, so silence is never mistaken for safety."""

    from .detectors.context import truncate
    from .models import Category, Finding, Remediation

    coverage = analysis.context.metadata.get("unscanned")
    findings = []
    if _has_only_empty_spine(validation):
        findings.append(
            Finding(
                rule="coverage.empty_content",
                severity=Severity.INFO,
                category=Category.MALFORMED,
                message="The spine contains no readable content documents.",
                remediation=Remediation.REPORT,
                evidence=truncate(coverage),
            )
        )
    return findings


def _has_only_empty_spine(validation) -> bool:
    return not validation.package.spine_items()


__all__ = [
    "CleaningResult",
    "PurifierConfig",
    "PurifierError",
    "Report",
    "ValidationError",
    "__version__",
    "clean_epub",
    "inspect_epub",
]
