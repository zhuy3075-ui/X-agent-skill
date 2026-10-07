"""Start the Scweet MCP API over stdio or authenticated Streamable HTTP."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
# The outer mcp directory is not a Python package. The SDK keeps its own name.
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-db", action="store_true", help="Create PostgreSQL data tables and indexes, then exit.")
    parser.add_argument("--config", help="Path to non-secret ScweetConfig JSON.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if sys.version_info < (3, 10):
        print("Startup failed: MCP needs Python 3.10 or later. Use a supported Python interpreter.", file=sys.stderr)
        return 1
    try:
        from Scweet.config import ScweetConfig
        from scweet_mcp.service import create_server
        from scweet_mcp.store import StoreError, TweetStore
    except ImportError:
        print("Startup failed: Install Scweet and mcp/requirements.txt with this Python interpreter.", file=sys.stderr)
        return 1
    try:
        from scweet_mcp.settings import load_settings
        config = load_settings(args.config)
        if args.init_db:
            TweetStore(config).initialize()
            return 0
        server = create_server(config)
        logging.getLogger("scweet_mcp").info("Data service ready on %s", args.transport)
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            import uvicorn
            from scweet_mcp.http import http_app
            uvicorn.run(http_app(server, config), host=config.mcp_host, port=config.mcp_port, access_log=False)
    except StoreError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Service failed: Check the database and MCP connection, then restart.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
