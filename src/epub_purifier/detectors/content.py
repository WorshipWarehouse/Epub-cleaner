"""Branding and promotional-content detection inside XHTML documents.

The rules are deliberately conservative: a brand name alone is never enough to
delete text. Confidence comes from *context* — a short standalone paragraph in a
footer, a link whose text is nothing but the brand, or a heading wedged after the
last chapter. Quotations, citations, footnotes and long prose that merely
mentions the same site are reported, not removed.
"""

from __future__ import annotations

import posixpath

from bs4.element import NavigableString, Tag

from ..markup import (
    BLOCK_TAGS,
    attribute_text,
    is_inside,
    local_name,
    nearest_block,
    normalize_space,
    text_of,
)
from ..models import Category, Finding, Mutation, Remediation, Severity
from .context import (
    Context,
    Resource,
    count_words,
    describe_element,
    truncate,
)

#: Elements we are willing to delete when they only carry a promotion.
REMOVABLE_CONTAINERS = BLOCK_TAGS | {"span", "small", "em", "strong", "center", "font", "sup", "sub"}


def detect_markup_branding(ctx: Context, resource: Resource) -> list[Mutation]:
    soup = resource.soup
    if soup is None:
        return []

    mutations: list[Mutation] = []
    scheduled: set[int] = set()  # id() of elements already slated for removal

    # -- 1. text nodes containing the brand -------------------------------
    for node in soup.find_all(string=True):
        if not isinstance(node, NavigableString):
            continue
        raw = str(node)
        if not raw.strip():
            continue
        hits = ctx.brand_hits(raw)
        if not hits:
            continue

        parent = node.parent if isinstance(node.parent, Tag) else None
        if parent is None:
            continue

        # CSS and JavaScript bodies are not reader-visible text: styles.py and
        # security.py report them once, on their own terms. Without this the
        # same declaration is also scored as a promotional text block.
        if any(
            local_name(ancestor.name) in {"style", "script"}
            for ancestor in (parent, *_ancestors(parent))
        ):
            continue

        # Titles and headings are cleaned in place, never deleted.
        if local_name(parent.name) in {"title"}:
            mutation = _clean_text(node, resource.path)
            if mutation:
                mutations.append(mutation)
            continue

        container = nearest_block(parent) or parent
        if local_name(container.name) in {"html", "body"}:
            continue
        if id(container) in scheduled:
            continue

        confidence, _reasons = _score_brand_block(container, hits, ctx)
        evidence = truncate(text_of(container))
        location = describe_element(container)
        if confidence >= ctx.config.auto_remove_confidence:
            severity = Severity.HIGH
            remediation = Remediation.AUTO
            message = "Third-party promotional branding in a standalone block; block removed."
        elif confidence >= ctx.config.review_confidence:
            severity = Severity.MEDIUM
            remediation = Remediation.REVIEW
            message = "Possible third-party branding; removal needs confirmation."
        else:
            severity = Severity.INFO
            remediation = Remediation.REPORT
            message = "Brand mention inside normal content; kept for review."

        finding = Finding(
            rule="branding.text_block",
            severity=severity,
            category=Category.BRANDING,
            message=message,
            remediation=remediation,
            confidence=confidence,
            target=resource.path,
            location=location,
            evidence=evidence,
        )
        if remediation is Remediation.REPORT:
            mutations.append(Mutation(finding=finding, kind="report_only"))
            continue

        scheduled.add(id(container))
        mutations.append(
            Mutation(finding=finding, kind="remove_element", element=container, entry=resource.path)
        )
        for child in container.find_all(True):
            scheduled.add(id(child))

    # -- 2. links to promotional properties ------------------------------
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        text = text_of(anchor)
        href_is_promo = ctx.is_promo_url(href) if href else False
        text_hits = ctx.brand_hits(text)
        if not (href_is_promo or text_hits):
            continue
        if any(id(ancestor) in scheduled for ancestor in _ancestors(anchor)):
            continue

        brand_only_text = bool(text_hits) and _dominance(text, text_hits) >= 0.5
        location = describe_element(anchor)
        if brand_only_text or not text:
            finding = Finding(
                rule="branding.promo_link",
                severity=Severity.HIGH,
                category=Category.BRANDING,
                message="Hyperlink whose text is only third-party branding; link removed.",
                remediation=Remediation.AUTO,
                confidence=0.92,
                target=resource.path,
                location=location,
                evidence=truncate(f"{text} -> {href}"),
            )
            target = _promo_link_container(anchor)
            scheduled.add(id(target))
            mutations.append(
                Mutation(finding=finding, kind="remove_element", element=target, entry=resource.path)
            )
        elif ctx.config.neutralize_promo_links:
            finding = Finding(
                rule="branding.promo_link_target",
                severity=Severity.MEDIUM,
                category=Category.BRANDING,
                message=(
                    "Surviving text links to a promotional site; the link target is "
                    "removed but the words are kept."
                ),
                remediation=Remediation.REVIEW,
                confidence=0.6,
                target=resource.path,
                location=location,
                evidence=truncate(f"{text} -> {href}"),
            )
            mutations.append(
                Mutation(
                    finding=finding,
                    kind="strip_attribute",
                    element=anchor,
                    attribute="href",
                    entry=resource.path,
                )
            )

    # -- 3. images carrying branding ------------------------------------
    for image in soup.find_all(["img", "image"]):
        if any(id(ancestor) in scheduled for ancestor in _ancestors(image)):
            continue
        src = image.get("src") or image.get("xlink:href") or ""
        alt = image.get("alt") or image.get("title") or ""
        src_promo = ctx.is_promo_url(src) or ctx.has_brand(src)
        alt_brand = ctx.has_brand(alt)

        if src_promo:
            finding = Finding(
                rule="branding.promo_image",
                severity=Severity.HIGH,
                category=Category.BRANDING,
                message="Image loaded from third-party promotional branding; element removed.",
                remediation=Remediation.AUTO,
                confidence=0.93,
                target=resource.path,
                location=describe_element(image),
                evidence=truncate(src),
            )
            mutations.append(
                Mutation(finding=finding, kind="remove_element", element=image, entry=resource.path)
            )
        elif alt_brand:
            finding = Finding(
                rule="branding.promo_image_alt",
                severity=Severity.LOW,
                category=Category.BRANDING,
                message="Illustration alternative text carries branding; alt text cleared.",
                remediation=Remediation.AUTO,
                confidence=0.9,
                target=resource.path,
                location=describe_element(image),
                evidence=truncate(alt),
            )
            mutations.append(
                Mutation(
                    finding=finding,
                    kind="strip_attribute",
                    element=image,
                    attribute="alt",
                    entry=resource.path,
                )
            )

    # -- 4. inline styles and <style> blocks ----------------------------
    for style in soup.find_all("style"):
        raw = style.get_text()
        if ctx.has_brand(raw):
            finding = Finding(
                rule="branding.inline_style",
                severity=Severity.MEDIUM,
                category=Category.STYLE,
                message="Embedded stylesheet injects branding via generated content.",
                remediation=Remediation.AUTO,
                confidence=0.9,
                target=resource.path,
                location=describe_element(style),
                evidence=truncate(raw),
            )
            mutations.append(
                Mutation(
                    finding=finding,
                    kind="clean_text",
                    element=style,
                    entry=resource.path,
                )
            )

    for tag in soup.find_all(True):
        style_attr = tag.get("style")
        if isinstance(style_attr, str) and ctx.has_brand(style_attr):
            finding = Finding(
                rule="branding.inline_style_attribute",
                severity=Severity.MEDIUM,
                category=Category.STYLE,
                message="Inline style injects branding; declaration removed.",
                remediation=Remediation.AUTO,
                confidence=0.9,
                target=resource.path,
                location=describe_element(tag),
                evidence=truncate(style_attr),
            )
            mutations.append(
                Mutation(
                    finding=finding,
                    kind="strip_attribute",
                    element=tag,
                    attribute="style",
                    entry=resource.path,
                )
            )

    return mutations


