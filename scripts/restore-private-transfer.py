"""Verify/extract the internal transfer; no DB import, login, network or model call."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


def safe_path(root, name):
    candidate = PurePosixPath(name)
    if (candidate.is_absolute() or PureWindowsPath(name).drive or "\\" in name
            or any(part in {"..", "."} for part in candidate.parts)):
        raise ValueError("UNSAFE_ARCHIVE_PATH")
    result = root.joinpath(*candidate.parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("ARCHIVE_PATH_ESCAPE")
    return result


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(package):
    count = 0
    for line in (package / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("INVALID_CHECKSUM")
        path = safe_path(package, name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError("CHECKSUM_FAILED: " + name)
        count += 1
        print(json.dumps({"verified": name}, ensure_ascii=False), flush=True)
    if count < 8:
        raise ValueError("INCOMPLETE_TRANSFER")
    return count


def extract(package, destination, *, download_cache=False):
    if destination.exists():
        raise ValueError("DESTINATION_MUST_NOT_EXIST")
    verify(package)
    manifest = json.loads((package / "SOURCE-MANIFEST.json").read_text())
    expected = {"project/" + row["path"]: row for row in manifest
                if row["action"] in {"COPIED_IDENTICAL", "SANITIZED_DATABASE", "SANITIZED_JSON", "SANITIZED_PLIST"}}
    destination.mkdir(mode=0o700, parents=True)
    seen = set()
    for path in sorted(package.glob("[0-9][0-9]-*.zip")):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.filename not in expected or member.filename in seen or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("UNEXPECTED_ARCHIVE_MEMBER")
                target = safe_path(destination, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 4 * 1024 * 1024)
                if digest(target) != expected[member.filename]["sha256"]:
                    raise ValueError("EXTRACTED_HASH_MISMATCH")
                if os.name != "nt":
                    os.chmod(target, expected[member.filename]["mode"] & 0o777)
                seen.add(member.filename)
        print(json.dumps({"extracted": path.name}, ensure_ascii=False), flush=True)
    if seen != set(expected):
        raise ValueError("EXTRACTED_FILE_SET_INCOMPLETE")
    if download_cache:
        for row in manifest:
            if row["action"] not in {"DERIVED_EXACT_COPY", "DERIVED_EXACT_RANGE"}:
                continue
            source = safe_path(destination / "project", row["source"])
            target = safe_path(destination / "project", row["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                remaining = row.get("length", source.stat().st_size)
                reader.seek(row.get("offset", 0))
                while remaining:
                    block = reader.read(min(remaining, 4 * 1024 * 1024))
                    if not block:
                        raise ValueError("DERIVED_RANGE_INCOMPLETE")
                    writer.write(block)
                    remaining -= len(block)
            if digest(target) != row["sha256"]:
                raise ValueError("DERIVED_CACHE_HASH_MISMATCH")
    links = [row for row in manifest if row["action"] == "LINK_METADATA"]
    (destination / "MACOS-LINKS-REFERENCE.json").write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "files_extracted": len(seen), "download_cache_reconstructed": download_cache,
                      "macos_links_reference_only": len(links), "database_imported": False, "model_calls": 0}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--with-download-cache", action="store_true")
    args = parser.parse_args()
    if args.extract:
        if not args.destination:
            parser.error("--extract requires a new --destination")
        extract(args.package.resolve(), args.destination.resolve(), download_cache=args.with_download_cache)
    else:
        print(json.dumps({"status": "PASS", "files_verified": verify(args.package.resolve())}))


if __name__ == "__main__":
    main()
