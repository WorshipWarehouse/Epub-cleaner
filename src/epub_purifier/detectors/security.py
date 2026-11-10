"""Active-content and scripting detection.

Nothing here executes anything: markup is only ever parsed as data. Findings are
ranked so that genuine execution vectors (scripts, event handlers, JavaScript
URLs) sit above EPUB 3 features that are merely unusual.
"""

from __future__ import annotations

import re

from bs4.element import Tag

from ..markup import attribute_text, local_name, text_of
from ..models import Category, Finding, Mutation, Remediation, Severity
from .context import Context, Resource, describe_element, truncate

EVENT_HANDLER = re.compile(r"^on[a-z]{2,}$", re.IGNORECASE)
SCRIPT_SCHEME = re.compile(r"^\s*(?:javascript|vbscript|livescript)\s*:", re.IGNORECASE)
DANGEROUS_DATA = re.compile(
    r"^\s*data:\s*(?:text/html|application/xhtml\+xml|text/javascript|application/javascript|image/svg\+xml)",
    re.IGNORECASE,
)
CSS_SCRIPT = re.compile(r"(?:expression\s*\(|-moz-binding|behavior\s*:)", re.IGNORECASE)

#: Attributes that can carry a navigable or loadable URL.
URL_ATTRIBUTES = ("href", "src", "xlink:href", "action", "formaction", "poster", "data", "background")

#: Elements that embed or execute active content.
DANGEROUS_ELEMENTS = {
    "script": (Severity.CRITICAL, "Script element embedded in the book."),
    "iframe": (Severity.HIGH, "Inline frame can load remote content."),
    "frame": (Severity.HIGH, "Frame element can load remote content."),
    "frameset": (Severity.HIGH, "Frameset element can load remote content."),
    "object": (Severity.HIGH, "Embedded object may load or run external content."),
    "embed": (Severity.HIGH, "Embedded plugin content."),
    "applet": (Severity.HIGH, "Java applet element."),
    "portal": (Severity.MEDIUM, "Portal element can preload remote pages."),
    "form": (Severity.MEDIUM, "Form element can submit data to a remote endpoint."),
    "input": (Severity.LOW, "Form control inside a book."),
    "button": (Severity.LOW, "Form control inside a book."),
    "select": (Severity.LOW, "Form control inside a book."),
    "textarea": (Severity.LOW, "Form control inside a book."),
    "base": (Severity.MEDIUM, "Base element rewrites how every other link resolves."),
}


