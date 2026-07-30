import hashlib

# Content types by extension, so a presigned URL opens inline in the browser
# (plays / shows) instead of downloading. Clips uploaded without a type land
# as application/octet-stream, which browsers always download; overriding the
# response content-type on the presigned URL fixes that.
_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".json": "application/json", ".srt": "text/plain", ".txt": "text/plain",
}


def _content_type_for(key):
    lower = (key or "").lower()
    for ext, ctype in _CONTENT_TYPES.items():
        if lower.endswith(ext):
            return ctype
    return None


class S3Interpreter:
    """
    A read-only utility class for one S3 bucket.

    This class deliberately has no put, copy, delete, or multipart method.
    The pipeline must never modify a source object, and the most reliable
    way to guarantee that is for the capability not to exist in the code
    at all. Generated media is uploaded by the render worker through a
    separate, explicitly scoped path — not through this class.

    Credentials come from the EC2 instance role. Nothing here reads a key,
    a profile, or a credentials file.
    """

    def __init__(self, bucket, region="us-east-1"):
        self.bucket = bucket
        self.region = region
        self._s3 = None

    def client(self):
        """
        Lazily builds the boto3 client, matching the admin backend's
        pattern so importing this module never requires boto3 to be
        installed or credentials to be resolvable.
        """
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=self.region)
        return self._s3

    def list_objects(self, prefix):
        """
        Yields one dict per object beneath prefix.

        This is a generator, so an exception part way through pagination
        propagates to the caller mid-iteration. That is intentional: the
        caller must be able to tell a complete listing from a partial one,
        because marking assets missing after a partial listing would
        condemn the live library.
        """
        paginator = self.client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield {
                    "bucket": self.bucket,
                    "key": obj["Key"],
                    "size": obj.get("Size", 0),
                    "etag": (obj.get("ETag") or "").strip('"'),
                    "last_modified": obj.get("LastModified"),
                }

    def head(self, key):
        """Object metadata, including VersionId when versioning is enabled."""
        resp = self.client().head_object(Bucket=self.bucket, Key=key)
        return {
            "size": resp.get("ContentLength", 0),
            "etag": (resp.get("ETag") or "").strip('"'),
            "content_type": resp.get("ContentType"),
            "last_modified": resp.get("LastModified"),
            "version_id": resp.get("VersionId"),
        }

    def presign(self, key, expires=3600):
        """
        A time-limited GET URL. Used to hand objects to ffprobe without
        downloading them. Never log the result — it grants read access.
        """
        params = {
            "Bucket": self.bucket, "Key": key,
            # inline so the browser plays/shows it in a tab rather than
            # downloading; the content-type override handles objects stored
            # as application/octet-stream.
            "ResponseContentDisposition": "inline",
        }
        ctype = _content_type_for(key)
        if ctype:
            params["ResponseContentType"] = ctype
        return self.client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires)

    def iter_object(self, key, chunk_size=1024 * 1024):
        """Yields the object body in chunks. Never materializes it whole."""
        resp = self.client().get_object(Bucket=self.bucket, Key=key)
        body = resp["Body"]
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    def checksum_sha256(self, key, chunk_size=1024 * 1024):
        """
        SHA-256 over the whole object, streamed.

        This is the only operation that reads an object end to end. It
        stays incremental because the render host also carries the
        database and its scratch disk, and because the largest reaction
        clips are tens of MiB.
        """
        digest = hashlib.sha256()
        for chunk in self.iter_object(key, chunk_size=chunk_size):
            digest.update(chunk)
        return digest.hexdigest()
