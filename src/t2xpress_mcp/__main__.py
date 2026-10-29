"""Entry point: run the MCP server over stdio."""

import logging

from .server import mcp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
