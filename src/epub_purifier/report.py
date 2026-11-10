"""Report model and human-readable rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .models import SEVERITY_ORDER, Finding, RemovedItem, Severity

DISCLAIMER = (
    "EPUB Purifier analyses archive structure and markup only. It does not execute "
    "scripts, unpack exotic formats, or scan with an anti-malware engine, so the "
    "absence of findings is NOT a guarantee that a file is safe. Treat unknown "
    "EPUBs with the same caution as any other downloaded file."
)

SAFETY_NOTICE = (
    "Only clean files you own or are authorised to modify. This tool does not "
    "remove DRM or bypass access controls."
)


@dataclass
class Report:
    """Everything we know about one file, in one serialisable object."""

    mode: str
    file_name: str
    file_size: int
    epub_version: str | None = None
    package_path: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    removed: list[RemovedItem] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_name: str | None = None
    output_size: int | None = None
    output_sha256: str | None = None
    tool_version: str = "0.1.0"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # -- summary ---------------------------------------------------------
    def counts(self) -> dict[str, int]:
        counts = {severity.value: 0 for severity in SEVERITY_ORDER}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        return counts

    def findings_by_severity(self) -> list[Finding]:
        order = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
        return sorted(
            self.findings,
            key=lambda finding: (order.get(finding.severity, 99), finding.rule, finding.target or ""),
        )

    def highest_severity(self) -> Severity | None:
        for severity in SEVERITY_ORDER:
            if any(finding.severity is severity for finding in self.findings):
                return severity
        return None

    def reviewable(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.remediation.value == "review"]

    def verdict(self) -> str:
        if self.errors:
            return "The file could not be fully processed."
        counts = self.counts()
        if counts[Severity.CRITICAL.value] or counts[Severity.HIGH.value]:
            if self.mode == "clean" and self.removed:
                return (
                    "Dangerous or unwanted content was detected and the flagged items were "
                    "removed or neutralised. Remaining content was left untouched."
                )
            return "Dangerous or unwanted content was detected in this file."
        if counts[Severity.MEDIUM.value]:
            return "Unwanted or unusual content was found; review the findings below."
        if counts[Severity.LOW.value] or counts[Severity.INFO.value]:
            return "No dangerous content detected, only minor observations."
        return "No known indicators were detected. This is not a safety guarantee."

    def summary_line(self) -> str:
        counts = self.counts()
        parts = [f"{counts[s.value]} {s.label.lower()}" for s in SEVERITY_ORDER if counts[s.value]]
        detail = ", ".join(parts) if parts else "no findings"
        return f"{len(self.findings)} finding(s): {detail}"

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": {"name": "epub-purifier", "version": self.tool_version},
            "mode": self.mode,
            "generated_at": self.generated_at,
            "file": {
                "name": self.file_name,
                "size": self.file_size,
                "epub_version": self.epub_version,
                "package_path": self.package_path,
                "metadata": self.metadata,
            },
            "output": {
                "name": self.output_name,
                "size": self.output_size,
                "sha256": self.output_sha256,
            },
            "summary": {
                "counts": self.counts(),
                "highest_severity": (
                    self.highest_severity().value if self.highest_severity() else None
                ),
                "verdict": self.verdict(),
                "removed_count": len(self.removed),
            },
            "findings": [finding.to_dict() for finding in self.findings_by_severity()],
            "removed": [item.to_dict() for item in self.removed],
            "stats": self.stats,
            "notes": self.notes,
            "warnings": self.warnings,
            "errors": self.errors,
            "disclaimer": DISCLAIMER,
            "safety_notice": SAFETY_NOTICE,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_text(self, *, width: int = 100) -> str:
        lines: list[str] = []
        title = "EPUB Purifier inspection report" if self.mode == "inspect" else "EPUB Purifier cleaning report"
        lines.append(title)
        lines.append("=" * min(width, len(title)))
        lines.append(f"File      : {self.file_name} ({_human_size(self.file_size)})")
        if self.epub_version:
            lines.append(f"EPUB      : {self.epub_version}")
        if self.metadata.get("title"):
            lines.append(f"Title     : {self.metadata['title']}")
        if self.metadata.get("creator"):
            lines.append(f"Author    : {self.metadata['creator']}")
        lines.append("")
        lines.append(f"Findings  : {self.summary_line()}")
        if self.mode == "clean":
            lines.append(f"Removed   : {len(self.removed)} item(s)")
            if self.output_name:
                lines.append(
                    f"Output    : {self.output_name}"
                    + (f" ({_human_size(self.output_size)})" if self.output_size else "")
                )
        lines.append("")
        lines.append("Assessment")
        lines.append("-" * 10)
        for paragraph in _wrap(self.verdict(), width):
            lines.append(paragraph)
        lines.append("")

        if self.errors:
            lines.append("Errors")
            lines.append("-" * 6)
            for error in self.errors:
                lines.append(f"  ! {error}")
            lines.append("")

        if self.findings:
            lines.append("Findings")
            lines.append("-" * 8)
            for severity in SEVERITY_ORDER:
                group = [f for f in self.findings_by_severity() if f.severity is severity]
                if not group:
                    continue
                lines.append(f"{severity.label} ({len(group)})")
                for finding in group:
                    location = f" [{finding.target}]" if finding.target else ""
                    lines.append(f"  - {finding.rule}{location}")
                    lines.append(f"    {finding.message}")
                    if finding.location:
                        lines.append(f"    at {finding.location}")
                    if finding.evidence:
                        lines.append(f"    evidence: {finding.evidence}")
                    if finding.remediation.value == "review":
                        lines.append(f"    needs confirmation (id {finding.finding_id})")
                lines.append("")

        if self.removed:
            lines.append("Removed")
            lines.append("-" * 7)
            for item in self.removed:
                target = item.target or "(package)"
                lines.append(f"  - {item.action}: {target}")
                if item.detail:
                    lines.append(f"    {item.detail}")
            lines.append("")

        if self.notes or self.warnings:
            lines.append("Notes")
            lines.append("-" * 5)
            for note in [*self.notes, *self.warnings]:
                for paragraph in _wrap(note, width):
                    lines.append(f"  {paragraph}")
            lines.append("")

        lines.append("Safety")
        lines.append("-" * 6)
        for paragraph in _wrap(DISCLAIMER, width):
            lines.append(paragraph)
        for paragraph in _wrap(SAFETY_NOTICE, width):
            lines.append(paragraph)
        return "\n".join(lines).rstrip() + "\n"


def _human_size(value: int | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=max(40, width)) or [""]
