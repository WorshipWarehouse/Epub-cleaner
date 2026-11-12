"""Command line interface.

Two verbs, each mapping to one pipeline stage:

    epub-purifier inspect book.epub
    epub-purifier clean book.epub -o book.cleaned.epub

Ambiguous removals are only applied when the caller approves them (``--approve``
or ``--approve-all``), matching the review step in the web UI.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .config import PurifierConfig, load_config_file
from .errors import PurifierError, UsageError, ValidationError
from .models import Severity
from .pipeline import clean_epub, inspect_epub
from .report import Report

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

_COLORS = {
    Severity.CRITICAL: "\033[1;31m",
    Severity.HIGH: "\033[31m",
    Severity.MEDIUM: "\033[33m",
    Severity.LOW: "\033[36m",
    Severity.INFO: "\033[2m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epub-purifier",
        description=(
            "Remove third-party promotions, trackers and active content from EPUB files. "
            "Only clean books you own or are authorised to modify."
        ),
    )
    parser.add_argument("--version", action="version", version=f"epub-purifier {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="analyse a file and print a report without changing it"
    )
    inspect_parser.add_argument(
        "file", help="EPUB file, or an unzipped book folder, to inspect"
    )
    inspect_parser.add_argument("--json", dest="json_path", help="also write the report as JSON")
    inspect_parser.add_argument("--format", choices=("text", "json"), default="text")
    inspect_parser.add_argument("--config", help="TOML/JSON configuration file")
    inspect_parser.add_argument(
        "--fail-on",
        choices=("critical", "high", "medium", "low", "never"),
        default="never",
        help="exit with status 1 when a finding at this severity or worse exists",
    )
    _add_threshold_flags(inspect_parser, include_approval=False)

    clean_parser = subparsers.add_parser(
        "clean", help="inspect, remove unwanted content and write a cleaned EPUB"
    )
    clean_parser.add_argument(
        "file", help="EPUB file, or an unzipped book folder, to clean"
    )
    clean_parser.add_argument("-o", "--output", help="path for the cleaned EPUB")
    clean_parser.add_argument("--report", help="write the cleaning report as JSON")
    clean_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    clean_parser.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="FINDING_ID",
        help="approve one ambiguous finding by id (repeatable)",
    )
    clean_parser.add_argument(
        "--approve-all",
        action="store_true",
        help="approve every ambiguous finding without asking (non-interactive)",
    )
    clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the whole pipeline but do not write the cleaned file",
    )
    clean_parser.add_argument("--config", help="TOML/JSON configuration file")
    clean_parser.add_argument(
        "--fail-on",
        choices=("critical", "high", "medium", "low", "never"),
        default="never",
        help="exit with status 1 when a remaining finding at this severity exists",
    )
    _add_threshold_flags(clean_parser, include_approval=True)
    return parser


def _add_threshold_flags(parser: argparse.ArgumentParser, *, include_approval: bool) -> None:
    parser.add_argument(
        "--min-confidence",
        type=float,
        metavar="0..1",
        help="override the confidence needed to remove ambiguous items",
    )
    parser.add_argument(
        "--review-confidence",
        type=float,
        metavar="0..1",
        help="below this confidence a brand mention is only reported",
    )
    if include_approval:
        parser.add_argument(
            "--remove-remote",
            action="store_true",
            help="also strip remote images, stylesheets and media",
        )
        parser.add_argument(
            "--keep-promo-pages",
            action="store_true",
            help="never drop whole documents that only advertise",
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = _config_from_args(args)
    except UsageError as exc:
        print(f"epub-purifier: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.command == "inspect":
            return _run_inspect(args, config)
        return _run_clean(args, config)
    except ValidationError as exc:
        print(f"epub-purifier: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"  detail: {exc.detail}", file=sys.stderr)
        return exc.exit_code
    except PurifierError as exc:
        print(f"epub-purifier: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"  detail: {exc.detail}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\ninterrupted", file=sys.stderr)
        return 130


def _config_from_args(args: argparse.Namespace) -> PurifierConfig:
    config = PurifierConfig()
    if getattr(args, "config", None):
        config = load_config_file(args.config)
    overrides: dict[str, object] = {}
    if getattr(args, "min_confidence", None) is not None:
        overrides["auto_remove_confidence"] = args.min_confidence
    if getattr(args, "review_confidence", None) is not None:
        overrides["review_confidence"] = args.review_confidence
    if getattr(args, "remove_remote", False):
        overrides["remove_remote_resources"] = True
    if getattr(args, "keep_promo_pages", False):
        overrides["drop_promo_pages"] = False
    if overrides:
        config = config.with_overrides(**overrides)
    if not 0.0 <= config.review_confidence <= config.auto_remove_confidence <= 1.0:
        raise UsageError(
            "confidence thresholds must satisfy 0 <= review-confidence <= min-confidence <= 1"
        )
    return config


def _run_inspect(args: argparse.Namespace, config: PurifierConfig) -> int:
    report = inspect_epub(args.file, config)
    _emit(report, args.format, args.json_path)
    return _exit_for(report, args.fail_on)


def _run_clean(args: argparse.Namespace, config: PurifierConfig) -> int:
    approved = set(args.approve or [])
    approve_all = bool(args.approve_all)

    if not approve_all and not approved:
        # Show the ambiguous items first so a human can confirm them.
        preview = inspect_epub(args.file, config)
        ambiguous = preview.reviewable()
        if ambiguous:
            if sys.stdin.isatty() and not args.json:
                approve_all = _confirm(ambiguous)
            else:
                print(
                    "epub-purifier: "
                    f"{len(ambiguous)} ambiguous finding(s) left in place "
                    "(re-run with --approve-all or --approve ID).",
                    file=sys.stderr,
                )

    result = clean_epub(
        args.file,
        config,
        approved=approved,
        approve_all=approve_all,
        output=None if args.dry_run else (args.output or _default_output(args.file)),
        dry_run=args.dry_run,
    )
    report = result.report

    if not args.dry_run and result.output_path:
        report.output_name = Path(result.output_path).name

    _emit(report, "json" if args.json else "text", args.report)
    if not args.json:
        if args.dry_run:
            _print(f"\nDry run: no file written. {len(report.removed)} change(s) proposed.")
        elif result.output_path:
            _print(f"\nWrote {result.output_path}")
    return _exit_for(report, args.fail_on)


def _confirm(ambiguous) -> bool:
    _print(f"\n{len(ambiguous)} item(s) need confirmation:", stream=sys.stderr)
    for finding in ambiguous[:20]:
        target = finding.target or "(package)"
        _print(
            f"  [{finding.severity.label}] {finding.message}\n"
            f"    id {finding.finding_id}  {target}  {finding.location or ''}",
            stream=sys.stderr,
        )
    if len(ambiguous) > 20:
        _print(f"  ... and {len(ambiguous) - 20} more", stream=sys.stderr)
    try:
        answer = input("Remove all of these as well? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _default_output(source: str) -> str:
    path = Path(source)
    return str(path.with_name(f"{path.stem}.cleaned{path.suffix or '.epub'}"))


def _emit(report: Report, fmt: str, json_path: str | None) -> None:
    if json_path:
        Path(json_path).write_text(report.to_json(), encoding="utf-8")
        if fmt != "json":
            _print(f"Report written to {json_path}")
    if fmt == "json":
        _print(report.to_json())
    else:
        _print(render_colored(report))


def _print(*values: object, stream=None) -> None:
    print(*values, file=stream or sys.stdout)


def render_colored(report: Report) -> str:
    """Plain text report with severity colours when the terminal allows it."""

    text = report.to_text()
    if not _use_color():
        return text
    for severity, color in _COLORS.items():
        text = text.replace(f"{severity.label} (", f"{color}{severity.label}{_RESET} (")
    header, _, rest = text.partition("\n")
    return f"{_BOLD}{header}{_RESET}\n{rest}"


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _exit_for(report: Report, fail_on: str) -> int:
    if fail_on == "never":
        return EXIT_OK
    threshold = {
        "critical": Severity.CRITICAL,
        "high": Severity.HIGH,
        "medium": Severity.MEDIUM,
        "low": Severity.LOW,
    }[fail_on]
    worst = report.highest_severity()
    if worst is None:
        return EXIT_OK
    return EXIT_ERROR if worst.weight >= threshold.weight else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
