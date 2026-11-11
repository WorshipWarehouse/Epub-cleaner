"""Stage 3: cleaning.

Applies exactly the mutations the caller approved, prunes the structures that
referenced the removed content (manifest, spine, guide, navigation), repackages
the container and verifies the result.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from bs4.element import Doctype, NavigableString, Tag

from .analysis import Analysis, analyze
from .archive import MIMETYPE, resolve_href
from .config import PurifierConfig
from .detectors.context import describe_element
from .detectors.styles import apply_css_edits
from .markup import (
    HTML5_DOCTYPE,
    STRUCTURAL_TAGS,
    XHTML_11_DOCTYPE,
    clean_string,
    is_empty_container,
    local_name,
    serialize_markup,
    text_of,
)
from .models import Mutation, Remediation, RemovedItem, Severity
from .opf import (
    has_valid_modified_timestamp,
    prune_empty_sections,
    prune_guide,
    prune_spine,
    remove_manifest_paths,
    set_modified_timestamp,
)
from .repackage import XHTML_EXTENSIONS, build_epub
from .validation import ValidationResult, utc_timestamp


@dataclass
class CleaningOutcome:
    """Result of the cleaning stage, before it is turned into a report."""

    data: bytes
    analysis: Analysis
    applied: list[RemovedItem] = field(default_factory=list)
    dropped: set[str] = field(default_factory=set)
    changed: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def clean_book(
    validation: ValidationResult,
    config: PurifierConfig | None = None,
    *,
    approved: set[str] | frozenset[str] = frozenset(),
    approve_all: bool = False,
) -> CleaningOutcome:
    """Apply approved mutations, prune references and rebuild the EPUB."""

    config = config or PurifierConfig()
    analysis = analyze(validation, config)
    ctx = analysis.context

    approved_ids = set(approved)
    applied: list[RemovedItem] = []
    dropped: set[str] = set()
    changed: set[str] = set()
    rewrites: dict[str, bytes] = {}

    # -- archive-level removals decided during validation -----------------
    for finding in validation.findings:
        if not finding.drops_entry:
            continue
        approved_now = finding.remediation is Remediation.AUTO or (
            finding.remediation is Remediation.REVIEW
            and (approve_all or finding.finding_id in approved_ids)
        )
        if not approved_now:
            continue
        dropped.add(finding.drops_entry)
        applied.append(
            RemovedItem(
                finding_id=finding.finding_id,
                action="drop_entry",
                target=finding.drops_entry,
                detail=finding.message,
                severity=finding.severity,
            )
        )

    # -- group approved mutations by entry --------------------------------
    by_entry: dict[str, list[Mutation]] = {}
    for mutation in analysis.mutations:
        finding = mutation.finding
        approved_now = finding.remediation is Remediation.AUTO or (
            finding.remediation is Remediation.REVIEW
            and (approve_all or finding.finding_id in approved_ids)
        )
        if not approved_now or mutation.kind == "report_only":
            continue
        if mutation.kind == "drop_entry":
            if mutation.entry:
                dropped.add(mutation.entry)
                applied.append(
                    RemovedItem(
                        finding_id=finding.finding_id,
                        action="drop_entry",
                        target=mutation.entry,
                        detail=finding.message,
                        severity=finding.severity,
                    )
                )
            continue
        by_entry.setdefault(mutation.entry or "", []).append(mutation)

    # -- package-level edits ---------------------------------------------
    package = validation.package
    package_findings = [
        f for f in analysis.findings if f.target == package.entry and f.remediation is not Remediation.AUTO
    ]
    if package_findings:
        ctx.warnings.append(
            f"{len(package_findings)} package metadata finding(s) were left in place for review."
        )
    ids_before = {item.item_id for item in package.items()}
    package_mutations = by_entry.pop(package.entry, [])
    for mutation in package_mutations:
        record = _removed(mutation)
        if _apply(package.soup, mutation, config):
            changed.add(package.entry)
            applied.append(record)
    ids_after = {item.item_id for item in package.items()}
    package_removed_ids = ids_before - ids_after

    # -- element-level edits ---------------------------------------------
    touched: list[tuple[str, Tag | None]] = []
    for entry, mutations in by_entry.items():
        resource = ctx.resources.get(entry)
        if resource is None or resource.soup is None:
            # Stylesheets are rebuilt from the approved declaration edits.
            declarations: list[tuple[str, str]] = []
            imports: list[str] = []
            for mutation in mutations:
                if mutation.kind == "css_declaration":
                    declarations.append((mutation.attribute or "", mutation.value or ""))
                elif mutation.kind == "css_import":
                    imports.append(mutation.value or "")
                else:
                    continue
                record = _removed(mutation)
                changed.add(entry)
                applied.append(record)
            if (declarations or imports) and resource is not None and resource.text is not None:
                rewrites[entry] = apply_css_edits(
                    resource.text, declarations, imports
                ).encode("utf-8")
            continue
        for mutation in mutations:
            if mutation.kind in {"css_declaration", "css_import"}:
                continue
            record = _removed(mutation)
            if mutation.kind == "strip_doctype":
                # Entity declarations are already dropped at parse time; rebuilding
                # without the doctype is what makes the removal effective.
                _strip_doctype(resource.soup)
                changed.add(entry)
                applied.append(record)
                continue
            parent_before = (
                getattr(mutation.element, "parent", None)
                if mutation.kind == "remove_element"
                else None
            )
            if _apply(resource.soup, mutation, config):
                changed.add(entry)
                applied.append(record)
                if parent_before is not None:
                    touched.append((entry, parent_before))

    _prune_orphan_wrappers(touched, changed)

    # -- references to dropped documents ---------------------------------
    if dropped:
        for entry, resource in ctx.resources.items():
            if resource.soup is None or entry in dropped:
                continue
            if entry in ctx.nav_paths and _prune_nav(resource.soup, entry, dropped):
                changed.add(entry)

    removed_ids = set(package_removed_ids)
    if dropped:
        removed_ids.update(remove_manifest_paths(package, dropped))
        prune_guide(package, dropped)
        changed.add(package.entry)
    if removed_ids and prune_spine(package, removed_ids):
        changed.add(package.entry)
    prune_empty_sections(package)

    if (
        config.update_modified_timestamp
        and (changed or not has_valid_modified_timestamp(package))
        and set_modified_timestamp(package, utc_timestamp())
    ):
        changed.add(package.entry)

    # -- serialize --------------------------------------------------------
    is_epub2 = validation.version.startswith("2.")
    doctype = XHTML_11_DOCTYPE if is_epub2 else HTML5_DOCTYPE
    if is_epub2:
        # XHTML 1.1 requires a doctype even when a document needed no content
        # edit, so rebuild every content document rather than only changed ones.
        # Include extension-selected documents the verifier treats as XHTML even
        # when their manifest media type is unrecognised.
        content_entries = set(validation.markup_entries)
        content_entries.update(
            item.path
            for item in validation.package.items()
            if item.path and posixpath.splitext(item.path)[1].lower() in XHTML_EXTENSIONS
        )
        for entry in content_entries:
            resource = ctx.resources.get(entry)
            if (
                resource is not None
                and resource.soup is not None
                and resource.soup.find("html") is not None
            ):
                changed.add(entry)

    payload: dict[str, bytes] = {}
    for entry in changed:
        resource = ctx.resources.get(entry)
        if resource is not None and resource.soup is not None:
            payload[entry] = serialize_markup(
                resource.soup,
                resource.parser,
                xml_declaration=resource.xml_declaration,
                doctype=doctype,
            )
        elif entry in rewrites:
            payload[entry] = rewrites[entry]
    if package.entry in changed:
        payload[package.entry] = serialize_markup(
            package.soup,
            "xml" if package.soup.find() is not None else "html",
            xml_declaration=True,
        )

    # -- assemble ---------------------------------------------------------
    entries: list[tuple[str, bytes]] = [
        ("META-INF/container.xml", validation.archive.read("META-INF/container.xml"))
    ]
    for name in validation.archive.names:
        if name in dropped or name == "mimetype" or name == "META-INF/container.xml":
            continue
        if name in payload:
            data = payload[name]
        else:
            try:
                data = validation.archive.read(name)
            except Exception as exc:  # noqa: BLE001 - unreadable member: keep it out
                ctx.warnings.append(f"Skipped unreadable member {name!r}: {exc}")
                continue
        entries.append((name, data))

    data = build_epub(entries)
    return CleaningOutcome(
        data=data,
        analysis=analysis,
        applied=applied,
        dropped=dropped,
        changed=changed,
        warnings=list(ctx.warnings),
    )


def _apply(soup, mutation: Mutation, config: PurifierConfig) -> bool:
    element = mutation.element
    if mutation.kind == "remove_element":
        if element is None or getattr(element, "parent", None) is None:
            return False
        element.decompose()
        return True

    if mutation.kind == "strip_attribute":
        if element is None or not getattr(element, "attrs", None):
            return False
        attribute = mutation.attribute or ""
        if attribute and attribute in element.attrs:
            del element.attrs[attribute]
            return True
        return False

    if mutation.kind == "clean_text":
        if isinstance(element, NavigableString):
            cleaned = clean_string(str(element), config.brand_regex)
            if cleaned != str(element):
                element.replace_with(cleaned)
                return True
            return False
        if isinstance(element, Tag):
            original = text_of(element)
            cleaned = clean_string(original, config.brand_regex)
            if cleaned != original:
                element.clear()
                if cleaned:
                    element.append(cleaned)
                return True
        return False

    if mutation.kind == "clean_element_text":
        if not isinstance(element, Tag):
            return False
        original = text_of(element)
        cleaned = clean_string(original, config.brand_regex)
        if cleaned == original:
            return False
        element.clear()
        if cleaned:
            element.append(cleaned)
        return True

    return False


def _strip_doctype(soup) -> bool:
    removed = False
    for node in soup.find_all(string=True):
        if isinstance(node, Doctype):
            node.extract()
            removed = True
    return removed


def _prune_orphan_wrappers(touched: list[tuple[str, Tag | None]], changed: set[str]) -> None:
    """Remove wrappers we emptied, without touching meaningful containers."""

    for entry, parent in touched:
        current = parent
        depth = 0
        while isinstance(current, Tag) and depth < 4:
            if local_name(current.name) in STRUCTURAL_TAGS:
                break
            if not is_empty_container(current):
                break
            grandparent = current.parent
            current.decompose()
            changed.add(entry)
            current = grandparent if isinstance(grandparent, Tag) else None
            depth += 1


def _prune_nav(soup, entry: str, dropped: set[str]) -> bool:
    """Remove TOC/NCX entries that point at documents we deleted."""

    changed = False
    for anchor in list(soup.find_all("a")):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        target = resolve_href(entry, href)
        if target in dropped:
            container = anchor.find_parent("li") or anchor
            container.decompose()
            changed = True

    for element in list(soup.find_all(True)):
        if local_name(element.name) != "navpoint":
            continue
        content_tag = None
        for child in element.find_all(True):
            if local_name(child.name) == "content":
                content_tag = child
                break
        src = content_tag.get("src") if content_tag is not None else None
        target = resolve_href(entry, src) if isinstance(src, str) else None
        if target in dropped:
            element.decompose()
            changed = True
    return changed


def _removed(mutation: Mutation) -> RemovedItem:
    finding = mutation.finding
    if mutation.kind == "remove_element" and mutation.element is not None:
        detail = text_of(mutation.element)[:160] or describe_element(mutation.element)
    elif mutation.kind in {"css_declaration", "css_import"}:
        detail = f"removed: {mutation.value or ''}"[:200]
    elif mutation.kind == "strip_attribute":
        detail = f"{mutation.attribute} removed from {describe_element(mutation.element)}"
    elif mutation.kind == "rewrite_entry":
        detail = "resource rewritten"
    else:
        detail = finding.message
    return RemovedItem(
        finding_id=finding.finding_id,
        action=mutation.kind,
        target=mutation.entry or finding.target,
        detail=detail or finding.message,
        severity=finding.severity,
    )


def mime_type_of(entries: list[tuple[str, bytes]]) -> str:
    for name, data in entries:
        if name == "mimetype":
            return data.decode("ascii", errors="replace")
    return MIMETYPE


__all__ = ["CleaningOutcome", "clean_book", "mime_type_of", "Severity", "posixpath"]
