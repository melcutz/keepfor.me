# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

from keepfor.utils.url import extract_url

# Documented CLI exit codes
EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_USAGE = 2
EXIT_AUTH = 3


class CLIArgumentParser(argparse.ArgumentParser):
    """Custom parser that routes usage errors to stderr and exits with status 2."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"error: {message}\n")
        sys.exit(EXIT_USAGE)


def _build_parser() -> CLIArgumentParser:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--url",
        "-u",
        dest="base_url",
        default=None,
        help="Base URL of Keepfor instance (or env KEEPFOR_URL, default: http://localhost:8000)",
    )
    common_parser.add_argument(
        "--token",
        default=None,
        help="Keepfor Personal Access Token (or env KEEPFOR_TOKEN)",
    )
    common_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON to stdout on success",
    )

    parser = CLIArgumentParser(
        prog="keepfor",
        parents=[common_parser],
        description="Command-line interface for Keepfor.me personal library.",
        epilog=(
            "Exit codes:\n"
            "  0: Success (OK)\n"
            "  1: General failure (server 5xx, network error, runtime error)\n"
            "  2: Usage or validation error (invalid arguments, invalid URL)\n"
            "  3: Authentication error (missing or invalid token, 401/403)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # save subcommand
    save_parser = subparsers.add_parser(
        "save",
        parents=[common_parser],
        help="Save a URL to your Keepfor library",
        description="Save a URL to your Keepfor library.",
    )
    save_parser.add_argument("url", help="HTTP or HTTPS URL to save")
    save_parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        default=[],
        help="Tag to attach to the item (can be specified multiple times)",
    )
    save_parser.add_argument("--title", default=None, help="Optional title override")

    # search subcommand
    search_parser = subparsers.add_parser(
        "search",
        parents=[common_parser],
        help="Search your saved library",
        description="Search your saved library.",
    )
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument(
        "--tag",
        default=None,
        help="Filter results by tag",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum results to return (default: 10)",
    )
    search_parser.add_argument(
        "--mode",
        choices=["hybrid", "keyword", "semantic"],
        default="hybrid",
        help="Search mode (default: hybrid)",
    )

    # get subcommand
    get_parser = subparsers.add_parser(
        "get",
        parents=[common_parser],
        help="Get a saved item by its ID",
        description="Get a saved item by its ID.",
    )
    get_parser.add_argument("item_id", help="Item ID")

    # list subcommand
    list_parser = subparsers.add_parser(
        "list",
        parents=[common_parser],
        help="List recent items from your library",
        description="List recent items from your library.",
    )
    list_parser.add_argument(
        "--tag",
        default=None,
        help="Filter items by tag",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum items to return (default: 20)",
    )

    return parser


def _send_request(
    method: str,
    endpoint_url: str,
    token: str,
    data: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    """Execute an HTTP request with Keepfor authentication headers."""
    headers = {
        "User-Agent": "keepfor-cli/1.0",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body_bytes = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(
        endpoint_url,
        data=body_bytes,
        headers=headers,
        method=method,
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        status_code = response.status
        content_type = response.headers.get("Content-Type", "")
        raw_body = response.read().decode("utf-8")
        if "application/json" in content_type:
            try:
                parsed_data = json.loads(raw_body)
            except json.JSONDecodeError:
                parsed_data = raw_body
        else:
            parsed_data = raw_body
        return status_code, parsed_data


def _extract_error_detail(body: Any) -> str:
    """Extract human-readable error detail from an API response body."""
    if isinstance(body, dict):
        if "detail" in body:
            return str(body["detail"])
        if "message" in body:
            return str(body["message"])
        if "error" in body:
            err = body["error"]
            if isinstance(err, dict) and "message" in err:
                return str(err["message"])
            return str(err)
    if isinstance(body, str) and body.strip():
        return body.strip()
    return "Unknown error"


def _handle_request_exception(exc: Exception) -> int:
    """Map request exceptions to CLI exit codes and write errors to stderr."""
    if isinstance(exc, urllib.error.HTTPError):
        raw_err = exc.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(raw_err)
            detail = _extract_error_detail(err_json)
        except Exception:
            detail = raw_err.strip() or exc.reason

        if exc.code in (401, 403):
            sys.stderr.write(
                f"error: authentication failed (HTTP {exc.code}): {detail}\n"
            )
            return EXIT_AUTH
        if exc.code in (400, 422):
            sys.stderr.write(f"error: validation failed (HTTP {exc.code}): {detail}\n")
            return EXIT_USAGE
        if exc.code == 404:
            sys.stderr.write(f"error: not found (HTTP 404): {detail}\n")
            return EXIT_GENERAL
        if exc.code >= 500:
            sys.stderr.write(f"error: server error (HTTP {exc.code}): {detail}\n")
            return EXIT_GENERAL

        sys.stderr.write(f"error: HTTP {exc.code}: {detail}\n")
        return EXIT_GENERAL

    if isinstance(exc, urllib.error.URLError):
        sys.stderr.write(f"error: network error: {exc.reason}\n")
        return EXIT_GENERAL

    if isinstance(exc, TimeoutError):
        sys.stderr.write("error: request timed out\n")
        return EXIT_GENERAL

    sys.stderr.write(f"error: unexpected failure: {exc}\n")
    return EXIT_GENERAL


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint logic returning the exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    url_val = args.base_url or os.environ.get("KEEPFOR_URL", "http://localhost:8000")
    base_url = url_val.rstrip("/")
    token_val = (
        args.token if args.token is not None else os.environ.get("KEEPFOR_TOKEN", "")
    )
    token = token_val.strip()

    if not token:
        sys.stderr.write(
            "error: missing authentication token "
            "(provide --token or set KEEPFOR_TOKEN)\n"
        )
        return EXIT_AUTH

    cmd = args.command

    if cmd == "save":
        url = args.url.strip()
        clean_url = extract_url(url)
        if not clean_url:
            sys.stderr.write(
                "error: invalid URL: URL must be a valid HTTP or HTTPS address\n"
            )
            return EXIT_USAGE

        # Flatten tags if passed multiple times or comma-separated
        tags: list[str] = []
        for t in args.tags:
            for sub in t.split(","):
                clean_tag = sub.strip()
                if clean_tag and clean_tag not in tags:
                    tags.append(clean_tag)

        payload: dict[str, Any] = {"url": url}
        if tags:
            payload["tags"] = tags
        if args.title:
            payload["title"] = args.title.strip()

        try:
            _, result = _send_request(
                "POST",
                f"{base_url}/api/save",
                token=token,
                data=payload,
            )
        except Exception as exc:
            return _handle_request_exception(exc)

        if args.json:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
        else:
            title = result.get("title") or result.get("url")
            item_id = result.get("id")
            sys.stdout.write(f"Saved: {title} (id: {item_id})\n")
        return EXIT_OK

    elif cmd == "search":
        query = args.query.strip()
        if not query:
            sys.stderr.write("error: search query cannot be empty\n")
            return EXIT_USAGE

        payload = {
            "query": query,
            "limit": args.limit,
            "mode": args.mode,
        }
        if args.tag:
            payload["tag"] = args.tag.strip()

        try:
            _, result = _send_request(
                "POST",
                f"{base_url}/api/search",
                token=token,
                data=payload,
            )
        except Exception as exc:
            return _handle_request_exception(exc)

        if args.json:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
        else:
            if not isinstance(result, list) or not result:
                sys.stdout.write("No items found.\n")
            else:
                for item in result:
                    item_id = item.get("id", "")
                    title = item.get("title") or item.get("url") or "Untitled"
                    url = item.get("url", "")
                    sys.stdout.write(f"- [{item_id}] {title} ({url})\n")
        return EXIT_OK

    elif cmd == "get":
        item_id = args.item_id.strip()
        if not item_id:
            sys.stderr.write("error: item_id cannot be empty\n")
            return EXIT_USAGE

        try:
            _, result = _send_request(
                "GET",
                f"{base_url}/api/items/{urllib.parse.quote(item_id)}",
                token=token,
            )
        except Exception as exc:
            return _handle_request_exception(exc)

        if args.json:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
        else:
            title = result.get("title") or result.get("url") or "Untitled"
            sys.stdout.write(
                f"{title} (id: {result.get('id')})\n"
                f"URL: {result.get('url')}\n"
                f"Status: {result.get('status')}\n"
            )
        return EXIT_OK

    elif cmd == "list":
        query_params: dict[str, Any] = {"limit": args.limit}
        if args.tag:
            query_params["tag"] = args.tag.strip()
        qs = urllib.parse.urlencode(query_params)

        try:
            _, result = _send_request(
                "GET",
                f"{base_url}/api/items?{qs}",
                token=token,
            )
        except Exception as exc:
            return _handle_request_exception(exc)

        if args.json:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
        else:
            if not isinstance(result, list) or not result:
                sys.stdout.write("No items in library.\n")
            else:
                for item in result:
                    item_id = item.get("id", "")
                    title = item.get("title") or item.get("url") or "Untitled"
                    url = item.get("url", "")
                    sys.stdout.write(f"- [{item_id}] {title} ({url})\n")
        return EXIT_OK

    sys.stderr.write(f"error: unknown command '{cmd}'\n")
    return EXIT_USAGE


def main() -> int:
    """CLI entrypoint."""
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
