"""Deterministic file-level CI partition; every test file belongs to one shard."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    files = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "backend/tests").rglob("*.py")
        if p.name.startswith("test_") or p.name.endswith("_test.py"))
    if not 0 <= args.shard < args.shards <= len(files): parser.error("Invalid shard range")
    selected = files[args.shard::args.shards]
    print(f"Shard {args.shard + 1}/{args.shards}: {len(selected)} of {len(files)} files; no skipped file filters", flush=True)
    if args.plan:
        print("\n".join(selected)); return 0
    return subprocess.call([sys.executable, "-m", "pytest", *selected, "-o", "addopts=", "-q"], cwd=ROOT)


if __name__ == "__main__": sys.exit(main())
