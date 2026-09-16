"""Private object storage. Keys are identifiers, never filesystem paths or URLs."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock


class StorageError(RuntimeError):
    pass


def safe_filename(filename: str) -> str:
    """Preserve a readable extension without letting an upload choose directories."""
    name = re.sub(r"[^\w.\-]+", "_", filename.replace("\\", "/").split("/")[-1], flags=re.UNICODE)
    name = name.strip(". ")[:180]
    return name or "document.bin"


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or len(key) > 1024:
        raise ValueError("INVALID_STORAGE_KEY")
    if "\\" in key or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("INVALID_STORAGE_KEY")
    if PurePosixPath(key).is_absolute() or PureWindowsPath(key).drive:
        raise ValueError("INVALID_STORAGE_KEY")
    if any(p in ("", ".", "..") for p in key.split("/")):
        raise ValueError("INVALID_STORAGE_KEY")
    return key


class Storage:
    """Atomic local writes or private S3 objects; originals/parts are immutable.

    No public/signed object URL is returned. The HTTP layer must authorize every
    read. S3 local_path files live only for this Storage object's lifetime.
    """

    def __init__(self, settings):
        self.backend = getattr(settings, "storage_backend", "local")
        self.root = Path(settings.storage_dir).expanduser().resolve()
        self._temp = None
        self._lock = RLock()
        self._closed = False
        self.client = None
        if self.backend == "local":
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif self.backend == "s3":
            import boto3
            import botocore.session

            self.bucket = getattr(settings, "s3_bucket", None)
            if not self.bucket:
                raise StorageError("S3_BUCKET_REQUIRED")
            prefix = (getattr(settings, "s3_prefix", "") or "").strip("/")
            self.prefix = validate_key(prefix) + "/" if prefix else ""
            credential_mode = getattr(settings, "s3_credential_mode", "explicit")
            access = getattr(settings, "s3_access_key", None) or getattr(settings, "s3_access_key_id", None)
            secret = getattr(settings, "s3_secret_key", None) or getattr(settings, "s3_secret_access_key", None)
            token = getattr(settings, "s3_session_token", None)
            core = botocore.session.Session()
            core.set_config_variable("config_file", "/dev/null")
            core.set_config_variable("credentials_file", "/dev/null")
            core.set_config_variable("profile", None)
            if credential_mode == "explicit":
                if not access or not secret:
                    raise StorageError("S3_EXPLICIT_CREDENTIALS_REQUIRED")
                core.set_credentials(access, secret, token)
                session = boto3.Session(botocore_session=core)
            elif credential_mode == "workload":
                # Deliberately omit profile, environment, process, SSO and assume-role
                # providers. Workload auth supports only container/instance identity.
                from botocore.credentials import (
                    ContainerProvider,
                    CredentialResolver,
                    InstanceMetadataProvider,
                )
                from botocore.utils import InstanceMetadataFetcher

                core.register_component("credential_provider", CredentialResolver([
                    ContainerProvider(), InstanceMetadataProvider(iam_role_fetcher=InstanceMetadataFetcher(
                        timeout=1, num_attempts=1)),
                ]))
                session = boto3.Session(botocore_session=core)
            else:
                raise StorageError("S3_CREDENTIAL_MODE_INVALID")
            self.client = session.client(
                "s3", endpoint_url=getattr(settings, "s3_endpoint_url", None),
                region_name=getattr(settings, "s3_region", None),
            )
            self._temp = tempfile.TemporaryDirectory(prefix="fund-kb-s3-")
        else:
            raise StorageError("UNSUPPORTED_STORAGE_BACKEND")

    def _key(self, key: str) -> str:
        if self._closed:
            raise StorageError("STORAGE_CLOSED")
        return validate_key(key)

    def _path(self, key: str) -> Path:
        path = self.root.joinpath(*self._key(key).split("/"))
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("INVALID_STORAGE_KEY")
        # Reject even inward symlinks: immutable paths must identify one object.
        current = self.root
        for part in key.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError("SYMLINK_STORAGE_KEY")
        return path

    @staticmethod
    def _immutable(key: str) -> bool:
        return key.startswith(("blobs/", "uploads/"))

    def write_bytes(self, key: str, data: bytes) -> str:
        key = self._key(key)
        if not isinstance(data, bytes):
            raise TypeError("storage accepts bytes")
        with self._lock:
            if self.backend == "s3":
                from botocore.exceptions import ClientError

                options = {"IfNoneMatch": "*"} if self._immutable(key) else {}
                try:
                    self.client.put_object(
                        Bucket=self.bucket, Key=self.prefix + key, Body=data,
                        ContentType="application/octet-stream", **options,
                    )
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") not in ("PreconditionFailed", "412"):
                        raise
                    if self.read_bytes(key) != data:
                        raise StorageError("IMMUTABLE_OBJECT_CONFLICT") from None
                return key
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Recheck newly created components before creating the temp file.
            path = self._path(key)
            fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if self._immutable(key):
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        if self.read_bytes(key) != data:
                            raise StorageError("IMMUTABLE_OBJECT_CONFLICT") from None
                else:
                    os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return key

    def read_bytes(self, key: str) -> bytes:
        key = self._key(key)
        if self.backend == "local":
            return self._path(key).read_bytes()
        response = self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)
        try:
            return response["Body"].read()
        finally:
            response["Body"].close()

    def exists(self, key: str) -> bool:
        key = self._key(key)
        if self.backend == "local":
            return self._path(key).is_file()
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise  # AccessDenied/network failure is not proof of absence.

    def delete(self, key: str) -> None:
        key = self._key(key)
        with self._lock:
            if self.backend == "local":
                self._path(key).unlink(missing_ok=True)
            else:
                self.client.delete_object(Bucket=self.bucket, Key=self.prefix + key)
                local = Path(self._temp.name) / hashlib.sha256(key.encode()).hexdigest()
                local.unlink(missing_ok=True)

    def local_path(self, key: str) -> Path:
        key = self._key(key)
        if self.backend == "local":
            path = self._path(key)
            if not path.is_file():
                raise FileNotFoundError(key)
            return path
        with self._lock:
            data = self.read_bytes(key)
            path = Path(self._temp.name) / hashlib.sha256(key.encode()).hexdigest()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            return path

    def close(self) -> None:
        with self._lock:
            if self._temp is not None:
                self._temp.cleanup()
            if self.client is not None:
                self.client.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
