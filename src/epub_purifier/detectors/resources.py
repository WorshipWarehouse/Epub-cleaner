"""Remote resource, tracking-pixel and data-URI detection."""

from __future__ import annotations

import re
from io import BytesIO

from ..archive import resolve_href
from ..markup import local_name, parse_int
from ..models import Category, Finding, Mutation, Remediation, Severity
from .context import Context, Resource, describe_element, truncate

#: Query parameters that exist to identify a reader rather than load content.
TRACKING_QUERY = re.compile(
    r"(?:utm_[a-z]+|fbclid|gclid|dclid|msclkid|mc_eid|mc_cid|yclid|igshid|"
    r"pixel|beacon|track(?:ing|er)?|spacer|1x1|ref(?:errer)?)\s*=",
    re.IGNORECASE,
)

#: File names used by invisible beacons.
PIXEL_FILENAME = re.compile(
    r"(?:^|[/_.-])(?:pixel|spacer|beacon|tracker|impression|1x1|blank|clear|transparent)"
    r"[^/]*\.(?:gif|png|jpe?g)",
    re.IGNORECASE,
)

URL_ATTRIBUTES = ("src", "href", "poster", "data", "xlink:href", "background")

#: Elements the security stage deletes outright; no need to report twice.
OWNED_BY_SECURITY = {"script", "iframe", "frame", "frameset", "object", "embed", "applet"}

#: Elements that load a resource as part of rendering.
MEDIA_ELEMENTS = {"img", "image", "audio", "video", "source", "track", "picture"}

REMOTE_SCHEMES_NOTE = "Remote resources let the publisher track readers and break offline reading."


def detect_remote_resources(ctx: Context, resource: Resource) -> list[Mutation]:
    soup = resource.soup
    if soup is None:
        return []

    mutations: list[Mutation] = []

    for element in soup.find_all(True):
        name = local_name(element.name)
        if name in OWNED_BY_SECURITY:
            continue

        for attribute in URL_ATTRIBUTES:
            value = element.get(attribute)
            if not isinstance(value, str) or not value.strip():
                continue
            check = ctx.classify(value)
            if check is None or check.is_script or check.is_data:
                continue
            if check.is_promo:
                # Branding stage owns promotional URLs.
                continue

            if name == "a" and check.is_tracker:
                mutations.append(
                    _strip_link(resource.path, element, attribute, value, check.host)
                )
                continue
            if name == "a":
                # Ordinary outbound citations are legitimate book content.
                continue

            if check.is_tracker:
                mutations.append(
                    _removal(
                        resource.path,
                        element,
                        rule="tracking.tracker_resource",
                        severity=Severity.HIGH,
                        category=Category.TRACKING,
                        message=f"Resource loads from tracking/analytics host {check.host}.",
                        confidence=0.95,
                        evidence=truncate(value),
                    )
                )
                continue

            if _is_tracking_pixel(ctx, resource, element, check):
                mutations.append(
                    _removal(
                        resource.path,
                        element,
                        rule="tracking.pixel",
                        severity=Severity.HIGH,
                        category=Category.TRACKING,
                        message="Invisible tracking pixel (1x1 image or beacon filename).",
                        confidence=0.9,
                        evidence=truncate(value),
                    )
                )
                continue

            if check.is_protocol_relative:
                mutations.append(
                    _review_remote(
                        ctx,
                        resource.path,
                        element,
                        attribute,
                        value,
                        severity=Severity.MEDIUM,
                        rule="external.protocol_relative",
                        message="Protocol-relative URL loads over the network.",
                    )
                )
                continue

            if check.is_remote:
                # Stylesheets and fonts are treated as rendering dependencies.
                if name == "link" or attribute in {"background", "poster"}:
                    mutations.append(
                        _review_remote(
                            ctx,
                            resource.path,
                            element,
                            attribute,
                            value,
                            severity=Severity.MEDIUM,
                            rule="external.remote_asset",
                            message=f"Rendering asset loaded from {check.host}." if check.host else "Rendering asset loaded from the network.",
                        )
                    )
                else:
                    mutations.append(
                        _review_remote(
                            ctx,
                            resource.path,
                            element,
                            attribute,
                            value,
                            severity=Severity.LOW,
                            rule="external.remote_resource",
                            message=f"Figure/media loaded from {check.host}." if check.host else "Figure/media loaded from the network.",
                        )
                    )

        style = element.get("style")
        if isinstance(style, str) and "url(" in style.lower():
            for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", style, re.IGNORECASE):
                check = ctx.classify(url)
                if check and (check.is_remote or check.is_protocol_relative) and not check.is_promo:
                    if check.is_tracker:
                        mutations.append(
                            _strip_style(resource.path, element, style, check.host)
                        )
                    else:
                        mutations.append(
                            _review_remote(
                                ctx,
                                resource.path,
                                element,
                                "style",
                                style,
                                severity=Severity.LOW,
                                rule="external.inline_style_url",
                                message="Inline style loads a remote resource.",
                            )
                        )

    return mutations


