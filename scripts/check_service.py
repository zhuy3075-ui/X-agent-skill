"""Read-only stdio MCP health check. No collection or monitor is created."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv


async def check(config):
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    params = StdioServerParameters(command=sys.executable,
        args=[str(root / "mcp/server.py"), "--config", str(Path(config).resolve())], cwd=str(root), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {item.name for item in (await session.list_tools()).tools}
            required = {"collect_author", "continue_comments", "query_post_changes", "monitors_list", "save_research_note", "get_research_note", "query_research_notes", "rebuild_research_index"}
            if len(names) != 35 or not required <= names:
                raise RuntimeError("Unexpected MCP tool set; install the matching release.")
            result = await session.call_tool("monitors_list", {"limit": 1})
            if result.isError:
                raise RuntimeError("Database query failed; check schema and runtime permissions.")
            print(json.dumps({"healthy": True, "transport": "stdio", "tools": len(names), "database_read": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(check(args.config))
    except Exception:
        print("Health check failed. Check dependencies, database connection, schema and configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