def detect_active_content(ctx: Context, resource: Resource) -> list[Mutation]:
    soup = resource.soup
    if soup is None:
        return []

    mutations: list[Mutation] = []

    for element in soup.find_all(True):
        name = local_name(element.name)
        severity_message = DANGEROUS_ELEMENTS.get(name)
        if severity_message is not None:
            severity, message = severity_message
            if name == "script":
                evidence = truncate(element.get("src") or text_of(element))
            elif name in {"base", "form"}:
                evidence = truncate(
                    " ".join(f"{k}={v}" for k, v in element.attrs.items() if isinstance(v, str))
                )
            else:
                evidence = None
            mutations.append(
                _removal(
                    resource.path,
                    element,
                    rule=f"security.{name}",
                    severity=severity,
                    category=Category.SCRIPT
                    if name == "script"
                    else Category.EMBEDDED_OBJECT,
                    message=message,
                    evidence=evidence,
                )
            )

        if name == "meta":
            http_equiv = str(element.get("http-equiv") or "").lower()
            if http_equiv == "refresh":
                mutations.append(
                    _removal(
                        resource.path,
                        element,
                        rule="security.meta_refresh",
                        severity=Severity.HIGH,
                        category=Category.SCRIPT,
                        message="Meta refresh redirects the reader to another location.",
                        evidence=truncate(str(element.get("content") or "")),
                    )
                )

        if name == "link":
            rel = " ".join(str(element.get("rel") or "").lower().split())
            if "import" in rel:
                mutations.append(
                    _removal(
                        resource.path,
                        element,
                        rule="security.html_import",
                        severity=Severity.HIGH,
                        category=Category.SCRIPT,
                        message="HTML import loads an external document.",
                        evidence=truncate(str(element.get("href") or "")),
                    )
                )

        # -- attribute level -------------------------------------------
        for attribute, value in list(element.attrs.items()):
            if not isinstance(value, str):
                continue
            lowered_attr = attribute.lower()

            if EVENT_HANDLER.match(lowered_attr):
                finding = Finding(
                    rule="security.event_handler",
                    severity=Severity.HIGH,
                    category=Category.SCRIPT,
                    message=f"Inline event handler '{attribute}' can run code when triggered.",
                    remediation=Remediation.AUTO,
                    confidence=0.95,
                    target=resource.path,
                    location=describe_element(element),
                    evidence=truncate(f"{attribute}={value}"),
                )
                mutations.append(
                    Mutation(
                        finding=finding,
                        kind="strip_attribute",
                        element=element,
                        attribute=attribute,
                        entry=resource.path,
                    )
                )
                continue

            if lowered_attr in URL_ATTRIBUTES:
                if SCRIPT_SCHEME.match(value):
                    finding = Finding(
                        rule="security.script_url",
                        severity=Severity.HIGH,
                        category=Category.SCRIPT,
                        message="URL uses a script pseudo-scheme.",
                        remediation=Remediation.AUTO,
                        confidence=0.97,
                        target=resource.path,
                        location=describe_element(element),
                        evidence=truncate(value),
                    )
                    mutations.append(
                        Mutation(
                            finding=finding,
                            kind="strip_attribute",
                            element=element,
                            attribute=attribute,
                            entry=resource.path,
                        )
                    )
                elif DANGEROUS_DATA.match(value):
                    finding = Finding(
                        rule="security.data_uri_active",
                        severity=Severity.HIGH,
                        category=Category.SCRIPT,
                        message="Data URI carries active content (HTML/JavaScript/SVG).",
                        remediation=Remediation.AUTO,
                        confidence=0.95,
                        target=resource.path,
                        location=describe_element(element),
                        evidence=truncate(value, 80),
                    )
                    mutations.append(
                        Mutation(
                            finding=finding,
                            kind="remove_element",
                            element=element,
                            attribute=attribute,
                            entry=resource.path,
                        )
                    )

            if lowered_attr == "style" and CSS_SCRIPT.search(value):
                finding = Finding(
                    rule="security.css_expression",
                    severity=Severity.HIGH,
                    category=Category.STYLE,
                    message="Inline CSS uses a legacy script expression.",
                    remediation=Remediation.AUTO,
                    confidence=0.95,
                    target=resource.path,
                    location=describe_element(element),
                    evidence=truncate(value),
                )
                mutations.append(
                    Mutation(
                        finding=finding,
                        kind="strip_attribute",
                        element=element,
                        attribute=attribute,
                        entry=resource.path,
                    )
                )

    # -- SVG-specific ---------------------------------------------------
    for element in soup.find_all(True):
        name = local_name(element.name)
        if name == "foreignobject":
            finding = Finding(
                rule="security.svg_foreign_object",
                severity=Severity.LOW,
                category=Category.EMBEDDED_OBJECT,
                message="SVG embeds foreign markup; unusual for a book illustration.",
                remediation=Remediation.REPORT,
                confidence=0.5,
                target=resource.path,
                location=describe_element(element),
            )
            mutations.append(Mutation(finding=finding, kind="report_only", entry=resource.path))
        if name == "use":
            href = element.get("xlink:href") or element.get("href") or ""
            if isinstance(href, str) and href.strip().startswith(("http://", "https://", "//")):
                finding = Finding(
                    rule="security.svg_external_use",
                    severity=Severity.HIGH,
                    category=Category.EXTERNAL_RESOURCE,
                    message="SVG <use> references a remote document.",
                    remediation=Remediation.AUTO,
                    confidence=0.9,
                    target=resource.path,
                    location=describe_element(element),
                    evidence=truncate(href),
                )
                mutations.append(
                    Mutation(finding=finding, kind="remove_element", element=element, entry=resource.path)
                )

    return mutations


def detect_entity_declarations(ctx: Context, resource: Resource) -> list[Mutation]:
    """Flag and strip XML entity declarations (XXE-style payloads)."""

    from ..markup import has_entity_declarations

    if not resource.text or not has_entity_declarations(resource.text):
        return []
    finding = Finding(
        rule="security.xml_entities",
        severity=Severity.HIGH,
        category=Category.SCRIPT,
        message=(
            "Document declares custom XML entities; this is the classic XXE delivery "
            "pattern and has no purpose in a book."
        ),
        remediation=Remediation.AUTO,
        confidence=0.9,
        target=resource.path,
        location="doctype",
    )
    return [Mutation(finding=finding, kind="strip_doctype", entry=resource.path)]


def _removal(
    entry: str,
    element: Tag,
    *,
    rule: str,
    severity: Severity,
    category: Category,
    message: str,
    evidence: str | None = None,
) -> Mutation:
    finding = Finding(
        rule=rule,
        severity=severity,
        category=category,
        message=message,
        remediation=Remediation.AUTO,
        confidence=0.95,
        target=entry,
        location=describe_element(element),
        evidence=evidence or truncate(attribute_text(element)) or None,
    )
    return Mutation(finding=finding, kind="remove_element", element=element, entry=entry)
