"""Install the MCP, worker and one skill from a fresh checkout without storing credentials."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def merge_missing(saved, template):
    for key, value in template.items():
        if key not in saved:
            saved[key] = value
        elif isinstance(value, dict) and isinstance(saved[key], dict):
            merge_missing(saved[key], value)
    return saved


def install_skills(destination):
    destination = destination.resolve()
    source, target = ROOT / "mcp/skills/x-agent-skill", destination / "x-agent-skill"
    template = json.loads((source / "config.json").read_text(encoding="utf-8"))
    legacy = [destination / name for name in ("social-monitoring", "scweet-monitoring-router")]
    saved = {}
    for config in (target / "config.json", legacy[0] / "config.json"):
        if config.exists():
            preferences = json.loads(config.read_text(encoding="utf-8-sig"))
            if not isinstance(preferences, dict):
                raise ValueError("Skill config must be a JSON object; keep the original and repair it before installation.")
            merge_missing(saved, preferences)
    merge_missing(saved, template)
    for item in source.rglob("*"):
        if not item.is_file() or item.name == "config.json":
            continue
        result = target / item.relative_to(source)
        result.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, result)
    updated = target / "config.json.install"
    updated.write_text(json.dumps(saved, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    updated.replace(target / "config.json")
    # Retire only the two known predecessors; retain their complete contents outside discovery.
    existing = [folder for folder in legacy if folder.exists()]
    if existing:
        backup = destination.parent / "skill-backups" / ("x-agent-skill-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        backup.mkdir(parents=True)
        for folder in existing:
            if folder.is_symlink() or folder.resolve().parent != destination:
                raise ValueError("Legacy skill must be a direct directory in the selected skill root.")
            shutil.move(str(folder), str(backup / folder.name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, help="Custom skill directory; default is CODEX_HOME/skills")
    parser.add_argument("--skip-skills", action="store_true")
    parser.add_argument("--no-register-codex", action="store_true", help="Write client configuration without changing Codex")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required.")
    stage = "create the Python environment"
    try:
        environment = ROOT / ".venv"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        stage = "install dependencies"
        subprocess.run([str(python), "-m", "pip", "install", "-e", str(ROOT)], check=True)
        stage = "write local configuration"
        config_path = ROOT / "config.json"
        if not config_path.exists():
            config = json.loads((ROOT / "mcp/config.local.example.json").read_text(encoding="utf-8"))
            config["mcp_credentials_file"] = str(ROOT / "private/worker-env.json")
            config["mcp_artifact_dir"] = str(ROOT / "outputs/mcp")
            config["mcp_manifest_db_path"] = str(ROOT / "outputs/mcp/manifest.sqlite3")
            config_path.write_text(json.dumps(config, indent=2)+"\n", encoding="utf-8")
        (ROOT / "private").mkdir(exist_ok=True)
        if not (ROOT / ".env").exists():
            shutil.copy2(ROOT / ".env.example", ROOT / ".env")
            if os.name != "nt":
                (ROOT / ".env").chmod(0o600)
        command = [str(python), str(ROOT / "run.py"), "mcp", "--config", str(config_path)]
        client = {"mcpServers": {"scweet-monitoring": {"command": command[0], "args": command[1:]}}}
        (ROOT / "mcp-client.local.json").write_text(json.dumps(client, indent=2)+"\n", encoding="utf-8")
        if not args.skip_skills:
            stage = "update the skill and preserve preference configuration"
            destination = args.skills_dir or Path(os.environ.get("CODEX_HOME") or str(Path.home()/".codex")) / "skills"
            install_skills(destination.expanduser())
        codex = shutil.which("codex")
        registered = False
        if codex and not args.no_register_codex:
            stage = "register the MCP client"
            listing = subprocess.run([codex, "mcp", "list", "--json"], capture_output=True, text=True, check=True)
            current = json.loads(listing.stdout)
            if not any(item.get("name") == "scweet-monitoring" for item in current):
                subprocess.run([codex, "mcp", "add", "scweet-monitoring", "--", *command], check=True)
                registered = True
            else:
                print("An existing scweet-monitoring registration was preserved; use mcp-client.local.json to update it.")
        print(json.dumps({"installed": True, "skills_installed": not args.skip_skills,
                          "codex_registered": registered, "client_config": "mcp-client.local.json"}))
        print("Next: configure PostgreSQL in .env; run the database init, save an X token with the hidden prompt, then start the worker.")
        return 0
    except Exception as exc:
        print(f"Installation failed while attempting to {stage} ({type(exc).__name__}). Check this step and retry; existing credentials are not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
