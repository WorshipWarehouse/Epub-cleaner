#!/usr/bin/env python3
"""Independent proof for EPUB Purifier.

Builds fresh fixtures (see ``proof/fixtures.py``), then drives the *real* CLI and
the public pipeline API to demonstrate:

1. EPUB 2 branding removal end to end,
2. false-positive protection for legitimate mentions plus the approval flow,
3. hostile-archive refusal/neutralisation,
4. honest reporting, DRM refusal and signature flagging.

Every check prints PASS/FAIL; the process exits non-zero if anything fails.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "proof"))

import fixtures  # noqa: E402

from epub_purifier import validate  # noqa: E402
from epub_purifier.archive import resolve_href  # noqa: E402
from epub_purifier.repackage import verify_epub  # noqa: E402

CLI = ROOT / ".venv" / "bin" / "epub-purifier"
WORK = fixtures.WORK

EPUBCHECK_JAR: Path | None = None
EPUBCHECK_VERSION = "unknown"

PASSED = 0
FAILED: list[str] = []


def check(scenario: str, ok: bool, message: str, detail: object = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  PASS  {scenario} · {message}")
    else:
        FAILED.append(f"{scenario}: {message}" + (f" [{detail}]" if detail != "" else ""))
        print(f"  FAIL  {scenario} · {message}" + (f"  -> {detail}" if detail != "" else ""))


def note(message: str) -> None:
    print(f"        {message}")


def cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - fixed argv, this script drives our own CLI
        [str(CLI), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def markup_blob(files: dict[str, bytes]) -> str:
    return "\n".join(
        data.decode("utf-8", "replace")
        for name, data in files.items()
        if name.endswith((".xhtml", ".html", ".opf", ".ncx", ".css", ".svg"))
    )


def brand_hits(files: dict[str, bytes]) -> int:
    blob = markup_blob(files).lower()
    return blob.count("oceanofpdf") + blob.count("ocean of pdf")


def ebooklib_summary(path: Path) -> tuple[list[str], list[str]]:
    from ebooklib import epub

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        book = epub.read_epub(str(path))
    names = [item.get_name() for item in book.get_items()]
    titles = [str(title) for title, _ in book.get_metadata("DC", "title")]
    return names, titles


def reference_problems(data: bytes) -> list[str]:
    """Every internal reference in manifest/spine/guide/nav/NCX must resolve."""

    from bs4 import BeautifulSoup

    problems: list[str] = []
    try:
        result = validate(data)
    except Exception as exc:  # noqa: BLE001
        return [f"validation failed: {exc}"]

    package = result.package
    archive = result.archive

    def exists(path: str | None) -> bool:
        return bool(path) and archive.exists(path)

    for item in package.items():
        if "://" in item.href:
            continue
        if not exists(item.path):
            problems.append(f"manifest href missing: {item.href}")
    for item in package.spine_items():
        if not exists(item.path):
            problems.append(f"spine href missing: {item.href}")
    if package.guide is not None:
        for reference in package.guide.find_all(True):
            href = reference.get("href")
            if isinstance(href, str) and not exists(resolve_href(package.entry, href)):
                problems.append(f"guide href missing: {href}")

    ncx = package.ncx_entry()
    if ncx and archive.exists(ncx):
        soup = BeautifulSoup(archive.read_text(ncx), "lxml-xml")
        for content in soup.find_all("content"):
            src = content.get("src")
            if isinstance(src, str) and "://" not in src and not exists(resolve_href(ncx, src)):
                problems.append(f"ncx src missing: {src}")

    nav = package.nav_entry()
    if nav and archive.exists(nav):
        soup = BeautifulSoup(archive.read_text(nav), "lxml-xml")
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if isinstance(href, str) and not exists(resolve_href(nav, href)):
                problems.append(f"nav href missing: {href}")
    return problems


def conformance(scenario: str, path: Path) -> None:
    """Judge a cleaned file with the official EPUBCheck validator, when supplied."""

    if EPUBCHECK_JAR is None:
        return
    if not path.exists():
        check(scenario, False, f"EPUBCheck could not run: {path.name} is missing")
        return
    java = shutil.which("java")
    if java is None:
        check(scenario, False, "EPUBCheck could not run: no java executable on PATH")
        return
    proc = subprocess.run(  # noqa: S603 - fixed argv, local jar
        [java, "-jar", str(EPUBCHECK_JAR), str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    summary = re.search(
        r"Messages:\s*(\d+) fatals? / (\d+) errors? / (\d+) warnings? / (\d+) infos?",
        output,
    )
    if summary is None:
        check(scenario, False, f"EPUBCheck gave no verdict for {path.name}", output.strip()[-300:])
        return
    fatals, errors, warnings_seen, infos = (int(value) for value in summary.groups())
    note(f"{path.name}: {EPUBCHECK_VERSION} -> {summary.group(0)}")
    check(
        scenario,
        fatals == 0 and errors == 0,
        f"{path.name} conforms to the EPUB specification (0 fatal / 0 error)",
        summary.group(0),
    )
    for line in output.splitlines():
        if line.startswith(("WARNING", "INFO")):
            note(f"    {line}")


# ---------------------------------------------------------------------------
# Scenario 1
# ---------------------------------------------------------------------------
def scenario_1(paths: dict[str, Path]) -> None:
    print("\n=== Scenario 1: EPUB 2 book with Ocean-of-PDF insertions ===")
    source = paths["s1-epub2-branded.epub"]
    cleaned = WORK / "s1-cleaned.epub"
    inspect_json = WORK / "s1-inspect.json"
    clean_json = WORK / "s1-clean.json"

    code, _stdout, stderr = cli(
        "inspect", str(source), "--format", "json", "--json", str(inspect_json)
    )
    check("S1", code == 0, "inspect exits 0", stderr.strip()[:200])
    report = json.loads(inspect_json.read_text())
    findings = report["findings"]
    rules = {finding["rule"] for finding in findings}
    note(f"findings by severity: {report['summary']['counts']}")

    expected = {
        "branding.text_block": "chapter footer paragraph",
        "branding.metadata": "OPF metadata",
        "branding.css_generated_content": "CSS ::after content",
        "branding.navigation_entry": "NCX navigation entry",
        "branding.guide_reference": "guide entry",
        "branding.promo_page": "dedicated promo page",
        "branding.promo_link": "hyperlink",
    }
    for rule, label in expected.items():
        check("S1", rule in rules, f"{label} detected ({rule})", sorted(rules))
    check(
        "S1",
        report["summary"]["counts"]["critical"] == 0,
        "a promo-only book produces no critical findings",
        report["summary"]["counts"],
    )
    auto = [finding for finding in findings if finding["remediation"] == "auto"]
    check("S1", len(auto) >= 6, "promo artifacts are auto-removable", len(auto))

    code, _stdout, stderr = cli(
        "clean", str(source), "-o", str(cleaned), "--report", str(clean_json), "--json"
    )
    check("S1", code == 0, "clean exits 0", stderr.strip()[:200])
    clean_report = json.loads(clean_json.read_text())
    note(
        f"removed {len(clean_report['removed'])} item(s); "
        f"dropped entries: {clean_report['stats']['dropped_entries']}"
    )

    files = entries(cleaned)
    blob = markup_blob(files)
    check("S1", "OEBPS/text/promo.xhtml" not in files, "dedicated promo page dropped")
    check("S1", brand_hits(files) == 0, "no OceanofPDF text left anywhere", brand_hits(files))
    check("S1", "The church has always been analog" in blob, "chapter prose preserved")
    check("S1", "Screens are not evil" in blob, "second chapter paragraph preserved")
    check(
        "S1",
        "Readers who want to compare translations" in blob,
        "sentence around the removed hyperlink preserved",
    )

    opf = files["OEBPS/content.opf"].decode()
    check("S1", "The Analog Church" in opf and "Jay Y. Kim" in opf, "title and author preserved")
    check("S1", 'id="promo"' not in opf and 'idref="promo"' not in opf, "promo item removed from manifest/spine")
    check("S1", opf.count("<itemref") == 2, "spine keeps exactly the two real documents", opf.count("<itemref"))
    check("S1", "OceanofPDF" not in opf, "publisher/description branding removed")
    check("S1", opf.count("<reference") == 2, "guide keeps cover + text references", opf.count("<reference"))
    check("S1", 'version="2.0"' in opf, "package still declares OPF 2.0")
    check("S1", "dcterms:modified" not in opf, "no EPUB 3-only meta injected into an OPF 2 package")

    ncx = files["OEBPS/toc.ncx"].decode()
    check("S1", "OceanofPDF" not in ncx, "NCX navigation entry removed")
    check("S1", ncx.count("<navPoint") == 2, "NCX keeps the two genuine entries", ncx.count("<navPoint"))

    css = files["OEBPS/styles/book.css"].decode()
    check("S1", "OceanofPDF" not in css and "::after" not in css, "CSS generated-content branding removed")
    check("S1", "font-style: italic" in css and "font-family: Georgia" in css, "legitimate CSS preserved")

    problems = verify_epub(cleaned.read_bytes())
    check("S1", problems == [], "output passes structural verification", problems)
    references = reference_problems(cleaned.read_bytes())
    check("S1", references == [], "manifest/spine/guide/NCX references all resolve", references)

    names, titles = ebooklib_summary(cleaned)
    check(
        "S1",
        len(names) >= 4 and not any("promo" in name.lower() for name in names),
        "EbookLib reopens the book without the promo document",
        names,
    )
    check("S1", titles[:1] == ["The Analog Church"], "EbookLib reads the title", titles)
    conformance("S1", cleaned)


# ---------------------------------------------------------------------------
# Scenario 2
# ---------------------------------------------------------------------------
def scenario_2(paths: dict[str, Path]) -> None:
    print("\n=== Scenario 2: legitimate mentions and the approval flow ===")
    source = paths["s2-legit-mentions.epub"]
    inspect_json = WORK / "s2-inspect.json"

    code, _stdout, stderr = cli(
        "inspect", str(source), "--format", "json", "--json", str(inspect_json)
    )
    check("S2", code == 0, "inspect exits 0", stderr.strip()[:200])
    report = json.loads(inspect_json.read_text())
    findings = report["findings"]
    note(f"findings by severity: {report['summary']['counts']}")

    branding = [finding for finding in findings if finding["category"] == "branding"]
    note(
        "brand mentions: "
        + ", ".join(
            f"{finding['remediation']}/{finding['confidence']}" for finding in branding
        )
    )
    check(
        "S2",
        not any(finding["remediation"] == "auto" for finding in branding),
        "no brand mention in prose is auto-removable",
        [(f["rule"], f["confidence"]) for f in branding],
    )
    check(
        "S2",
        not any("ocean stretched out" in (f["evidence"] or "").lower() for f in findings),
        "'ocean' and 'PDF' in separate sentences raised no finding",
    )
    check("S2", "branding.text_block" in {f["rule"] for f in branding}, "mentions are still reported")
    check(
        "S2",
        all(f["severity"] in {"informational", "medium"} for f in branding),
        "mentions are only informational/medium severity",
        [f["severity"] for f in branding],
    )

    reviewable = [f for f in findings if f["remediation"] == "review"]
    check("S2", len(reviewable) == 1, "exactly one ambiguous item surfaces for approval", len(reviewable))

    default_out = WORK / "s2-default.epub"
    default_report = WORK / "s2-default.json"
    code, _stdout, stderr = cli(
        "clean", str(source), "-o", str(default_out), "--report", str(default_report), "--json"
    )
    check("S2", code == 0, "clean without approval exits 0", stderr.strip()[:200])
    check("S2", "ambiguous" in stderr.lower(), "CLI explains the ambiguous items were kept", stderr.strip()[:120])

    chapter = entries(default_out)["OEBPS/text/notes.xhtml"].decode()
    for text in (
        "The ocean stretched out before us",
        "opened the PDF on my laptop",
        "As the editors at OceanofPDF put it",
        "Source: oceanofpdf.com",
        "downloaded from OceanofPDF",
        "liturgy and attention",
    ):
        check("S2", text in chapter, f"preserved without approval: {text[:44]}")

    approved_out = WORK / "s2-approved.epub"
    approved_report = WORK / "s2-approved.json"
    approved_id = reviewable[0]["id"]
    note(f"approving finding id {approved_id}")
    code, _stdout, stderr = cli(
        "clean",
        str(source),
        "-o",
        str(approved_out),
        "--approve",
        approved_id,
        "--report",
        str(approved_report),
        "--json",
    )
    check("S2", code == 0, "--approve run exits 0", stderr.strip()[:200])
    chapter = entries(approved_out)["OEBPS/text/notes.xhtml"].decode()
    check("S2", "downloaded from OceanofPDF" not in chapter, "approved paragraph removed")
    check("S2", "As the editors at OceanofPDF put it" in chapter, "quotation still preserved")
    check("S2", "Source: oceanofpdf.com" in chapter, "footnote citation still preserved")
    check(
        "S2",
        "The ocean stretched out before us" in chapter and "opened the PDF on my laptop" in chapter,
        "ordinary prose untouched",
    )
    check("S2", "liturgy and attention" in chapter, "neighbouring paragraph preserved")
    check("S2", verify_epub(approved_out.read_bytes()) == [], "approved output is structurally valid")
    names, _titles = ebooklib_summary(approved_out)
    check("S2", len(names) >= 2, "EbookLib reopens the approved output", names)
    conformance("S2", default_out)
    conformance("S2", approved_out)


# ---------------------------------------------------------------------------
# Scenario 3
# ---------------------------------------------------------------------------
def scenario_3(paths: dict[str, Path]) -> None:
    print("\n=== Scenario 3: hostile archive ===")
    bomb_source = paths["s3-hostile.epub"]
    bomb_out = WORK / "s3-bomb-out.epub"
    bomb_out.unlink(missing_ok=True)

    code, _stdout, stderr = cli("clean", str(bomb_source), "-o", str(bomb_out))
    check("S3", code == 3, "decompression bomb is refused (exit 3)", f"code={code} {stderr.strip()[:120]}")
    check("S3", not bomb_out.exists(), "no output written for a refused archive")
    check(
        "S3",
        any(word in stderr.lower() for word in ("ratio", "limit")),
        "refusal names the limit that was hit",
        stderr.strip()[:160],
    )

    source = paths["s3-hostile-no-bomb.epub"]
    inspect_json = WORK / "s3-inspect.json"
    code, _stdout, stderr = cli(
        "inspect", str(source), "--format", "json", "--json", str(inspect_json)
    )
    check("S3", code == 0, "cleanable hostile archive is inspected", stderr.strip()[:200])
    report = json.loads(inspect_json.read_text())
    severities: dict[str, set[str]] = {}
    for finding in report["findings"]:
        severities.setdefault(finding["rule"], set()).add(finding["severity"])
    note(f"findings by severity: {report['summary']['counts']}")
    note("rules: " + ", ".join(f"{rule}={sorted(values)}" for rule, values in sorted(severities.items())))

    expected = {
        "archive.unsafe_member": "critical",
        "security.script": "critical",
        "security.event_handler": "high",
        "security.script_url": "high",
        "security.iframe": "high",
        "security.xml_entities": "high",
        "tracking.pixel": "high",
        "external.remote_asset": "medium",
        "style.remote_import": "medium",
        "archive.nested_archive": "medium",
        "archive.extra_document": "low",
    }
    for rule, severity in expected.items():
        check(
            "S3",
            severity in severities.get(rule, set()),
            f"{rule} classified {severity}",
            sorted(severities.get(rule, set())),
        )

    cleaned = WORK / "s3-cleaned.epub"
    clean_json = WORK / "s3-clean.json"
    code, _stdout, stderr = cli(
        "clean",
        str(source),
        "-o",
        str(cleaned),
        "--approve-all",
        "--remove-remote",
        "--report",
        str(clean_json),
        "--json",
    )
    check("S3", code == 0, "clean with approvals succeeds", stderr.strip()[:200])
    clean_report = json.loads(clean_json.read_text())
    note(f"removed {len(clean_report['removed'])} item(s); dropped: {clean_report['stats']['dropped_entries']}")

    files = entries(cleaned)
    names = list(files)
    check(
        "S3",
        not any(name.startswith("/") or ".." in name.split("/") for name in names),
        "no traversal or absolute members survive",
        names,
    )
    check("S3", "OEBPS/extras/bonus.zip" not in files, "nested archive dropped after approval")
    check("S3", "OEBPS/extras/companion.pdf" not in files, "oversized embedded document dropped after approval")

    chapter = files["OEBPS/text/chapter1.xhtml"].decode()
    for token in (
        "<script",
        "onload",
        "onerror",
        "javascript:",
        "<iframe",
        "tracker.example",
        "cdn.example.org",
        "&xxe;",
        "<!ENTITY",
        "root:",
        "document.write",
    ):
        check("S3", token not in chapter, f"chapter neutralised: {token}")
    check("S3", "Ordinary paragraph that should survive" in chapter, "chapter prose preserved")

    svg = files["OEBPS/art/logo.svg"].decode()
    check("S3", "<script" not in svg and "alert(3)" not in svg, "SVG script removed")
    check("S3", "<rect" in svg, "SVG illustration itself preserved")

    css = files["OEBPS/styles/book.css"].decode()
    check("S3", "@import" not in css and "cdn.example.org" not in css, "remote CSS import and url removed")
    check("S3", "font-family: Georgia, serif" in css, "local CSS declaration preserved")

    problems = verify_epub(cleaned.read_bytes())
    check("S3", problems == [], "cleaned hostile book is structurally valid", problems)
    check("S3", reference_problems(cleaned.read_bytes()) == [], "cleaned hostile book references resolve")
    result = validate(cleaned.read_bytes())
    check("S3", result.version.startswith("3."), "output re-validates as EPUB 3", result.version)
    names, _titles = ebooklib_summary(cleaned)
    check("S3", len(names) >= 3, "EbookLib reopens the cleaned hostile book", names)
    conformance("S3", cleaned)


# ---------------------------------------------------------------------------
# Scenario 4
# ---------------------------------------------------------------------------
def scenario_4(paths: dict[str, Path]) -> None:
    print("\n=== Scenario 4: reporting honesty, DRM and signatures ===")
    drm_source = paths["s4-drm.epub"]
    drm_out = WORK / "s4-drm-out.epub"
    drm_out.unlink(missing_ok=True)

    code, _stdout, stderr = cli("clean", str(drm_source), "-o", str(drm_out))
    check("S4", code == 4, "DRM-protected book refused with exit 4", f"code={code} {stderr.strip()[:120]}")
    check("S4", not drm_out.exists(), "DRM book left unaltered (no output)")
    check("S4", "drm" in stderr.lower(), "refusal explains DRM", stderr.strip()[:160])

    signed_source = paths["s4-signed.epub"]
    signed_out = WORK / "s4-signed-out.epub"
    signed_json = WORK / "s4-signed.json"
    code, _stdout, stderr = cli(
        "clean", str(signed_source), "-o", str(signed_out), "--report", str(signed_json), "--json"
    )
    check("S4", code == 0, "signed book is flagged rather than refused", stderr.strip()[:160])
    signed_report = json.loads(signed_json.read_text())
    check(
        "S4",
        any(finding["rule"] == "epub.digital_signature" for finding in signed_report["findings"]),
        "signature finding present",
    )
    check(
        "S4",
        any("signature" in text.lower() for text in signed_report["notes"]),
        "report notes that the signature is invalidated",
        signed_report["notes"],
    )
    check(
        "S4",
        "META-INF/signatures.xml" in entries(signed_out),
        "signature document preserved rather than silently rewritten",
    )

    # A book with nothing to report must still refuse to promise safety.
    clean_source = paths["s4-clean.epub"]
    clean_json_path = WORK / "s4-clean-report.json"
    code, _stdout, stderr = cli(
        "inspect", str(clean_source), "--format", "json", "--json", str(clean_json_path)
    )
    check("S4", code == 0, "clean book inspects successfully", stderr.strip()[:160])
    clean_report = json.loads(clean_json_path.read_text())
    check(
        "S4",
        clean_report["summary"]["counts"] == {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "informational": 0,
        },
        "clean book produces zero findings",
        clean_report["summary"]["counts"],
    )
    verdict = clean_report["summary"]["verdict"].lower()
    check("S4", "not a safety guarantee" in verdict, "zero-finding verdict refuses to promise safety", verdict)

    # No report produced in this run may imply the file is safe.
    banned = re.compile(r"malware[- ]?free|no malware|guaranteed safe|safe to open|100% safe", re.I)
    for report_path in sorted(WORK.glob("*.json")):
        payload = json.loads(report_path.read_text())
        check(
            "S4",
            "not a guarantee" in payload["disclaimer"].lower(),
            f"{report_path.name}: disclaimer present",
        )
        match = banned.search(json.dumps(payload))
        check("S4", match is None, f"{report_path.name}: no malware-free/safe claim", match.group(0) if match else "")

    _code, text_report, _stderr = cli("inspect", str(clean_source))
    check(
        "S4",
        "not a guarantee" in text_report.lower() and "safety" in text_report.lower(),
        "text report carries the safety disclaimer",
    )


# ---------------------------------------------------------------------------
def main() -> int:
    global EPUBCHECK_JAR, EPUBCHECK_VERSION
    parser = argparse.ArgumentParser(description="Independent proof for EPUB Purifier.")
    parser.add_argument(
        "--epubcheck",
        metavar="JAR",
        help="path to the official EPUBCheck jar; every cleaned output is then judged by it",
    )
    args = parser.parse_args()

    print("EPUB Purifier — independent proof")
    if args.epubcheck:
        jar = Path(args.epubcheck).expanduser().resolve()
        if not jar.is_file():
            print(f"EPUBCheck jar not found: {jar}")
            return 2
        java = shutil.which("java")
        if java is None:
            print("EPUBCheck could not run: no java executable on PATH")
            return 2
        EPUBCHECK_JAR = jar
        version = subprocess.run(  # noqa: S603 - fixed argv, local jar
            [java, "-jar", str(jar), "--version"], capture_output=True, text=True
        )
        banner = (version.stdout + version.stderr).strip()
        EPUBCHECK_VERSION = banner.splitlines()[0].strip() if banner else "unknown"
        note(f"validator: {EPUBCHECK_VERSION} ({jar})")
    print("=" * 72)
    for stale in WORK.glob("*"):
        if stale.is_file():
            stale.unlink()
        else:
            shutil.rmtree(stale, ignore_errors=True)
    paths = fixtures.build_all()
    for name, path in paths.items():
        note(f"fixture {name}: {path.stat().st_size} bytes")

    scenario_1(paths)
    scenario_2(paths)
    scenario_3(paths)
    scenario_4(paths)

    print("\n" + "=" * 72)
    if FAILED:
        print(f"PROOF RESULT: {PASSED} passed, {len(FAILED)} FAILED")
        for failure in FAILED:
            print(f"  - {failure}")
        return 1
    print(f"PROOF RESULT: all {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
