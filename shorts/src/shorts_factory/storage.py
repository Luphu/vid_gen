"""Artifact storage: S3-compatible in production, local directory for dev/tests.

Files are streamed from/to disk; nothing loads a whole video into memory.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Protocol

from .config import Settings


class ArtifactStore(Protocol):
    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None: ...
    def get_file(self, key: str, dest: Path) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete_older_than(self, prefix: str, max_age_s: float) -> int: ...


def _safe_key(key: str) -> str:
    if key.startswith("/") or ".." in Path(key).parts or not key:
        raise ValueError(f"unsafe storage key: {key!r}")
    return key


class LocalStore:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = (self.root / _safe_key(key)).resolve()
        if self.root not in p.parents:
            raise ValueError(f"key escapes storage root: {key!r}")
        return p

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        shutil.copyfile(path, tmp)
        os.replace(tmp, dst)

    def get_file(self, key: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._p(key), dest)
        return dest

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self.root
        return sorted(str(p.relative_to(base)) for p in base.rglob("*")
                      if p.is_file() and str(p.relative_to(base)).startswith(prefix))

    def delete_older_than(self, prefix: str, max_age_s: float) -> int:
        n, cutoff = 0, time.time() - max_age_s
        for key in self.list(prefix):
            p = self._p(key)
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        return n


class S3Store:
    def __init__(self, s: Settings):
        import boto3
        from botocore.config import Config

        s.require("s3_bucket")
        self.bucket, self.prefix = s.s3_bucket, s.s3_prefix
        self.client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url or None,
            region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key_id.get_secret_value() if s.s3_access_key_id else None,
            aws_secret_access_key=s.s3_secret_access_key.get_secret_value() if s.s3_secret_access_key else None,
            config=Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 5, "mode": "standard"},
                          s3={"addressing_style": "path"}),
        )

    def _k(self, key: str) -> str:
        return self.prefix + _safe_key(key)

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
        self.client.upload_file(str(path), self.bucket, self._k(key), ExtraArgs={"ContentType": content_type})

    def get_file(self, key: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, self._k(key), str(dest))
        return dest

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list(self, prefix: str) -> list[str]:
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                          Prefix=self._k(prefix)):
            out += [o["Key"][len(self.prefix):] for o in page.get("Contents", [])]
        return out

    def delete_older_than(self, prefix: str, max_age_s: float) -> int:
        n, cutoff = 0, time.time() - max_age_s
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                          Prefix=self._k(prefix)):
            for o in page.get("Contents", []):
                if o["LastModified"].timestamp() < cutoff:
                    self.client.delete_object(Bucket=self.bucket, Key=o["Key"])
                    n += 1
        return n


def make_store(s: Settings) -> ArtifactStore:
    return S3Store(s) if s.storage_backend == "s3" else LocalStore(s.storage_local_dir)