def detect_data_uris(ctx: Context, resource: Resource) -> list[Mutation]:
    """Data URIs are legal but unusual; report the benign ones for awareness."""

    soup = resource.soup
    if soup is None:
        return []
    mutations: list[Mutation] = []
    for element in soup.find_all(True):
        for attribute in URL_ATTRIBUTES:
            value = element.get(attribute)
            if not isinstance(value, str):
                continue
            check = ctx.classify(value)
            if check is None or not check.is_data:
                continue
            from .security import DANGEROUS_DATA

            if DANGEROUS_DATA.match(value):
                continue  # handled (and removed) by the security stage
            finding = Finding(
                rule="content.data_uri",
                severity=Severity.LOW,
                category=Category.EXTERNAL_RESOURCE,
                message="Inline data URI resource; not dangerous by itself.",
                remediation=Remediation.REPORT,
                confidence=0.5,
                target=resource.path,
                location=describe_element(element),
                evidence=truncate(value, 60),
            )
            mutations.append(Mutation(finding=finding, kind="report_only", entry=resource.path))
    return mutations


def read_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return image dimensions via Pillow, never raising on hostile input."""

    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            return image.size
    except Exception:  # noqa: BLE001 - Pillow raises a wide range of errors on bad input
        return None


# -- helpers -------------------------------------------------------------
def _is_tracking_pixel(ctx: Context, resource: Resource, element, check) -> bool:
    name = local_name(element.name)
    if name not in {"img", "image"}:
        return False

    width = parse_int(element.get("width"))
    height = parse_int(element.get("height"))
    if width == 1 and height == 1:
        return True

    style = str(element.get("style") or "")
    if re.search(r"width\s*:\s*1(?:\.0)?px", style, re.I) and re.search(
        r"height\s*:\s*1(?:\.0)?px", style, re.I
    ):
        return True

    if PIXEL_FILENAME.search(check.path or check.raw):
        return True
    if TRACKING_QUERY.search(check.raw):
        return True

    if check.is_local:
        target = resolve_href(resource.path, check.raw)
        if target is not None:
            dimensions = ctx.dimensions.get(target)
            if dimensions is not None and dimensions[0] * dimensions[1] <= 4:
                return True
    return False


def _removal(
    entry: str,
    element,
    *,
    rule: str,
    severity: Severity,
    category: Category,
    message: str,
    confidence: float,
    evidence: str | None,
) -> Mutation:
    finding = Finding(
        rule=rule,
        severity=severity,
        category=category,
        message=message,
        remediation=Remediation.AUTO,
        confidence=confidence,
        target=entry,
        location=describe_element(element),
        evidence=evidence,
    )
    return Mutation(finding=finding, kind="remove_element", element=element, entry=entry)


def _review_remote(
    ctx: Context,
    entry: str,
    element,
    attribute: str,
    value: str,
    *,
    severity: Severity,
    rule: str,
    message: str,
) -> Mutation:
    """Remove remote content automatically only when the user asked for it."""

    auto = ctx.config.remove_remote_resources
    finding = Finding(
        rule=rule,
        severity=severity,
        category=Category.EXTERNAL_RESOURCE,
        message=message + (" Removed as requested." if auto else " " + REMOTE_SCHEMES_NOTE),
        remediation=Remediation.AUTO if auto else Remediation.REVIEW,
        confidence=0.85 if auto else 0.55,
        target=entry,
        location=describe_element(element),
        evidence=truncate(value),
    )
    kind = "remove_element" if attribute != "style" else "strip_attribute"
    return Mutation(
        finding=finding,
        kind=kind,
        element=element,
        attribute=None if kind == "remove_element" else attribute,
        entry=entry,
    )


def _strip_link(entry: str, element, attribute: str, value: str, host: str | None) -> Mutation:
    finding = Finding(
        rule="tracking.link",
        severity=Severity.MEDIUM,
        category=Category.TRACKING,
        message=f"Hyperlink points at tracking host {host or 'unknown'}; target removed.",
        remediation=Remediation.AUTO,
        confidence=0.9,
        target=entry,
        location=describe_element(element),
        evidence=truncate(value),
    )
    return Mutation(
        finding=finding, kind="strip_attribute", element=element, attribute=attribute, entry=entry
    )


def _strip_style(entry: str, element, style: str, host: str | None) -> Mutation:
    finding = Finding(
        rule="tracking.inline_style",
        severity=Severity.HIGH,
        category=Category.TRACKING,
        message=f"Inline style loads a tracking host ({host or 'unknown'}).",
        remediation=Remediation.AUTO,
        confidence=0.9,
        target=entry,
        location=describe_element(element),
        evidence=truncate(style),
    )
    return Mutation(
        finding=finding, kind="strip_attribute", element=element, attribute="style", entry=entry
    )