def detect_navigation_branding(ctx: Context, resource: Resource) -> list[Mutation]:
    """Branding in nav documents and NCX files."""

    soup = resource.soup
    if soup is None:
        return []
    mutations: list[Mutation] = []

    for element in soup.find_all(True):
        name = local_name(element.name)
        is_nav_point = name == "navpoint"
        is_anchor = name == "a" or (name == "a" and element.get("href"))
        if not (is_nav_point or is_anchor):
            continue
        text = _nav_label_text(element) if is_nav_point else text_of(element)
        href = element.get("href") or element.get("src") or ""
        branded = ctx.has_brand(text) or (href and ctx.is_promo_url(href))
        if not branded:
            continue

        # NCX: drop the whole navPoint subtree. Nav: drop the list item.
        target = element if is_nav_point else element.find_parent("li") or element
        if target is None or local_name(target.name) in {"nav", "body", "html"}:
            continue

        finding = Finding(
            rule="branding.navigation_entry",
            severity=Severity.HIGH,
            category=Category.NAVIGATION,
            message="Table-of-contents entry created by a third party; entry removed.",
            remediation=Remediation.AUTO,
            confidence=0.93,
            target=resource.path,
            location=describe_element(target),
            evidence=truncate(text or href),
        )
        mutations.append(
            Mutation(finding=finding, kind="remove_element", element=target, entry=resource.path)
        )
    return mutations


