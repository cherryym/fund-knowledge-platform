"""Build a NEW candidate profile with complete role and zero-tool probes.

The root is a new offline candidate directory, not the live host/auth directory.
Installation is a separate coordinated step; existing profiles remain intact.
"""
import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--models", required=True)
    args = parser.parse_args()
    if args.root.exists() or args.root.is_symlink():
        parser.error("Use a NEW candidate root; never overwrite a running profile or auth directory.")
    command = [sys.executable, str(Path(__file__).with_name("probe-codex-instruction-channels.py")),
        "--profile-root", str(args.root), "--catalog", str(args.catalog),
        "--executable", args.executable, "--models", args.models]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
