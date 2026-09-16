"""Check Git-indexed release content without reading runtime data or credentials."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {"data", "runtime", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "output", "tmp", "test-results", ".playwright-cli"}
PRIVATE_NAMES = {"auth.json", "host.json", "archive.key"}
PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".db", ".sqlite", ".sqlite3", ".safetensors", ".onnx", ".gguf", ".log"}


def main():
    listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    names = [name for name in listed if name]
    errors = []
    if not names: errors.append("No tracked files: stage and review the release first")
    for name in names:
        relative = Path(name); path = ROOT / relative
        if (any(part in FORBIDDEN_PARTS for part in relative.parts) or relative.name in PRIVATE_NAMES
                or relative.suffix in PRIVATE_SUFFIXES or (relative.name.startswith(".env") and relative.name != ".env.example")):
            errors.append(f"Forbidden release path: {name}"); continue
        if path.is_symlink(): errors.append(f"Symlink needs review: {name}"); continue
        if not path.is_file(): errors.append(f"Tracked file missing: {name}"); continue
        if path.stat().st_size > 5 * 1024 * 1024: errors.append(f"Large file needs review: {name}")
        try: body = path.read_text(encoding="utf-8")
        except UnicodeError: continue
        if re.search(r"/(?:Users|home)/[^/\s\"']+/", body):
            errors.append(f"Personal absolute path: {name}")
        if relative.suffix == ".md":
            for link in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", body):
                if re.match(r"^(?:[a-z]+:|#)", link): continue
                target = link.split("#", 1)[0].split(" ", 1)[0].strip("<>")
                if target and not (path.parent / target).exists(): errors.append(f"Broken local link in {name}: {target}")
    for error in errors: print(error)
    print(f"{'FAIL' if errors else 'PASS'}: {len(names)} tracked files; {len(errors)} findings; no model calls")
    return bool(errors)


if __name__ == "__main__": sys.exit(main())
