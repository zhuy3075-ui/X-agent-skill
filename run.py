"""Launch an installed component, loading private .env settings without overriding process values."""
import runpy
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"mcp", "worker", "init-db"}:
        print("Usage: python run.py {mcp|worker|init-db} [component options]", file=sys.stderr)
        return 2
    mode, arguments = sys.argv[1], sys.argv[2:]
    load_dotenv(ROOT / ".env", override=False)
    if "--config" not in arguments and (ROOT / "config.json").exists():
        arguments.extend(["--config", str(ROOT / "config.json")])
    filename = "worker.py" if mode == "worker" else "server.py"
    if mode == "init-db":
        arguments.append("--init-db")
    entry = ROOT / "mcp" / filename
    sys.path[:0] = [str(ROOT), str(ROOT / "mcp")]
    sys.argv = [str(entry), *arguments]
    runpy.run_path(str(entry), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
