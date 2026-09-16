"""Preserve launchd's existing app credentials; add only prepared local retrieval configuration."""
import json
import hashlib
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    environment=dict(os.environ)
    profile=ROOT/"data"/"retrieval-profile.json"
    if profile.is_file():
        sys.path.insert(0,str(ROOT/"backend"))
        from fund_kb.retrieval_profile import read_local_profile
        for name,value in read_local_profile(profile,ROOT/"data").items():
            environment["FKB_"+name.upper()]=json.dumps(value) if isinstance(value,bool) else str(value)
        environment["FKB_RETRIEVAL_PROFILE"]=str(profile)
        environment["FKB_RETRIEVAL_MODE"]="hybrid"
        registry_path=ROOT/"data"/"retrieval-profiles.json"
        if registry_path.is_file():
            environment["FKB_RETRIEVAL_PROFILES_FILE"]=str(registry_path)
        # A verified local release may select a separately probed text contract.
        # It contains no credentials and is tied to this exact retrieval profile.
        release_path=ROOT/"data"/"universal-v1"/"active-release.json"
        if release_path.is_file() and release_path.stat().st_size <= 16384:
            release=json.loads(release_path.read_text())
            if (release.get("retrieval_profile_sha256")==hashlib.sha256(profile.read_bytes()).hexdigest()
                    and environment.get("FKB_CODEX_TEXT_PROFILE_FILE")):
                text_profile=Path(release["codex_text_profile_file"])
                authorized_parent=Path(environment["FKB_CODEX_TEXT_PROFILE_FILE"]).parent
                if (not text_profile.is_absolute() or text_profile.is_symlink()
                        or text_profile.parent != authorized_parent
                        or text_profile.resolve().parent != authorized_parent.resolve()
                        or text_profile.stat().st_size > 1048576
                        or hashlib.sha256(text_profile.read_bytes()).hexdigest()!=release["codex_text_profile_sha256"]):
                    raise RuntimeError("VERIFIED_TEXT_PROFILE_MISMATCH")
                environment["FKB_CODEX_TEXT_PROFILE_FILE"]=str(text_profile)
    manifest=ROOT/"data"/"qdrant-native.json"
    if manifest.is_file():
        config=json.loads(manifest.read_text())
        if config.get("activation_ready"):
            if config.get("url")!="http://127.0.0.1:6333":
                raise RuntimeError("LOCAL_QDRANT_ENDPOINT_INVALID")
            key_file=Path(config["key_file"])
            if key_file.resolve()!=(ROOT/"data"/"private"/"qdrant-api.key").resolve() or key_file.is_symlink():
                raise RuntimeError("LOCAL_QDRANT_KEY_REFERENCE_INVALID")
            environment["FKB_QDRANT_URL"]=config["url"]
            environment["FKB_QDRANT_API_KEY"]=key_file.read_text().strip()
    command=[sys.executable,"-m","uvicorn","fund_kb.main:app","--host","127.0.0.1","--port","8765",
        "--no-access-log","--timeout-graceful-shutdown","10"]
    os.chdir(ROOT/"backend")
    os.execve(sys.executable,command,environment)


if __name__=="__main__":
    main()
