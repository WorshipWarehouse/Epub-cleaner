"""Detection rules.

Each module exposes ``detect_*`` functions that return :class:`~epub_purifier.models.Mutation`
objects. A mutation always carries the finding it belongs to, so inspection and
cleaning share one pass over the document and can never disagree about what was
seen.
"""

from . import content, metadata, resources, security, styles

__all__ = ["content", "metadata", "resources", "security", "styles"]
