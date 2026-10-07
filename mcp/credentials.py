"""Save a worker credential from a hidden prompt or stdin. Never use a token argument."""
import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scweet_mcp.credentials import save_credential
from scweet_mcp.store import StoreError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Private JSON path set in mcp_credentials_file")
    parser.add_argument("--credential-ref", required=True)
    parser.add_argument("--stdin", action="store_true", help="Read one token from stdin instead of the hidden prompt")
    args = parser.parse_args()
    try:
        token = sys.stdin.read(514).strip() if args.stdin else getpass.getpass("X auth token: ")
        print(json.dumps(save_credential(args.file, args.credential_ref, token)))
        return 0
    except (StoreError, OSError) as exc:
        message = str(exc) if isinstance(exc, StoreError) else "CREDENTIAL_FILE_ERROR: Check the private file path and permissions."
        print(json.dumps({"saved": False, "error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
