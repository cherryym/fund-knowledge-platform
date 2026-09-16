"""Select the explicitly provisioned, project-owned vector service for maintenance."""
import json
from pathlib import Path


def operator_vector_settings(settings):
    # Existing explicitly configured deployments take precedence over this local helper.
    if settings.qdrant_url:
        return settings
    root=Path(settings.storage_dir).resolve()
    manifest=root/"qdrant-native.json"
    if not manifest.is_file():
        return settings
    config=json.loads(manifest.read_text())
    if not config.get("activation_ready"):
        raise RuntimeError("VECTOR_SERVICE_MIGRATION_NOT_VERIFIED")
    key_file=Path(config["key_file"])
    if config.get("url") != "http://127.0.0.1:6333" or key_file.is_symlink() \
            or key_file.resolve() != root/"private"/"qdrant-api.key":
        raise RuntimeError("LOCAL_VECTOR_SERVICE_REFERENCE_INVALID")
    return settings.model_copy(update={"qdrant_url":config["url"],"qdrant_api_key":key_file.read_text().strip()})
