"""
Render worker: source verification, disk preconditions, and export layout.

The encode itself needs ffmpeg and is exercised on the render host. What is
tested here is everything around it — the checks that decide whether an
encode should happen at all, and the guarantee that its output can only
land under the export prefix.
"""

import os
import shutil
import sys
import tempfile

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import RenderWorker as worker  # noqa: E402
import VideoRenderer as vr  # noqa: E402
from S3Exporter import S3Exporter, ExportPathError, sha256_file  # noqa: E402
from S3Interpreter import S3Interpreter  # noqa: E402

BUCKET = "big-city-travel-guide-clips"


@pytest.fixture
def aws_credentials(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def workdir():
    path = tempfile.mkdtemp(prefix="mmtest_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def recipe_for(payloads):
    """One clip per payload, checksums matching the bytes."""
    import hashlib
    timeline, at = [], 0
    for index, payload in enumerate(payloads):
        timeline.append({
            "asset_id": f"UGC-{index:05d}", "asset_pk": index + 1,
            "s3_key": f"ugc-assets/b-roll/food/pizza/new-york/{index}.mov",
            "s3_version_id": None,
            "checksum_sha256": hashlib.sha256(payload).hexdigest(),
            "role": "hook_visual", "source_in_ms": 0, "source_out_ms": 3000,
            "timeline_in_ms": at, "transform": {}, "audio_policy": {"mode": "keep"},
        })
        at += 3000
    return {"recipe_version": 1, "template": {"id": "t", "version": 1}, "brief": {},
            "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "timeline": timeline, "total_duration_ms": at, "captions": [],
            "audio_mix": {}, "renderer": {"name": "ffmpeg"}}


class TestSourceVerification:
    def test_matching_checksums_download_cleanly(self, aws_credentials, workdir):
        payloads = [b"clip-one-bytes", b"clip-two-bytes"]
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=BUCKET)
            recipe = recipe_for(payloads)
            for clip, payload in zip(recipe["timeline"], payloads):
                client.put_object(Bucket=BUCKET, Key=clip["s3_key"], Body=payload)

            paths = worker.download_sources(
                S3Interpreter(BUCKET), recipe, workdir)
            assert len(paths) == 2
            assert all(os.path.exists(p) for p in paths)
            for path, payload in zip(paths, payloads):
                assert open(path, "rb").read() == payload

    def test_changed_source_aborts_the_render(self, aws_credentials, workdir):
        # The object was replaced after cataloguing. Rendering from it would
        # produce a video whose manifest names footage it did not use.
        payloads = [b"original-bytes"]
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=BUCKET)
            recipe = recipe_for(payloads)
            client.put_object(Bucket=BUCKET, Key=recipe["timeline"][0]["s3_key"],
                              Body=b"DIFFERENT-BYTES-ENTIRELY")

            with pytest.raises(worker.RenderFailure) as excinfo:
                worker.download_sources(S3Interpreter(BUCKET), recipe, workdir)
            assert excinfo.value.code == "source_missing"
            assert "changed since it was catalogued" in excinfo.value.detail

    def test_verification_can_be_disabled_explicitly(self, aws_credentials, workdir):
        payloads = [b"original-bytes"]
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=BUCKET)
            recipe = recipe_for(payloads)
            client.put_object(Bucket=BUCKET, Key=recipe["timeline"][0]["s3_key"],
                              Body=b"DIFFERENT")
            paths = worker.download_sources(
                S3Interpreter(BUCKET), recipe, workdir, verify=False)
            assert len(paths) == 1


class TestDiskPreconditions:
    def test_sufficient_space_passes(self, workdir):
        assert worker.check_free_space(workdir, needed=1024) > 0

    def test_insufficient_space_refuses_to_start(self, workdir):
        # Failing up front beats failing mid-encode with a part-written file
        # on a disk now too full to clean up comfortably — this host also
        # carries the API.
        with pytest.raises(worker.RenderFailure) as excinfo:
            worker.check_free_space(workdir, needed=10 ** 18)
        assert excinfo.value.code == "render_failed"
        assert "refusing to start" in excinfo.value.detail


class TestExportRoundTrip:
    def test_artifacts_land_under_the_export_prefix(self, aws_credentials, workdir):
        import datetime as dt
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            exporter = S3Exporter(BUCKET)
            render_id = vr.new_render_id()
            prefix = vr.export_prefix(render_id, "dev", dt.datetime(2026, 7, 27))

            local = os.path.join(workdir, "final.mp4")
            open(local, "wb").write(b"pretend-this-is-an-mp4")

            result = exporter.put_file(local, prefix + "final.mp4")
            assert result["s3_key"].startswith("ugc-assets/exported/dev/")
            assert result["checksum_sha256"] == sha256_file(local)
            assert result["size_bytes"] == os.path.getsize(local)

    def test_a_render_directory_is_immutable(self, aws_credentials, workdir):
        # A retry gets a new render id, so an existing key means something
        # is wrong rather than something needs replacing.
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            exporter = S3Exporter(BUCKET)
            key = "ugc-assets/exported/dev/2026/07/27/RND-X/final.mp4"
            local = os.path.join(workdir, "final.mp4")
            open(local, "wb").write(b"first")

            exporter.put_file(local, key)
            with pytest.raises(ExportPathError) as excinfo:
                exporter.put_file(local, key)
            assert "immutable" in str(excinfo.value)

    def test_writing_over_a_source_master_is_refused(self, aws_credentials, workdir):
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            exporter = S3Exporter(BUCKET)
            local = os.path.join(workdir, "final.mp4")
            open(local, "wb").write(b"x")
            with pytest.raises(ExportPathError):
                exporter.put_file(
                    local, "ugc-assets/b-roll/food/pizza/new-york/lindustrie_001.mov")

    def test_manifest_and_validation_upload_as_json(self, aws_credentials):
        import json as jsonlib
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            exporter = S3Exporter(BUCKET)
            key = "ugc-assets/exported/dev/2026/07/27/RND-X/manifest.json"
            result = exporter.put_bytes(jsonlib.dumps({"schema_version": 1}), key)
            assert result["content_type"] == "application/json"
            assert result["checksum_sha256"]


class TestBriefParsing:
    def test_inline_brief_fields(self):
        import ContentLibrarySelect as sel
        brief = sel.VideoBrief.from_dict({
            "cityid": "CIT-00000000002", "topic": "pizza",
            "target_duration_ms": 25000, "seed": "abc"})
        assert brief.cityid == "CIT-00000000002"
        assert brief.target_duration_ms == 25000
        assert brief.seed == "abc"

    def test_defaults_are_sane(self):
        import ContentLibrarySelect as sel
        brief = sel.VideoBrief.from_dict({})
        assert brief.template_id == "city-discovery-v1"
        assert brief.target_duration_ms == 20000
        assert brief.environment == "dev"

    def test_city_id_alias_is_accepted(self):
        # The design package's example brief uses city_id; the column is
        # cityid. Accept both rather than failing on a hyphen of history.
        import ContentLibrarySelect as sel
        assert sel.VideoBrief.from_dict({"city_id": "CIT-1"}).cityid == "CIT-1"


class TestDatabaseUrl:
    def test_asyncpg_scheme_is_normalized(self):
        parsed = worker.parse_database_url(
            "postgresql+asyncpg://bigcity:pw@host:5432/mediamixer")
        assert parsed["database"] == "mediamixer"
        assert parsed["user"] == "bigcity"

    def test_bad_scheme_raises(self):
        with pytest.raises(ValueError):
            worker.parse_database_url("mysql://u:p@h/d")
