"""Internal, complete local-project transfer with credential-free DB snapshots.

Only explicitly supplied source/output paths are used. Never decrypt credentials,
upload data, start services, call models, or modify the original database/files.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml

SECRET_FIELDS = {
    "apikey", "apikeyenv", "credentialciphertext", "accesstoken", "refreshtoken", "idtoken",
    "clientsecret", "password", "secretkey", "masterkey", "providermasterkey", "privatekey",
    "authorization", "cookie", "setcookie", "csrftoken", "tokenhash", "tokensha256", "rawtoken",
    "apikeyvalue", "s3secretkey", "qdrantapikey", "oidcclientsecret", "archivemasterkey",
}
IMMUTABLE_TABLES = {
    "users", "spaces", "space_members", "resources", "resource_grants", "blobs", "resource_versions",
    "content_blocks", "evidence_links", "relation_edges", "review_decisions", "releases", "uploads",
    "upload_parts", "run_evidence", "feedback", "issue_cases", "consultation_threads",
}
CREDENTIAL_NAMES = {"auth.json", "archive.key", "provider-master.key", "qdrant-api.key", "host.json",
                    "credentials.json", "credentials", ".netrc", ".npmrc", "settings.local.json"}
PART = re.compile(r"^(\d+)-(\d+)\.part$")
TEMP_SUFFIX = re.compile(r"\.[a-f0-9]{32}\.(?:assembling|downloading)$")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       default=lambda value: {"$bytes": base64.b64encode(value).decode()}) + "\n").encode()


def file_hash(path, offset=0, length=None):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        stream.seek(offset)
        remaining = length
        while remaining is None or remaining > 0:
            block = stream.read(4 * 1024 * 1024 if remaining is None else min(remaining, 4 * 1024 * 1024))
            if not block:
                if remaining:
                    raise ValueError("SOURCE_RANGE_INCOMPLETE")
                break
            result.update(block)
            if remaining is not None:
                remaining -= len(block)
    return result.hexdigest()


def normalized_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower()).removeprefix("fkb")


def scrub(value, changes, path=""):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            location = path + "/" + str(key)
            is_secret = normalized_key(key) in SECRET_FIELDS
            if normalized_key(key) == "authorization" and isinstance(item, str):
                # Preserve human audit authorization notes; remove actual HTTP
                # authorization values. This never grants target-side authority.
                is_secret = bool(re.match(r"^(?:Bearer|Basic|Digest|Token)\s+", item, re.I))
            if is_secret and isinstance(item, str) and item:
                result[key] = None
                changes[location] += 1
            else:
                result[key] = scrub(item, changes, location)
        if isinstance(result.get("state"), str) and result["state"] in {"AUTHENTICATED", "PENDING"} and "auth_epoch" in result:
            for key in ("account", "attempt_id", "active_job_id", "checked_at"):
                if key in result:
                    result[key] = None
            result.update(state="SIGNED_OUT", auth_epoch=0, state_source="transfer_signed_out")
            changes[path + "/oauth_state_reset"] += 1
        return result
    if isinstance(value, list):
        return [scrub(item, changes, path + "/*") for item in value]
    return value


def scrub_plist(value):
    """Sanitize launchd/environment metadata, without reading credential targets."""
    changes = Counter()
    cleaned = scrub(value, changes)
    def plist_safe(item):
        if item is None:
            return ""
        if isinstance(item, dict):
            return {k: plist_safe(v) for k, v in item.items()}
        if isinstance(item, list):
            return [plist_safe(v) for v in item]
        return item
    return plist_safe(cleaned), changes


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def table_proof(connection, table):
    columns = connection.execute("PRAGMA table_info(" + identifier(table) + ")").fetchall()
    primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
    names = [row[1] for row in columns]
    ordering = primary or names
    query = ("SELECT " + ",".join(map(identifier, names)) + " FROM " + identifier(table)
             + " ORDER BY " + ",".join(map(identifier, ordering)))
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        digest.update(json_bytes(list(row)))
        count += 1
    return {"rows": count, "sha256": digest.hexdigest(), "columns": names}


def snapshot_database(source, destination, *, export_directory=None):
    """Sensitive source pages exist only in memory; VACUUM creates clean output pages."""
    if destination.exists():
        raise ValueError("SNAPSHOT_EXISTS")
    source_uri = "file:" + quote(str(source.resolve()), safe="/") + "?mode=ro"
    # Offline files without a live WAL can be opened without touching SHM.
    wal = Path(str(source) + "-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        source_uri += "&immutable=1"
    memory = sqlite3.connect(":memory:")
    changes = Counter()
    now = datetime.now(UTC).isoformat()
    try:
        with sqlite3.connect(source_uri, uri=True) as original:
            original.backup(memory)
        memory.execute("PRAGMA secure_delete=ON")
        tables = [row[0] for row in memory.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if "runtime_policies" not in tables or "content_blocks" not in tables:
            raise ValueError("UNRECOGNIZED_DATABASE_REQUIRES_REVIEW")
        before = {table: table_proof(memory, table) for table in tables}
        integrity_before = memory.execute("PRAGMA foreign_key_check").fetchall()
        for table in tables:
            if table in IMMUTABLE_TABLES:
                continue
            rows = memory.execute("SELECT rowid,* FROM " + identifier(table))
            names = [item[0] for item in rows.description][1:]
            for row in rows.fetchall():
                values = dict(zip(names, row[1:]))
                updated = {}
                if table == "login_sessions":
                    updated = {"token_hash": None, "csrf_token": "TRANSFER_REAUTH_REQUIRED",
                               "revoked_at": values.get("revoked_at") or now}
                    changes["login_sessions/revoked"] += 1
                for name, raw in values.items():
                    if not isinstance(raw, str) or not raw.lstrip().startswith(("{", "[")):
                        continue
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    clean = scrub(parsed, changes, table + "/" + name)
                    if table == "runtime_policies" and name == "config":
                        prefix = values["name"].split(":")[0]
                        if prefix == "model-connection":
                            clean.update(credential_ciphertext=None, api_key_env=None, enabled=False,
                                         status="DISABLED", last_error_code="TRANSFER_REAUTH_REQUIRED")
                            changes["model_connections/disabled"] += 1
                        elif prefix == "model-oauth":
                            clean.update(state="SIGNED_OUT", account=None, auth_epoch=0, attempt_id=None,
                                         active_job_id=None, checked_at=None, state_source="transfer_signed_out",
                                         last_error_code=None)
                            changes["model_oauth/reset"] += 1
                        elif prefix == "agent-access":
                            clean.update(revoked_at=now, token_sha256=None)
                            changes["agent_access/revoked"] += 1
                    if clean != parsed:
                        updated[name] = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
                if updated:
                    memory.execute("UPDATE " + identifier(table) + " SET "
                                   + ",".join(identifier(key) + "=?" for key in updated) + " WHERE rowid=?",
                                   [*updated.values(), row[0]])
        memory.commit()
        if memory.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("SANITIZED_DATABASE_INTEGRITY_FAILURE")
        if memory.execute("PRAGMA foreign_key_check").fetchall() != integrity_before:
            raise ValueError("SANITIZED_FOREIGN_KEY_FAILURE")
        after = {table: table_proof(memory, table) for table in tables}
        for table in tables:
            if before[table]["rows"] != after[table]["rows"]:
                raise ValueError("ROW_COUNT_CHANGED_" + table)
            if table in IMMUTABLE_TABLES and before[table] != after[table]:
                raise ValueError("BUSINESS_CONTENT_CHANGED_" + table)
        destination.parent.mkdir(parents=True, exist_ok=True)
        memory.execute("VACUUM INTO ?", (str(destination),))
        os.chmod(destination, 0o600)
        if export_directory:
            export_directory.mkdir(parents=True, exist_ok=True)
            for table in tables:
                columns = [row[1] for row in memory.execute("PRAGMA table_info(" + identifier(table) + ")")]
                with (export_directory / (table + ".jsonl")).open("xb") as output:
                    for row in memory.execute("SELECT " + ",".join(map(identifier, columns)) + " FROM " + identifier(table)):
                        output.write(json_bytes(dict(zip(columns, row))))
            (export_directory / "schema.sql").write_text("\n".join(
                row[0] + ";" for row in memory.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")), encoding="utf-8")
        return {"tables_before": before, "tables_after": after, "changes": dict(changes),
                "integrity": "PASS", "foreign_keys": "PASS" if not integrity_before else "SOURCE_ISSUES_PRESERVED",
                "source_foreign_key_issues": len(integrity_before), "source_modified": False}
    finally:
        memory.close()


def credentials_reason(relative):
    parts, name = relative.parts, relative.name.lower()
    if parts[:2] in {("data", "private"), ("data", "oauth"), ("data", "identities")} or ".codex" in parts:
        return "CREDENTIAL_DIRECTORY"
    if any(part.startswith("codex-text-v3-candidate-") for part in parts) and "runs" in parts:
        return "CODEX_IDENTITY_RUNTIME"
    if any(part in {".http-test-work", ".security-test-work", ".e2e-test-work", ".protocol-test-work"} for part in parts):
        return "AUTHENTICATION_TEST_RUNTIME"
    if name in CREDENTIAL_NAMES or name.endswith(".key") or name.startswith(".env") and "example" not in name:
        return "CREDENTIAL_FILE"
    if name in {"archive.enc", "auth.enc", "credentials.enc"} or name.endswith(".auth"):
        return "ENCRYPTED_LOGIN_ARCHIVE"
    if name.endswith((".sqlite-shm", ".sqlite-wal", ".sqlite3-shm", ".sqlite3-wal", ".sqlite-journal")):
        return "DATABASE_SNAPSHOT_COMPANION"
    return None


def bucket(relative):
    parts = relative.parts
    if "universal-models" in parts:
        if "qwen3-reranker-4b" in parts:
            return "08-model-qwen3-reranker-4b"
        if "qwen3-embedding-4b" in parts:
            return "04-model-qwen3-embedding-4b"
        if "bge-m3" in parts:
            return "05-model-bge-m3"
        if "bge-reranker-v2-m3" in parts:
            return "06-model-bge-reranker-v2-m3"
    if ".venv" in parts or "node_modules" in parts or "__pycache__" in parts:
        return "07-macos-dependencies-reference"
    if len(parts) > 1 and parts[:2] == ("data", "qdrant-server"):
        return "03-qdrant-index"
    if parts[0] == "data":
        return "02-business-data-and-history"
    return "01-project-source-design-documents"


def source_inventory(source, excluded_output):
    result = []
    for directory, directories, files in os.walk(source, followlinks=False):
        base = Path(directory)
        directories[:] = [name for name in directories if (base / name).resolve() != excluded_output
                          and not ((base / name).parent == excluded_output.parent and name.startswith("private-transfer-"))]
        for name in list(directories):
            if (base / name).is_symlink():
                files.append(name)
                directories.remove(name)
        for name in files:
            path = base / name
            relative = path.relative_to(source)
            info = path.lstat()
            result.append({"path": relative.as_posix(), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                           "mode": stat.S_IMODE(info.st_mode), "symlink": path.is_symlink()})
    return sorted(result, key=lambda row: row["path"])


def derived_model_file(path, source):
    if "universal-models" not in path.parts:
        return None
    match = PART.fullmatch(path.name)
    if match and path.parent.parent.name == ".parts":
        canonical = path.parent.parent.parent / path.parent.name
        start, end = map(int, match.groups())
        if canonical.is_file() and end > start and end - start == path.stat().st_size and end <= canonical.stat().st_size:
            actual = file_hash(path)
            if actual == file_hash(canonical, start, end - start):
                return {"action": "DERIVED_EXACT_RANGE", "source": canonical.relative_to(source).as_posix(),
                        "offset": start, "length": end - start, "sha256": actual}
    if TEMP_SUFFIX.search(path.name):
        canonical = path.with_name(TEMP_SUFFIX.sub("", path.name))
        if canonical.is_file() and canonical.stat().st_size == path.stat().st_size:
            actual = file_hash(path)
            if actual == file_hash(canonical):
                return {"action": "DERIVED_EXACT_COPY", "source": canonical.relative_to(source).as_posix(), "sha256": actual}
    return None


def progress(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)


def prepare(source, output, application):
    if output.exists() or not source.is_dir() or not application.is_file():
        raise ValueError("NEW_OUTPUT_AND_VALID_SOURCE_REQUIRED")
    if (source / ".git").exists() or (output / ".git").exists():
        raise ValueError("BUSINESS_PACKAGE_MUST_NOT_TARGET_A_GIT_CHECKOUT")
    files = source_inventory(source, output)
    output.mkdir(mode=0o700, parents=True)
    (output / "SOURCE-INVENTORY.json").write_bytes(json_bytes(files))
    stage = output / "prepared"
    stage.mkdir(mode=0o700)
    (output / "application").mkdir()
    shutil.copy2(application, output / "application" / application.name)
    manifest, databases, counts = [], {}, Counter()
    start = last_progress = time.monotonic()
    for index, item in enumerate(files):
        relative = Path(item["path"])
        path, target = source / relative, stage / "project" / relative
        row = {**item, "archive": bucket(relative) + ".zip"}
        reason = credentials_reason(relative)
        if reason:
            row.update(action="EXCLUDED_CREDENTIAL", reason=reason)
        elif item["symlink"]:
            row.update(action="LINK_METADATA", target=str(path.readlink()))
        elif not path.is_file():
            row.update(action="NONREGULAR_METADATA")
        elif (derived := derived_model_file(path, source)):
            row.update(derived)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as probe:
                header = probe.read(64)
            if b"PRIVATE KEY-----" in header:
                row.update(action="EXCLUDED_CREDENTIAL", reason="PRIVATE_KEY_CONTENT")
            elif header.startswith(b"SQLite format 3\x00"):
                proof = snapshot_database(path, target,
                    export_directory=output / "logical-database" if relative.as_posix() == "data/fund_kb.sqlite3" else None)
                databases[item["path"]] = proof
                row.update(action="SANITIZED_DATABASE", sha256=file_hash(target))
            elif path.suffix.lower() == ".plist" and path.stat().st_size < 4 * 1024 * 1024:
                try:
                    clean, changes = scrub_plist(plistlib.loads(path.read_bytes()))
                except Exception as exc:
                    raise ValueError("PLIST_REQUIRES_SECURITY_REVIEW: " + item["path"]) from None
                target.write_bytes(plistlib.dumps(clean) if changes else path.read_bytes())
                row.update(action="SANITIZED_PLIST" if changes else "COPIED_IDENTICAL", sha256=file_hash(target))
                if changes:
                    row["redacted_fields"] = dict(changes)
            elif relative.parts[0] == "data" and path.suffix.lower() in {".json", ".jsonl", ".ndjson", ".yaml", ".yml"} and path.stat().st_size < 100 * 1024 * 1024 \
                    and ".venv" not in relative.parts and "node_modules" not in relative.parts \
                    and "universal-models" not in relative.parts and relative.parts[:2] != ("data", "qdrant-server"):
                changes = Counter()
                try:
                    original = path.read_text(encoding="utf-8-sig")
                    if path.suffix.lower() in {".json", ".yaml", ".yml"}:
                        data = json.loads(original) if path.suffix.lower() == ".json" else yaml.safe_load(original)
                        cleaned = scrub(data, changes)
                        content = json_bytes(cleaned) if changes else path.read_bytes()
                    else:
                        content = b"".join(json_bytes(scrub(json.loads(line), changes)) for line in original.splitlines() if line.strip())
                        if not changes:
                            content = path.read_bytes()
                    target.write_bytes(content)
                    row.update(action="SANITIZED_JSON" if changes else "COPIED_IDENTICAL", sha256=file_hash(target))
                    if changes:
                        row["redacted_fields"] = dict(changes)
                except (ValueError, UnicodeError, yaml.YAMLError):
                    shutil.copy2(path, target)
                    row.update(action="COPIED_IDENTICAL", sha256=file_hash(target))
            else:
                shutil.copy2(path, target)
                row.update(action="COPIED_IDENTICAL", sha256=file_hash(target))
            # Compare to source, not just to what the copy operation produced.
            if row.get("action") == "COPIED_IDENTICAL" and row["sha256"] != file_hash(path):
                raise ValueError("COPY_HASH_MISMATCH")
        if not reason:
            current = path.lstat()
            if (current.st_size, current.st_mtime_ns) != (item["bytes"], item["mtime_ns"]):
                raise ValueError("SOURCE_CHANGED_DURING_TRANSFER: " + item["path"])
        manifest.append(row)
        counts[row["action"]] += 1
        if time.monotonic() - last_progress > 15:
            progress(phase="prepare", complete=index + 1, total=len(files), seconds=round(time.monotonic() - start), actions=dict(counts))
            last_progress = time.monotonic()
    stable_before = [row for row in files if not credentials_reason(Path(row["path"]))]
    stable_after = [row for row in source_inventory(source, output) if not credentials_reason(Path(row["path"]))]
    if stable_after != stable_before:
        raise ValueError("SOURCE_INVENTORY_CHANGED_DURING_TRANSFER")
    (output / "SOURCE-MANIFEST.json").write_bytes(json_bytes(manifest))
    (output / "DATABASE-VALIDATION.json").write_bytes(json_bytes(databases))
    summary = {"phase": "PREPARED_NOT_SEALED", "source_files": len(files), "actions": dict(counts),
               "original_file_bytes": sum(item["bytes"] for item in files), "source_modified": False,
               "credential_decryption": False, "production_import_executed": False, "docker_images_included": False}
    (output / "TRANSFER.json").write_bytes(json_bytes(summary))
    progress(**summary)


def seal(output):
    security = json.loads((output / "SECURITY-REVIEW.json").read_text())
    if security.get("status") != "PASS":
        raise ValueError("SECURITY_REVIEW_REQUIRED")
    manifest = json.loads((output / "SOURCE-MANIFEST.json").read_text())
    stage = output / "prepared"
    archives = {}
    try:
        last_progress = time.monotonic()
        for index, row in enumerate(manifest):
            if row["action"] not in {"COPIED_IDENTICAL", "SANITIZED_DATABASE", "SANITIZED_JSON", "SANITIZED_PLIST"}:
                continue
            path = stage / "project" / row["path"]
            if file_hash(path) != row["sha256"]:
                raise ValueError("STAGED_FILE_CHANGED")
            archive_name = row["archive"]
            if archive_name not in archives:
                archives[archive_name] = zipfile.ZipFile(output / archive_name, "x", allowZip64=True,
                                                        compression=zipfile.ZIP_DEFLATED, compresslevel=1)
            compress = zipfile.ZIP_STORED if archive_name.startswith(("04-", "05-", "06-", "08-")) else zipfile.ZIP_DEFLATED
            archives[archive_name].write(path, arcname="project/" + row["path"], compress_type=compress, compresslevel=1)
            if time.monotonic() - last_progress > 15:
                progress(phase="archive", processed=index + 1, total=len(manifest))
                last_progress = time.monotonic()
    finally:
        for archive in archives.values():
            archive.close()
    checksums = []
    for archive in sorted(output.glob("*.zip")):
        with zipfile.ZipFile(archive) as check:
            if check.testzip():
                raise ValueError("ARCHIVE_CRC_FAILED")
        checksums.append({"file": archive.name, "bytes": archive.stat().st_size, "sha256": file_hash(archive)})
        progress(phase="archive_verified", **checksums[-1])
    application_files = list((output / "application").glob("*.zip"))
    for path in application_files:
        checksums.append({"file": "application/" + path.name, "bytes": path.stat().st_size, "sha256": file_hash(path)})
    summary = json.loads((output / "TRANSFER.json").read_text())
    summary.update(phase="ARCHIVED", archives=checksums, archive_total_bytes=sum(item["bytes"] for item in checksums))
    (output / "TRANSFER.json").write_bytes(json_bytes(summary))
    for path in sorted(output.rglob("*")):
        relative = path.relative_to(output)
        if (not path.is_file() or relative.parts[0] in {"prepared", "work", "application"}
                or path.suffix == ".zip" or path.name == "SHA256SUMS"):
            continue
        checksums.append({"file": relative.as_posix(), "bytes": path.stat().st_size, "sha256": file_hash(path)})
    (output / "SHA256SUMS").write_text("".join(item["sha256"] + "  " + item["file"] + "\n" for item in checksums), encoding="utf-8")
    progress(phase="ARCHIVED", archives=len(checksums), total_bytes=summary["archive_total_bytes"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "seal"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--application", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.source.resolve(), args.output.resolve(), args.application.resolve())
    else:
        seal(args.output.resolve())


if __name__ == "__main__":
    main()
