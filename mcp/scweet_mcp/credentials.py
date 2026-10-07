"""Local credential input. Secret values never enter jobs or tool results."""
import getpass
import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

from scweet_mcp.store import StoreError


def read_credentials(path):
    if not path:
        return {}
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
            raise ValueError()
        return values
    except (OSError, ValueError):
        raise StoreError("CREDENTIAL_FILE_ERROR: Read a valid private JSON credential file, or fix its access permissions.") from None


def private_file(path):
    if os.name == "nt":
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                 f"{getpass.getuser()}:F", "*S-1-5-18:F"],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise StoreError("CREDENTIAL_FILE_ERROR: Cannot restrict file access. Use a private writable directory.")
    else:
        path.chmod(0o600)


def save_credential(path, reference, token):
    if not re.fullmatch(r"SCWEET_X_[A-Z0-9_]{1,119}", reference):
        raise StoreError("INVALID_INPUT: credential_ref must start with SCWEET_X_ and use uppercase letters, digits or underscores.")
    if not isinstance(token, str) or not 16 <= len(token) <= 512 or any(c.isspace() for c in token):
        raise StoreError("INVALID_TOKEN_FORMAT: Supply one token of 16 to 512 characters without whitespace.")
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StoreError("CREDENTIAL_FILE_BUSY: Another writer holds the credential file. Retry after it finishes.") from None
    try:
        os.close(descriptor)
        values = read_credentials(path) if path.exists() else {}
        values[reference] = token
        with temporary.open("x", encoding="utf-8") as stream:
            private_file(temporary)
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return {"credential_ref": reference, "saved": True, "restart_required": False}
