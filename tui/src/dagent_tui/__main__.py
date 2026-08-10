"""Command-line entrypoint for dagent-tui."""

from __future__ import annotations

import argparse
import os

from dagent_tui.app import DagentTui


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal client for the dagent API")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("DAGENT_API_URL", "http://127.0.0.1:8001"),
        help="dagent API base URL (default: %(default)s)",
    )
    args = parser.parse_args()
    DagentTui(api_url=args.api_url).run()


if __name__ == "__main__":
    main()
