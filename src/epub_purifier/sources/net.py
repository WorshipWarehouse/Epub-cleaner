"""Guarded outbound HTTP used by source adapters.

Every request is forced through the same checks: https only, a per-source host
allowlist, no embedded credentials, every resolved address must be globally
routable (no loopback, link-local or private ranges), redirects are followed by
hand so each hop is re-checked, and the body is capped while it is read.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass

from .base import DownloadRefused, SourceError

USER_AGENT = "EPUBPurifier/0.1 (+https://github.com/epub-purifier/epub-purifier)"
MAX_REDIRECTS = 3
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Fetched:
    url: str
    content_type: str
    data: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses as HTTPError so redirects can be checked one by one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for pattern in allowed_hosts:
        pattern = pattern.lower().strip(".")
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def _require_public_address(host: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SourceError("Could not resolve the source host", detail=str(exc)) from exc
    if not infos:
        raise SourceError("Could not resolve the source host")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise DownloadRefused(
                "Refusing to contact a private or reserved address", detail=str(address)
            )


def check_url(url: str, *, allowed_hosts: Iterable[str]) -> str:
    """Validate a URL before any connection is opened."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise DownloadRefused("Only https:// downloads are allowed")
    if not parsed.hostname:
        raise DownloadRefused("Download URL has no host")
    if parsed.username or parsed.password:
        raise DownloadRefused("Download URL must not embed credentials")
    if not host_allowed(parsed.hostname, allowed_hosts):
        raise DownloadRefused(
            f"Host {parsed.hostname!r} is not on this source's allowlist",
            detail=parsed.hostname,
        )
    _require_public_address(parsed.hostname, parsed.port or 443)
    return url


def _read_limited(stream, max_bytes: int) -> bytes:  # noqa: ANN001 - file-like
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(_CHUNK, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DownloadRefused(
                "The file is larger than the download limit", detail=f"{max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_bytes(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    max_bytes: int,
    timeout: float = 15.0,
    accept: str | None = None,
) -> Fetched:
    """Fetch a URL with the guards above applied to every hop."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current, allowed_hosts=allowed_hosts)
        # check_url() has already forced https and a public address, so no
        # file:/custom scheme can reach the opener.
        request = urllib.request.Request(  # noqa: S310
            current,
            headers={"User-Agent": USER_AGENT, "Accept": accept or "*/*"},
        )
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                content_type = (
                    (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                )
                data = _read_limited(response, max_bytes)
                return Fetched(url=current, content_type=content_type, data=data)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in {301, 302, 303, 307, 308} and location:
                current = urllib.parse.urljoin(current, location)
                continue
            raise SourceError(f"The source answered HTTP {exc.code}", detail=current) from exc
        except urllib.error.URLError as exc:
            raise SourceError("The source could not be reached", detail=str(exc.reason)) from exc
        except TimeoutError as exc:
            raise SourceError("The source timed out", detail=current) from exc
    raise SourceError("Too many redirects while contacting the source", detail=url)


def looks_like_zip(data: bytes) -> bool:
    return data[:4] in {b"PK\x03\x04", b"PK\x05\x06"}


__all__ = ["Fetched", "check_url", "fetch_bytes", "host_allowed", "looks_like_zip"]
