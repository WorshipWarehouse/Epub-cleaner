# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# pyproject.toml names the "web" extra and the epub-purifier-web console
# script. README.md and LICENSE are required build metadata. The UI assets
# (static/ and templates/) live under src/epub_purifier/web and are included in
# the wheel by the package-data configuration in pyproject.toml.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install ".[web]" \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/lib/epub-purifier/transfers \
    && chown -R appuser:appuser /var/lib/epub-purifier

# Persistent job metadata and temporary cleaned books. Mount a volume here so a
# container restart can still expire and delete the jobs it was holding.
ENV EPUB_PURIFIER_HOST=0.0.0.0 \
    EPUB_PURIFIER_PORT=8000 \
    EPUB_PURIFIER_TRANSFER_DIR=/var/lib/epub-purifier/transfers

VOLUME ["/var/lib/epub-purifier"]

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]

CMD ["epub-purifier-web"]
