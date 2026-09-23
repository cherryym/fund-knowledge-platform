import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("private_restore", ROOT / "scripts/restore-private-transfer.py")
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/Windows/escape", "folder\\escape"])
def test_path_escape_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        restore.safe_path(tmp_path, path)


def test_actual_archive_restore_and_derived_cache(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    content = "完整文档与知识内容".encode()
    digest = hashlib.sha256(content).hexdigest()
    rows = [{"path": "data/原文.txt", "action": "COPIED_IDENTICAL", "sha256": digest, "mode": 0o600},
            {"path": "data/cache.part", "action": "DERIVED_EXACT_COPY", "sha256": digest, "source": "data/原文.txt"}]
    (package / "SOURCE-MANIFEST.json").write_text(json.dumps(rows, ensure_ascii=False))
    with zipfile.ZipFile(package / "01-documents.zip", "x") as archive:
        archive.writestr("project/data/原文.txt", content)
    for number in range(6):
        (package / f"info-{number}.txt").write_text("synthetic")
    files = sorted(package.iterdir())
    (package / "SHA256SUMS").write_text("".join(restore.digest(path) + "  " + path.name + "\n" for path in files))
    destination = tmp_path / "restored"
    restore.extract(package, destination, download_cache=True)
    assert (destination / "project/data/原文.txt").read_bytes() == content
    assert (destination / "project/data/cache.part").read_bytes() == content
    with pytest.raises(ValueError, match="DESTINATION_MUST_NOT_EXIST"):
        restore.extract(package, destination)
    (package / "info-0.txt").write_text("tampered")
    with pytest.raises(ValueError, match="CHECKSUM_FAILED"):
        restore.verify(package)
