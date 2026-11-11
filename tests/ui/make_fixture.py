"""Write the branded EPUB used by the browser drop test.

Run from the repository root by Playwright global setup; kept as a separate script
so the Playwright test only has to read one file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.converted_book import converted_book  # noqa: E402


def main() -> None:
    target = Path(__file__).resolve().parent / ".tmp" / "branded.epub"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(converted_book())
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
