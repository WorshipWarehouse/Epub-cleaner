"""Core data structures shared by every stage of the pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Threat/annoyance classification ordered from worst to least."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "informational"

    @property
    def weight(self) -> int:
        return _SEVERITY_WEIGHT[self]

    @property
    def label(self) -> str:
        return {
            Severity.CRITICAL: "Critical",
            Severity.HIGH: "High",
            Severity.MEDIUM: "Medium",
            Severity.LOW: "Low",
            Severity.INFO: "Informational",
        }[self]


_SEVERITY_WEIGHT = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}

#: Ordered worst-first; handy for sorting and for report summaries.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
)


class Category(str, Enum):
    """What a finding is about."""

    BRANDING = "branding"
    ADVERTISEMENT = "advertisement"
    TRACKING = "tracking"
    SCRIPT = "script"
    EMBEDDED_OBJECT = "embedded_object"
    EXTERNAL_RESOURCE = "external_resource"
    ARCHIVE = "archive"
    EXECUTABLE = "executable"
    MALFORMED = "malformed"
    METADATA = "metadata"
    STYLE = "style"
    NAVIGATION = "navigation"


class Remediation(str, Enum):
    """How the cleaner is allowed to act on a finding.

    AUTO    unambiguous: it is removed without asking (and reported).
    REVIEW  ambiguous: it is only removed when the caller approves the id.
    REPORT  never removed; shown so a human can decide outside the tool.
    """

    AUTO = "auto"
    REVIEW = "review"
    REPORT = "report"


@dataclass
class Finding:
    """One observation about one resource."""

    rule: str
    severity: Severity
    category: Category
    message: str
    remediation: Remediation = Remediation.REPORT
    confidence: float = 1.0
    target: str | None = None
    location: str | None = None
    evidence: str | None = None
    finding_id: str = ""
    #: Zip entry the finding would drop, when remediation is a whole-file removal.
    drops_entry: str | None = None

    def assign_id(self, ordinal: int) -> None:
        """Derive a stable id from content plus occurrence ordinal.

        Ids must survive an inspect -> clean round trip, so they may not depend
        on object identity, wall-clock time or random values.
        """
        key = "\x1f".join(
            [
                self.rule,
                self.target or "",
                self.location or "",
                self.evidence or "",
                str(ordinal),
            ]
        )
        self.finding_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "rule": self.rule,
            "severity": self.severity.value,
            "severity_label": self.severity.label,
            "category": self.category.value,
            "message": self.message,
            "remediation": self.remediation.value,
            "confidence": round(self.confidence, 3),
            "target": self.target,
            "location": self.location,
            "evidence": self.evidence,
            "drops_entry": self.drops_entry,
        }


@dataclass
class Mutation:
    """A concrete edit bound to a finding.

    The same analyzer pass produces both: the finding is what the report shows,
    the mutation is what the cleaner applies. ``element`` holds a live
    BeautifulSoup tag for DOM-aware edits and is ``None`` for archive-level
    edits.
    """

    finding: Finding
    kind: str  # remove_element | strip_attribute | clean_text | drop_entry | rewrite_entry
    element: Any = None
    attribute: str | None = None
    entry: str | None = None
    #: Replacement payload for whole-resource rewrites (CSS).
    value: str | None = None

    @property
    def approved_by_default(self) -> bool:
        return self.finding.remediation is Remediation.AUTO


@dataclass
class RemovedItem:
    """Record of something the cleaner actually did."""

    finding_id: str
    action: str
    target: str | None
    detail: str
    severity: Severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "severity": self.severity.value,
        }


@dataclass
class AnalysisResult:
    """Everything the inspection stage learned, plus pending edits."""

    findings: list[Finding] = field(default_factory=list)
    mutations: list[Mutation] = field(default_factory=list)

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def ambiguous(self) -> list[Finding]:
        return [f for f in self.findings if f.remediation is Remediation.REVIEW]


def severity_rank(severity: Severity) -> int:
    return severity.weight
