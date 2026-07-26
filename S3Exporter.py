"""
Write-only S3 access, scoped to ugc-assets/exported/.

The counterpart to S3Interpreter, which can only read. Splitting them is
deliberate: the inventory side physically cannot write, and this side
physically cannot write anywhere but the export prefix. Neither capability
is one import away from the other.

Three layers guard the same rule, because overwriting a source master is
unrecoverable and the library is irreplaceable:

  1. every key is checked here before any call is made;
  2. the instance role on server 3 permits PutObject only under
     ugc-assets/exported/ and denies it outright on the source prefixes;
  3. content_library_render_artifacts has
     CHECK (s3_key LIKE 'ugc-assets/exported/%').

This class also refuses to overwrite an object that already exists. A
render directory is immutable — a retry gets a new render id, so an
existing key means something is wrong rather than something needs
replacing.
"""

import hashlib
import mimetypes
import os

EXPORT_PREFIX = "ugc-assets/exported/"


class ExportPathError(Exception):
    """Raised when a key falls outside the export prefix."""


class S3Exporter:
    def __init__(self, bucket, region="us-east-1", prefix=EXPORT_PREFIX):
        self.bucket = bucket
        self.region = region
        self.prefix = prefix
        self._s3 = None

    def client(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    def check_key(self, key):
        """
        The guard every write passes through. Rejects anything outside the
        export prefix, and anything using traversal to climb out of it.
        """
        if not key or not key.startswith(self.prefix):
            raise ExportPathError(
                f"refusing to write outside {self.prefix}: {key!r}")
        if ".." in key.split("/"):
            raise ExportPathError(f"path traversal in key: {key!r}")
        return key

    def exists(self, key):
        from botocore.exceptions import ClientError
        try:
            self.client().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise

    def put_file(self, local_path, key, content_type=None, overwrite=False):
        """
        Uploads one artifact and returns its recorded facts.

        Returns the SHA-256 alongside the S3 response so the manifest can
        record what was actually uploaded rather than what was intended.
        """
        self.check_key(key)
        if not overwrite and self.exists(key):
            raise ExportPathError(
                f"{key} already exists; render directories are immutable "
                f"and a retry should use a new render id")

        digest = sha256_file(local_path)
        content_type = content_type or (
            mimetypes.guess_type(local_path)[0] or "application/octet-stream")

        with open(local_path, "rb") as handle:
            self.client().put_object(
                Bucket=self.bucket, Key=key, Body=handle,
                ContentType=content_type)

        return {
            "bucket_name": self.bucket,
            "s3_key": key,
            "size_bytes": os.path.getsize(local_path),
            "content_type": content_type,
            "checksum_sha256": digest,
        }

    def put_bytes(self, data, key, content_type="application/json", overwrite=False):
        self.check_key(key)
        if not overwrite and self.exists(key):
            raise ExportPathError(f"{key} already exists")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.client().put_object(Bucket=self.bucket, Key=key, Body=data,
                                 ContentType=content_type)
        return {
            "bucket_name": self.bucket,
            "s3_key": key,
            "size_bytes": len(data),
            "content_type": content_type,
            "checksum_sha256": hashlib.sha256(data).hexdigest(),
        }


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
