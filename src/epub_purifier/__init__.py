"""EPUB Purifier: detection, cleaning and verification for EPUB files."""

from __future__ import annotations

from .config import PurifierConfig, load_config_file
from .errors import (
    ArchiveError,
    CleaningError,
    DrmProtectedError,
    PurifierError,
    UsageError,
    ValidationError,
)
from .models import Category, Finding, Remediation, Severity
from .pipeline import CleaningResult, clean_epub, inspect_epub
from .report import DISCLAIMER, SAFETY_NOTICE, Report
from .validation import validate

__version__ = "0.1.0"

__all__ = [
    "ArchiveError",
    "Category",
    "CleaningError",
    "CleaningResult",
    "DISCLAIMER",
    "DrmProtectedError",
    "Finding",
    "PurifierConfig",
    "PurifierError",
    "Remediation",
    "Report",
    "SAFETY_NOTICE",
    "Severity",
    "UsageError",
    "ValidationError",
    "clean_epub",
    "inspect_epub",
    "load_config_file",
    "validate",
    "__version__",
]
