"""Initialize or rebuild a local Markdown research library without database or X access."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "mcp")]
from scweet_mcp.knowledge import KnowledgeLibrary
from scweet_mcp.store import StoreError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="The same absolute folder as mcp_knowledge_base_dir")
    parser.add_argument("--templates", action="store_true", help="Copy missing generic templates without changing existing files")
    args = parser.parse_args()
    try:
        library = KnowledgeLibrary(args.root)
        result = library.initialize()
        if args.templates:
            source = ROOT / "mcp/skills/x-agent-skill/assets/knowledge-templates"
            if not source.is_dir():
                raise StoreError("KB_TEMPLATE_ERROR: Install the complete repository to copy the skill templates.")
            for path in source.glob("*.md"):
                target = library.safe_path(Path("_模板") / path.name)
                if not target.exists():
                    shutil.copy2(path, target)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (StoreError, OSError) as exc:
        print(str(exc) if isinstance(exc, StoreError) else "KB_IO_ERROR: Check the library directory permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
