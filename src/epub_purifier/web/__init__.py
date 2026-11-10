"""Local web interface for EPUB Purifier.

Privacy model: files are written to a per-job temporary directory, never logged,
and deleted when the job expires (or when the server exits). Nothing is proxied
to a third party and the page loads no external assets.
"""

from .app import create_app, main

__all__ = ["create_app", "main"]