# -- helpers -------------------------------------------------------------
def _score_brand_block(container: Tag, hits: list[str], ctx: Context) -> tuple[float, str]:
    text = text_of(container)
    words = count_words(text)
    # Markers such as class="footnote" or epub:type="footnote" usually sit on a
    # wrapper around the paragraph, so hint matching has to look at the chain.
    attributes = _context_attributes(container)
    score = 0.45
    reasons: list[str] = ["brand match"]

    if words <= 25:
        score += 0.15
        reasons.append("short block")
    if _dominance(text, hits) >= 0.25:
        score += 0.15
        reasons.append("brand dominates text")

    keyword = ctx.promo_keyword(text)
    if keyword:
        score += 0.15
        reasons.append(f"promotional wording ({keyword})")

    promo_hint = ctx.hint_match(attributes, ctx.config.promo_hints)
    if promo_hint:
        score += 0.2
        reasons.append(f"promotional markup ({promo_hint})")

    name = local_name(container.name)
    if name in {"footer", "aside", "header"} or is_inside(container, {"footer", "aside"}):
        score += 0.15
        reasons.append("furniture position")
    elif _is_trailing_block(container):
        score += 0.1
        reasons.append("trailing block")

    if _contains_promo_link(container, ctx):
        score += 0.15
        reasons.append("links to promotional site")

    if _is_citation(container, attributes, ctx):
        score -= 0.5
        reasons.append("citation context")

    if words > 60:
        score -= 0.3
        reasons.append("long prose")
    elif words > 30:
        score -= 0.15
        reasons.append("multi-sentence prose")

    dominance = _dominance(text, hits)
    if dominance < 0.08:
        score -= 0.15
        reasons.append("brand incidental")

    confidence = max(0.05, min(0.99, round(score, 2)))
    return confidence, ", ".join(reasons)


def _dominance(text: str, hits: list[str]) -> float:
    if not text:
        return 0.0
    return min(1.0, sum(len(hit) for hit in hits) / len(text))


def _context_attributes(container: Tag) -> str:
    """Attribute text of the block plus its wrappers (class/id/role/epub:type)."""

    parts = [attribute_text(container)]
    for ancestor in _ancestors(container):
        if local_name(ancestor.name) in {"body", "html"}:
            break
        parts.append(attribute_text(ancestor))
    return " ".join(part for part in parts if part)


def _is_citation(container: Tag, attributes: str, ctx: Context) -> bool:
    if is_inside(container, {"blockquote", "cite", "q", "figure", "figcaption"}):
        return True
    if local_name(container.name) in {"blockquote", "cite", "q"}:
        return True
    return ctx.hint_match(attributes, ctx.config.citation_hints) is not None


def _is_trailing_block(container: Tag) -> bool:
    body = container.find_parent("body")
    scope = body if body is not None else container.find_parent()
    if scope is None:
        return False
    last: Tag | None = None
    for element in scope.find_all(True):
        if local_name(element.name) in BLOCK_TAGS:
            last = element
    return last is container


def _contains_promo_link(container: Tag, ctx: Context) -> bool:
    return any(ctx.is_promo_url(anchor.get("href")) for anchor in container.find_all("a"))


def _nav_label_text(nav_point: Tag) -> str:
    """Only the navPoint's own label, never the labels of its children."""

    for child in nav_point.find_all(True):
        if local_name(child.name) == "navlabel":
            return text_of(child)
    return ""


def _promo_link_container(anchor: Tag) -> Tag:
    """Delete the smallest wrapper that exists only to hold the promo link."""

    parent = anchor.parent
    if (
        isinstance(parent, Tag)
        and local_name(parent.name) in {"li", "p", "div", "dd", "td"}
        and count_words(text_of(parent)) <= 12
    ):
        return parent
    return anchor


def _ancestors(element: Tag):
    parent = element.parent
    while isinstance(parent, Tag):
        yield parent
        parent = parent.parent


def _clean_text(node: NavigableString, entry: str) -> Mutation | None:
    finding = Finding(
        rule="branding.text_node",
        severity=Severity.LOW,
        category=Category.BRANDING,
        message="Branding removed from a title/heading while keeping the wording.",
        remediation=Remediation.AUTO,
        confidence=0.9,
        target=entry,
        location="title",
        evidence=truncate(str(node)),
    )
    return Mutation(finding=finding, kind="clean_text", element=node, entry=entry)


def detect_image_filename_branding(ctx: Context, resource: Resource) -> list[Mutation]:
    """Images whose *file name* carries branding and that nothing references."""

    if not ctx.has_brand(resource.basename):
        return []
    if resource.path in ctx.referenced_images:
        return []
    finding = Finding(
        rule="branding.promo_image_file",
        severity=Severity.MEDIUM,
        category=Category.BRANDING,
        message="Unreferenced image file carrying third-party branding; file removed.",
        remediation=Remediation.AUTO,
        confidence=0.9,
        target=resource.path,
        drops_entry=resource.path,
        evidence=posixpath.basename(resource.path),
    )
    return [Mutation(finding=finding, kind="drop_entry", entry=resource.path)]


__all__ = [
    "detect_markup_branding",
    "detect_navigation_branding",
    "detect_image_filename_branding",
    "normalize_space",
]
