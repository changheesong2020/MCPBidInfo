"""Command line interface for the TED × UNGM toolkit."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Iterable, Sequence

from .ted_client import DEFAULT_FIELDS, build_query, iterate_all, search_once
from .ungm_helpers import build_ungm_deeplink, sync_country_codes, sync_unspsc_segments


def configure_logging(verbosity: int) -> None:
    """Configure logging based on the ``-v`` flag count."""

    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TED/UNGM harvesting utilities")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase logging verbosity")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ted_parser = subparsers.add_parser("ted", help="Execute a TED search")
    ted_parser.add_argument("--date-from", required=True, help="Inclusive lower bound publication date (YYYY-MM-DD)")
    ted_parser.add_argument("--date-to", required=True, help="Inclusive upper bound publication date (YYYY-MM-DD)")
    ted_parser.add_argument("--countries", nargs="*", default=[], help="ISO alpha-2 country codes")
    ted_parser.add_argument("--cpv", nargs="*", default=[], help="CPV code prefixes")
    ted_parser.add_argument("--keywords", nargs="*", default=[], help="Title keywords")
    ted_parser.add_argument("--form-type", nargs="*", default=[], help="eForms form-type filters")
    ted_parser.add_argument(
        "--fields",
        nargs="*",
        default=None,
        help="Projection fields (defaults to the minimal recommended list)",
    )
    ted_parser.add_argument("--mode", choices=("page", "iteration"), default="page")
    ted_parser.add_argument("--page", type=int, default=1, help="Page number when using page mode")
    ted_parser.add_argument("--limit", type=int, default=100, help="Page size or iteration batch size")
    ted_parser.add_argument("--sort-field", default="publication-date", help="Sort field")
    ted_parser.add_argument("--sort-order", choices=("asc", "desc"), default="desc", help="Sort order")
    ted_parser.add_argument("--out", help="Output file path (stdout when omitted)")
    ted_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    ted_parser.set_defaults(func=handle_ted)

    sync_parser = subparsers.add_parser("ungm-sync", help="Synchronise UNGM helper datasets")
    sync_parser.add_argument("--dataset", choices=("country", "unspsc"), required=True)
    sync_parser.add_argument("--out", help="Optional output file to write the dataset as JSON")
    sync_parser.add_argument("--pretty", action="store_true", help="Pretty-print the dataset JSON")
    sync_parser.set_defaults(func=handle_ungm_sync)

    link_parser = subparsers.add_parser("build-ungm-url", help="Generate a UNGM notice search URL")
    link_parser.add_argument("--countries", nargs="*", default=[])
    link_parser.add_argument("--unspsc", nargs="*", default=[])
    link_parser.add_argument("--keywords", nargs="*", default=[])
    link_parser.set_defaults(func=handle_ungm_link)

    return parser.parse_args(argv)


def handle_ted(args: argparse.Namespace) -> int:
    form_types = getattr(args, "form_type", []) or []
    query = build_query(
        date_from=args.date_from,
        date_to=args.date_to,
        countries=args.countries,
        cpv_prefixes=args.cpv,
        keywords=args.keywords,
        form_types=form_types,
    )
    fields = DEFAULT_FIELDS if args.fields in (None, []) else list(args.fields)

    if args.mode == "page":
        page_result = search_once(
            q=query,
            fields=fields,
            page=args.page,
            limit=args.limit,
            sort_field=args.sort_field,
            sort_order=args.sort_order,
        )
        payload = page_result.to_dict()
        _write_json_payload(payload, args.out, pretty=args.pretty)
    else:
        notices = iterate_all(
            q=query,
            fields=fields,
            batch_limit=args.limit,
            sort_field=args.sort_field,
            sort_order=args.sort_order,
        )
        _write_json_lines((notice.to_dict() for notice in notices), args.out, pretty=args.pretty)
    return 0


def handle_ungm_sync(args: argparse.Namespace) -> int:
    if args.dataset == "country":
        dataset = [entry.dict() for entry in sync_country_codes()]
    else:
        dataset = [entry.dict() for entry in sync_unspsc_segments()]
    _write_json_payload(dataset, args.out, pretty=args.pretty)
    print(f"Synchronised {len(dataset)} records", file=sys.stderr)
    return 0


def handle_ungm_link(args: argparse.Namespace) -> int:
    url = build_ungm_deeplink(args.countries, args.unspsc, args.keywords)
    print(url)
    return 0


def _write_json_payload(payload: object, output_path: str | None, pretty: bool) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    if output_path:
        Path(output_path).write_text(
            text if text.endswith("\n") else text + "\n", encoding="utf-8"
        )
    else:
        print(text)


def _write_json_lines(
    records: Iterable[dict], output_path: str | None, pretty: bool = False
) -> None:
    handle = open(output_path, "w", encoding="utf-8") if output_path else sys.stdout
    try:
        for record in records:
            line = json.dumps(record, ensure_ascii=False, indent=2 if pretty else None)
            if pretty and not line.endswith("\n"):
                handle.write(line + "\n")
            else:
                handle.write(line)
            if not pretty:
                handle.write("\n")
    finally:
        if handle is not sys.stdout:
            handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
