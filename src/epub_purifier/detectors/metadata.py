"""Package metadata, guide, manifest and navigation detection."""

from __future__ import annotations

from bs4.element import Tag

from ..markup import attribute_text, local_name, text_of
from ..models import Category, Finding, Mutation, Remediation, Severity
from ..opf import iter_local
from .context import Context, truncate

#: Metadata elements whose value is a URL we should classify.
URL_METADATA = {"identifier", "source", "relation", "references"}


def detect_package_metadata(ctx: Context) -> list[Mutation]:
    package = ctx.package
    mutations: list[Mutation] = []
    metadata = package.metadata
    if metadata is None:
        return mutations

    for element in list(metadata.find_all(True)):
        name = local_name(element.name)
        if name in {"meta", "link"}:
            values = str(element.get("content") or "") + " " + str(element.get("href") or "")
        else:
            values = text_of(element)
        attributes = attribute_text(element)
        combined = f"{values} {attributes}"
        if not ctx.has_brand(combined):
            continue

        location = f"metadata > {name}"
        if name == "title" and not ctx.has_brand(attributes):
            # Never delete the book's title; strip the inserted branding instead.
            finding = Finding(
                rule="branding.metadata_title",
                severity=Severity.MEDIUM,
                category=Category.BRANDING,
                message="Book title carries third-party branding; branding text removed.",
                remediation=Remediation.AUTO,
                confidence=0.9,
                target=package.entry,
                location=location,
                evidence=truncate(values),
            )
            mutations.append(
                Mutation(finding=finding, kind="clean_element_text", element=element, entry=package.entry)
            )
            continue

        finding = Finding(
            rule="branding.metadata",
            severity=Severity.HIGH,
            category=Category.METADATA,
            message=f"Package metadata '{name}' was injected by a third party; removed.",
            remediation=Remediation.AUTO,
            confidence=0.95,
            target=package.entry,
            location=location,
            evidence=truncate(values),
        )
        mutations.append(
            Mutation(finding=finding, kind="remove_element", element=element, entry=package.entry)
        )

    # Guide references invented by a packager.
    guide = package.guide
    if guide is not None:
        for reference in list(iter_local(guide, "reference")):
            href = str(reference.get("href") or "")
            title = str(reference.get("title") or "")
            reference_type = str(reference.get("type") or "")
            if not (ctx.has_brand(href) or ctx.has_brand(title) or ctx.is_promo_url(href)):
                continue
            finding = Finding(
                rule="branding.guide_reference",
                severity=Severity.HIGH,
                category=Category.NAVIGATION,
                message="Guide reference points at third-party promotional content; removed.",
                remediation=Remediation.AUTO,
                confidence=0.93,
                target=package.entry,
                location=f"guide > reference[{reference_type}]",
                evidence=truncate(f"{title} -> {href}"),
            )
            mutations.append(
                Mutation(
                    finding=finding, kind="remove_element", element=reference, entry=package.entry
                )
            )

    # Manifest entries that point outside the container (invalid EPUB, common in
    # converted web pages that pulled assets from a site).
    for item in package.items():
        if not item.href or "://" not in item.href:
            continue
        check = project_url(ctx, item.href)
        if check is None:
            continue
        severity = Severity.HIGH if (check.is_tracker or check.is_promo) else Severity.MEDIUM
        finding = Finding(
            rule="external.manifest_item",
            severity=severity,
            category=Category.EXTERNAL_RESOURCE,
            message=(
                f"Manifest item '{item.item_id}' points outside the book "
                f"({check.host or 'remote host'}); entry removed."
            ),
            remediation=Remediation.REVIEW,
            confidence=0.8,
            target=package.entry,
            location=f"manifest > item[{item.item_id}]",
            evidence=truncate(item.href),
        )
        mutations.append(
            Mutation(
                finding=finding, kind="remove_element", element=item.element, entry=package.entry
            )
        )

    return mutations


def project_url(ctx: Context, value: str):
    return ctx.classify(value)


def detect_navigation_metadata(ctx: Context, resource_path: str) -> list[Mutation]:
    """Branded entries in the nav document or NCX, plus page-list leftovers."""

    from .content import detect_navigation_branding

    resource = ctx.resources.get(resource_path)
    if resource is None:
        return []
    return detect_navigation_branding(ctx, resource)


def _is_metadata_container(element: Tag) -> bool:
    return local_name(element.name) in {"metadata", "package"}


__all__ = ["detect_package_metadata", "detect_navigation_metadata"]
