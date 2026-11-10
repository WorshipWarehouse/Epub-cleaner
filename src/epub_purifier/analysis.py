"""Stage 2: inspection.

Builds the resource graph for the book, runs every detector once, then ranks the
results. Promotional *pages* are decided here too, because that judgement needs
the whole document and the outcome of the element-level edits.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from dataclasses import dataclass, field

from .archive import resolve_href
from .config import (
    CSS_MEDIA_TYPES,
    IMAGE_EXTENSIONS,
    MARKUP_MEDIA_TYPES,
    PROTECTED_DOC_HINTS,
    SVG_MEDIA_TYPES,
    PurifierConfig,
)
from .detectors import content, metadata, resources, security, styles
from .detectors.context import Context, Resource
from .markup import BLOCK_TAGS, local_name, parse_markup, text_of
from .models import AnalysisResult, Category, Finding, Mutation, Remediation, Severity
from .opf import NCX_MEDIA_TYPE
from .validation import ValidationResult

#: Extensions we always treat as documents, even when the OPF lies about them.
DOC_EXTENSIONS = frozenset({".xhtml", ".html", ".htm"})
IMAGE_SIZE_CACHE_LIMIT = 512 * 1024


@dataclass
class Analysis:
    """The context (live parse trees) plus the reportable result."""

    context: Context
    result: AnalysisResult = field(default_factory=AnalysisResult)

    @property
    def findings(self) -> list[Finding]:
        return self.result.findings

    @property
    def mutations(self) -> list[Mutation]:
        return self.result.mutations


def analyze(validation: ValidationResult, config: PurifierConfig | None = None) -> Analysis:
    config = config or PurifierConfig()
    ctx = build_context(validation, config)
    mutations: list[Mutation] = []

    for path in validation.archive.names:
        resource = ctx.resources.get(path)
        if resource is None:
            continue
        if resource.is_markup:
            if path in ctx.nav_paths:
                mutations.extend(content.detect_navigation_branding(ctx, resource))
            else:
                mutations.extend(content.detect_markup_branding(ctx, resource))
            mutations.extend(security.detect_active_content(ctx, resource))
            if path not in ctx.nav_paths:
                mutations.extend(security.detect_entity_declarations(ctx, resource))
            mutations.extend(resources.detect_remote_resources(ctx, resource))
            mutations.extend(resources.detect_data_uris(ctx, resource))
        elif resource.media_type in CSS_MEDIA_TYPES or resource.extension == ".css":
            mutations.extend(styles.detect_stylesheet(ctx, resource))
        elif resource.extension in IMAGE_EXTENSIONS:
            mutations.extend(content.detect_image_filename_branding(ctx, resource))

    mutations.extend(metadata.detect_package_metadata(ctx))
    mutations.extend(_promotional_pages(ctx, mutations))

    _assign_ids(validation.findings, mutations)
    findings = list(validation.findings)
    seen = {id(finding) for finding in findings}
    for mutation in mutations:
        if id(mutation.finding) not in seen:
            seen.add(id(mutation.finding))
            findings.append(mutation.finding)

    return Analysis(context=ctx, result=AnalysisResult(findings=findings, mutations=mutations))


# -- context -------------------------------------------------------------
def build_context(validation: ValidationResult, config: PurifierConfig) -> Context:
    package = validation.package
    archive = validation.archive
    manifest_media = {item.path: item.media_type for item in package.items() if item.path}

    ctx = Context(
        config=config,
        package=package,
        resources={},
        metadata=dict(validation.metadata),
    )

    for name in archive.names:
        if name == package.entry:
            continue
        media_type = manifest_media.get(name) or _guess_media_type(name)
        extension = posixpath.splitext(name)[1].lower()
        is_markup = (
            media_type in MARKUP_MEDIA_TYPES
            or media_type in SVG_MEDIA_TYPES
            or media_type == NCX_MEDIA_TYPE
            or extension in DOC_EXTENSIONS
            or extension in {".svg", ".ncx"}
        )
        is_css = media_type in CSS_MEDIA_TYPES or extension == ".css"

        if is_markup:
            try:
                text = archive.read_text(name)
            except Exception:  # noqa: BLE001, S112 - unreadable member is reported elsewhere
                continue
            soup, parser = parse_markup(text)
            ctx.resources[name] = Resource(
                path=name,
                media_type=media_type or "application/xhtml+xml",
                text=text,
                soup=soup,
                parser=parser,
                xml_declaration=text.lstrip().startswith("<?xml"),
            )
        elif is_css:
            try:
                text = archive.read_text(name)
            except Exception:  # noqa: BLE001, S112
                continue
            ctx.resources[name] = Resource(path=name, media_type="text/css", text=text)
        else:
            data = b""
            if extension in IMAGE_EXTENSIONS:
                try:
                    if archive.info(name).size <= IMAGE_SIZE_CACHE_LIMIT:
                        data = archive.read(name)
                except Exception:  # noqa: BLE001
                    data = b""
            ctx.resources[name] = Resource(path=name, media_type=media_type, data=data)

    ctx.spine_paths = [item.path for item in package.spine_items() if item.path]
    ctx.nav_paths = {
        path for path in (package.nav_entry(), package.ncx_entry()) if path and path in ctx.resources
    }
    ctx.nav_targets = _navigation_targets(ctx)
    ctx.nav_targets_clean = _navigation_targets(ctx, skip_branded=True)
    ctx.protected_paths = _protected_documents(ctx)
    ctx.referenced_images = _referenced_images(ctx)
    ctx.dimensions = _image_dimensions(ctx)
    return ctx


def _guess_media_type(name: str) -> str:
    extension = posixpath.splitext(name)[1].lower()
    return {
        ".xhtml": "application/xhtml+xml",
        ".html": "text/html",
        ".htm": "text/html",
        ".css": "text/css",
        ".ncx": "application/x-dtbncx+xml",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".opf": "application/oebps-package+xml",
    }.get(extension, "application/octet-stream")


def _navigation_targets(ctx: Context, *, skip_branded: bool = False) -> set[str]:
    """Documents linked from the table of contents.

    With ``skip_branded`` only links that are not themselves branding count, so
    a page reachable *solely* through a promotional TOC entry is not treated as
    user-facing content when promotional pages are judged.
    """

    targets: set[str] = set()
    for path in ctx.nav_paths:
        resource = ctx.resources.get(path)
        if resource is None or resource.soup is None:
            continue
        for element in resource.soup.find_all(True):
            value = element.get("href") or element.get("src")
            if not isinstance(value, str):
                continue
            if skip_branded and (ctx.has_brand(text_of(element)) or ctx.is_promo_url(value)):
                continue
            resolved = resolve_href(path, value)
            if resolved:
                targets.add(resolved)
    return targets


def _protected_documents(ctx: Context) -> set[str]:
    protected: set[str] = set()
    for path in ctx.resources:
        lowered = path.lower()
        if any(hint in lowered for hint in PROTECTED_DOC_HINTS):
            protected.add(path)
    for item in ctx.package.items():
        if item.path and any(hint in item.item_id.lower() for hint in PROTECTED_DOC_HINTS):
            protected.add(item.path)
    return protected


def _referenced_images(ctx: Context) -> set[str]:
    referenced: set[str] = set()
    for path, resource in ctx.resources.items():
        if resource.soup is None:
            continue
        for element in resource.soup.find_all(True):
            name = local_name(element.name)
            if name not in {"img", "image", "picture", "source"}:
                continue
            for attribute in ("src", "xlink:href", "srcset"):
                value = element.get(attribute)
                if not isinstance(value, str):
                    continue
                for candidate in value.split(","):
                    target = resolve_href(path, candidate.strip().split(" ")[0])
                    if target:
                        referenced.add(target)
    return referenced


def _image_dimensions(ctx: Context) -> dict[str, tuple[int, int] | None]:
    dimensions: dict[str, tuple[int, int] | None] = {}
    for path in ctx.referenced_images:
        resource = ctx.resources.get(path)
        if resource is None or not resource.data:
            continue
        dimensions[path] = resources.read_image_dimensions(resource.data)
    return dimensions


# -- promotional page detection -----------------------------------------
def _promotional_pages(ctx: Context, mutations: list[Mutation]) -> list[Mutation]:
    """Drop documents that exist only to advertise something.

    Deliberately strict: the page must be almost entirely made of branded text,
    must not be reachable from the table of contents, and must not look like a
    cover/title/copyright page. Legitimate short chapters therefore survive.
    """

    if not ctx.config.drop_promo_pages:
        return []

    out: list[Mutation] = []
    for entry, resource in ctx.resources.items():
        if resource.soup is None:
            continue
        if posixpath.splitext(entry)[1].lower() not in DOC_EXTENSIONS:
            continue
        if entry in ctx.nav_paths or entry in ctx.protected_paths:
            continue
        if entry in ctx.nav_targets_clean:
            continue

        branded: list[Finding] = []
        removed_text = 0
        for mutation in mutations:
            finding = mutation.finding
            if finding.target != entry:
                continue
            if finding.category is Category.BRANDING and finding.remediation is not Remediation.REPORT:
                branded.append(finding)
            element = mutation.element
            if (
                mutation.kind == "remove_element"
                and element is not None
                and getattr(element, "parent", None) is not None
            ):
                removed_text += len(text_of(element))

        if not branded or max(f.confidence for f in branded) < 0.85:
            continue

        body = resource.soup.find("body") or resource.soup
        total_text = text_of(body)
        projected = max(0, len(total_text) - removed_text)
        if projected > 200:
            continue

        removed_elements = [
            mutation.element
            for mutation in mutations
            if mutation.finding.target == entry
            and mutation.kind == "remove_element"
            and mutation.element is not None
        ]
        remaining_blocks = 0
        for element in body.find_all(True):
            if local_name(element.name) not in BLOCK_TAGS:
                continue
            if any(element is removed for removed in removed_elements):
                continue
            length = len(text_of(element))
            if length <= 40:
                continue
            contained = sum(
                len(text_of(removed))
                for removed in removed_elements
                if any(parent is element for parent in removed.parents)
            )
            if length - contained <= 40:
                continue
            remaining_blocks += 1
        if remaining_blocks > 2:
            continue

        strong_headings = [
            heading
            for heading in body.find_all(["h1", "h2", "h3"])
            if not ctx.has_brand(text_of(heading)) and len(text_of(heading).split()) > 2
        ]
        if strong_headings:
            continue

        finding = Finding(
            rule="branding.promo_page",
            severity=Severity.HIGH,
            category=Category.BRANDING,
            message="Page exists only to advertise a third-party site; the document is removed.",
            remediation=Remediation.AUTO,
            confidence=0.9,
            target=entry,
            drops_entry=entry,
            evidence=text_of(body)[:120] or posixpath.basename(entry),
        )
        out.append(Mutation(finding=finding, kind="drop_entry", entry=entry))
    return out


# -- ids -----------------------------------------------------------------
def _assign_ids(findings: list[Finding], mutations: list[Mutation]) -> None:
    counters: Counter[tuple[str, str, str, str]] = Counter()
    for finding in findings:
        _assign(finding, counters)
    for mutation in mutations:
        _assign(mutation.finding, counters)


def _assign(finding: Finding, counters: Counter) -> None:
    key = (finding.rule, finding.target or "", finding.location or "", finding.evidence or "")
    finding.assign_id(counters[key])
    counters[key] += 1


__all__ = ["Analysis", "analyze", "build_context"]
