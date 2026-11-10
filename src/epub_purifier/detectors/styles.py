"""CSS inspection: generated-content branding, script expressions and beacons.

The stylesheet is never rewritten during inspection. Each problem is emitted as
a declaration-level mutation, so cleaning can rebuild the file from exactly the
edits the caller approved — which is what makes ``--approve`` meaningful for CSS.
"""

from __future__ import annotations

import re

from ..models import Category, Finding, Mutation, Remediation, Severity
from .context import Context, Resource, truncate

_RULE = re.compile(r"(?P<pre>[^{}]*)\{(?P<body>[^{}]*)\}", re.DOTALL)
_IMPORT = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?[^'\")]+['\"]?\s*\)?[^;{}]*;?", re.IGNORECASE
)
_SCRIPT_CSS = re.compile(r"(?:expression\s*\(|-moz-binding\s*:|behavior\s*:)", re.IGNORECASE)
_URL = re.compile(r"url\(\s*['\"]?(?P<url>[^'\")]+)", re.IGNORECASE)
_QUOTED = re.compile(r"['\"](?P<url>[^'\"]+)['\"]")


def detect_stylesheet(ctx: Context, resource: Resource) -> list[Mutation]:
    """Find removable declarations and remote imports; change nothing yet."""

    text = resource.text or ""
    if not text.strip():
        return []

    mutations: list[Mutation] = []

    for match in _RULE.finditer(text):
        prelude = match.group("pre").strip()
        for declaration in match.group("body").split(";"):
            stripped = declaration.strip()
            if not stripped:
                continue
            problem = _declaration_problem(stripped, ctx)
            if problem is None:
                continue
            rule, severity, message, remediation, category = problem
            finding = Finding(
                rule=rule,
                severity=severity,
                category=category,
                message=message,
                remediation=remediation,
                confidence=0.9 if remediation is Remediation.AUTO else 0.6,
                target=resource.path,
                location=prelude[:80] or "(stylesheet)",
                evidence=truncate(stripped),
            )
            mutations.append(
                Mutation(
                    finding=finding,
                    kind="css_declaration",
                    entry=resource.path,
                    value=stripped,
                    attribute=prelude,
                )
            )

    for match in _IMPORT.finditer(text):
        statement = match.group(0)
        url = _first_url(statement)
        check = ctx.classify(url) if url else None
        if check is None or not (check.is_remote or check.is_protocol_relative):
            continue
        tracking = check.is_tracker
        promotional = check.is_promo
        automatic = tracking or promotional or ctx.config.remove_remote_resources
        finding = Finding(
            rule="tracking.style_import" if tracking else "style.remote_import",
            severity=Severity.HIGH if tracking else Severity.MEDIUM,
            category=Category.TRACKING if tracking else Category.EXTERNAL_RESOURCE,
            message=(
                "Stylesheet imports a tracking endpoint."
                if tracking
                else "Stylesheet imports a remote stylesheet; loading it contacts "
                f"{check.host or 'the network'}."
            ),
            remediation=Remediation.AUTO if automatic else Remediation.REVIEW,
            confidence=0.9 if automatic else 0.6,
            target=resource.path,
            location=f"@import {url}",
            evidence=truncate(statement),
        )
        mutations.append(
            Mutation(
                finding=finding,
                kind="css_import",
                entry=resource.path,
                value=statement,
            )
        )

    return mutations


def apply_css_edits(
    text: str,
    declarations: list[tuple[str, str]],
    imports: list[str],
) -> str:
    """Remove the approved declarations/imports from a stylesheet.

    ``declarations`` holds ``(rule_prelude, declaration)`` pairs. A rule whose
    body becomes empty is dropped entirely, since it existed only to carry the
    removed declarations.
    """

    remaining = list(declarations)

    def replace_rule(match: re.Match[str]) -> str:
        prelude = match.group("pre").strip()
        body = match.group("body")
        kept: list[str] = []
        removed_any = False
        for declaration in body.split(";"):
            stripped = declaration.strip()
            if not stripped:
                kept.append(declaration)
                continue
            key = (prelude, stripped)
            if key in remaining:
                remaining.remove(key)
                removed_any = True
                continue
            kept.append(declaration)
        if not removed_any:
            return match.group(0)
        if not "".join(kept).strip():
            return ""
        return f"{match.group('pre')}{{{';'.join(kept)}}}"

    result = _RULE.sub(replace_rule, text)
    for statement in imports:
        result = result.replace(statement, "")
    return re.sub(r"\n{3,}", "\n\n", result)


def _declaration_problem(
    declaration: str, ctx: Context
) -> tuple[str, Severity, str, Remediation, Category] | None:
    lowered = declaration.lower()

    if _SCRIPT_CSS.search(declaration):
        return (
            "security.css_expression",
            Severity.HIGH,
            "CSS uses a legacy script expression that can execute code.",
            Remediation.AUTO,
            Category.STYLE,
        )

    if "javascript:" in lowered:
        return (
            "security.css_javascript_url",
            Severity.HIGH,
            "CSS embeds a javascript: URL.",
            Remediation.AUTO,
            Category.STYLE,
        )

    if ctx.has_brand(declaration):
        return (
            "branding.css_generated_content",
            Severity.HIGH,
            "Stylesheet injects third-party branding (generated content or background).",
            Remediation.AUTO,
            Category.BRANDING,
        )

    for match in _URL.finditer(declaration):
        check = ctx.classify(match.group("url"))
        if check is None:
            continue
        if check.is_tracker:
            return (
                "tracking.css_url",
                Severity.HIGH,
                f"Stylesheet loads a tracking host ({check.host}).",
                Remediation.AUTO,
                Category.TRACKING,
            )
        if check.is_promo:
            return (
                "branding.css_url",
                Severity.HIGH,
                f"Stylesheet loads promotional branding from {check.host}.",
                Remediation.AUTO,
                Category.BRANDING,
            )
        if check.is_remote or check.is_protocol_relative:
            automatic = ctx.config.remove_remote_resources
            return (
                "external.css_url",
                Severity.LOW,
                f"Stylesheet loads a remote resource ({check.host or 'remote host'}).",
                Remediation.AUTO if automatic else Remediation.REVIEW,
                Category.EXTERNAL_RESOURCE,
            )

    return None


def _first_url(statement: str) -> str | None:
    match = _URL.search(statement) or _QUOTED.search(statement)
    return match.group("url") if match else None
