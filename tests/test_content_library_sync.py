"""
Inventory behaviour against an in-process S3 reproducing the 2026-07-25
snapshot. No AWS credentials, no network, no database, no ffprobe.

The counts asserted here are the acceptance criteria from the design
package. The safety assertions — no write API on the S3 wrapper, and no
missing-marking after an incomplete listing — matter more than the counts.
"""

import hashlib
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.dirname(__file__))
from fixtures import inventory_snapshot  # noqa: E402

import ContentLibraryPaths as clpaths  # noqa: E402
import ContentLibrarySync as sync  # noqa: E402
from S3Interpreter import S3Interpreter  # noqa: E402

BUCKET = "big-city-travel-guide-clips"
PREFIX = "ugc-assets/"


@pytest.fixture
def aws_credentials(monkeypatch):
    # moto must never reach real AWS, and must never pick up an instance role.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                 "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def library(aws_credentials):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        expected = inventory_snapshot.populate(client, BUCKET)
        yield S3Interpreter(BUCKET, region="us-east-1"), expected


class TestListingCounts:
    def test_registers_exactly_73_media_objects(self, library):
        s3, expected = library
        objects, complete, counters = sync.list_source_objects(s3, PREFIX)
        assert len(objects) == expected["media"] == 73
        assert complete is True

    def test_folder_markers_are_skipped(self, library):
        s3, expected = library
        _, _, counters = sync.list_source_objects(s3, PREFIX)
        assert counters["markers"] == expected["markers"]

    def test_exported_is_never_ingested(self, library):
        s3, _ = library
        objects, _, counters = sync.list_source_objects(s3, PREFIX)
        assert counters["exported"] == 2
        assert not any(clpaths.is_exported(o["key"]) for o in objects)

    def test_non_media_is_skipped(self, library):
        s3, _ = library
        _, _, counters = sync.list_source_objects(s3, PREFIX)
        assert counters["non_media"] == 1

    def test_exported_reimport_can_be_enabled_explicitly(self, library):
        s3, _ = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX, allow_exported=True)
        # Only final.mp4 — manifest.json is still excluded as non-media.
        assert len(objects) == 74


class TestClassification:
    def test_asset_type_breakdown(self, library):
        s3, expected = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX)
        counts = {}
        for obj in objects:
            asset_type = clpaths.classify(obj["key"]).asset_type
            counts[asset_type] = counts.get(asset_type, 0) + 1
        assert counts == {"app": expected["app"], "broll": expected["broll"],
                          "reaction": expected["reaction"]}

    def test_every_key_is_recognized(self, library):
        s3, _ = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX)
        unrecognized = [o["key"] for o in objects
                        if not clpaths.classify(o["key"]).recognized]
        assert unrecognized == []

    def test_cities_resolve_to_new_york_and_tokyo(self, library):
        s3, _ = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX)
        cities = {}
        for obj in objects:
            city = clpaths.classify(obj["key"]).city_slug
            if city:
                cities[city] = cities.get(city, 0) + 1
        # Reactions carry no city; app and b-roll do.
        assert cities == {"new-york": 40, "tokyo": 2}


class TestDeduplication:
    def test_73_objects_are_59_unique_payloads(self, library):
        s3, expected = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX)
        checksums = {s3.checksum_sha256(o["key"]) for o in objects}
        assert len(checksums) == expected["unique_payloads"] == 59

    def test_31_reactions_are_17_performances(self, library):
        s3, expected = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX)
        reactions = [o for o in objects
                     if clpaths.classify(o["key"]).asset_type == "reaction"]
        assert len(reactions) == 31
        assert len({s3.checksum_sha256(o["key"]) for o in reactions}) == 17

    def test_aliases_contribute_a_second_emotion(self, library):
        s3, _ = library
        objects, _, _ = sync.list_source_objects(s3, PREFIX)
        by_checksum = {}
        for obj in objects:
            classified = clpaths.classify(obj["key"])
            if classified.asset_type != "reaction":
                continue
            by_checksum.setdefault(s3.checksum_sha256(obj["key"]), set()).update(
                classified.emotions)
        merged = [emotions for emotions in by_checksum.values() if len(emotions) > 1]
        # 14 duplicated payloads, each filed under two emotions.
        assert len(merged) == 14

    def test_checksum_matches_content(self, library):
        s3, _ = library
        key = inventory_snapshot.APP_KEYS[0]
        assert s3.checksum_sha256(key) == hashlib.sha256(key.encode()).hexdigest()

    def test_checksum_streams_rather_than_buffering(self, library):
        s3, _ = library
        chunks = list(s3.iter_object(inventory_snapshot.APP_KEYS[0], chunk_size=8))
        assert len(chunks) > 1


class TestIncompleteListingSafety:
    def test_pagination_failure_reports_incomplete(self, library, monkeypatch):
        s3, _ = library

        def explode(prefix):
            yield {"bucket": BUCKET, "key": "ugc-assets/app/new-york/guide/a.mov",
                   "size": 10, "etag": "x", "last_modified": None}
            raise RuntimeError("connection reset mid-pagination")

        monkeypatch.setattr(s3, "list_objects", explode)
        objects, complete, _ = sync.list_source_objects(s3, PREFIX)
        # The partial result is returned, but flagged — reconciliation keys
        # off this flag, so a truncated listing cannot condemn the library.
        assert complete is False
        assert len(objects) == 1

    def test_limit_also_marks_the_listing_incomplete(self, library):
        s3, _ = library
        objects, complete, _ = sync.list_source_objects(s3, PREFIX, limit=5)
        assert len(objects) == 5
        assert complete is False


class TestS3IsReadOnlyByConstruction:
    @pytest.mark.parametrize("forbidden", [
        "put_object", "upload_file", "upload_fileobj", "copy_object", "copy",
        "delete_object", "delete_objects", "create_multipart_upload",
        "put", "write", "save", "move", "rename",
    ])
    def test_no_write_method_exists(self, forbidden):
        # The guarantee is structural: a write cannot be reached by accident
        # because the capability is not present on the wrapper at all.
        assert not hasattr(S3Interpreter, forbidden)

    def test_public_surface_is_exactly_the_read_operations(self):
        surface = {name for name in vars(S3Interpreter) if not name.startswith("_")}
        assert surface == {"client", "list_objects", "head", "presign",
                           "iter_object", "checksum_sha256"}

    def test_no_source_object_is_modified_by_listing(self, library):
        s3, _ = library
        before = {o["key"]: o["etag"] for o in s3.list_objects(PREFIX)}
        sync.list_source_objects(s3, PREFIX)
        for obj in s3.list_objects(PREFIX):
            s3.checksum_sha256(obj["key"])
        after = {o["key"]: o["etag"] for o in s3.list_objects(PREFIX)}
        assert before == after


class TestDatabaseUrlParsing:
    def test_parses_rds_url(self):
        parsed = sync.parse_database_url(
            "postgresql://bigcity:pw@mediamixerdb.abc.us-east-1.rds.amazonaws.com:5432/mediamixer")
        assert parsed["user"] == "bigcity"
        assert parsed["database"] == "mediamixer"
        assert parsed["host"].startswith("mediamixerdb.")
        assert parsed["port"] == "5432"

    def test_percent_encoded_password_is_decoded(self):
        parsed = sync.parse_database_url(
            "postgresql://bigcity:pa%21ss@host:5432/mediamixer")
        assert parsed["password"] == "pa!ss"

    def test_asyncpg_scheme_is_normalized(self):
        parsed = sync.parse_database_url(
            "postgresql+asyncpg://bigcity:pw@host:5432/mediamixer")
        assert parsed["database"] == "mediamixer"

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(ValueError):
            sync.parse_database_url("mysql://u:p@host/db")


class TestTagParsing:
    """
    Tags are entered as free text in the tab, so parsing is the guard
    against a typo becoming a namespace nobody ever queries again.
    """

    def _parse(self):
        import os
        os.environ.setdefault("DATABASE_URL", "postgresql://u:p@h:5432/d")
        os.environ.setdefault("MEDIAMIXER_ADMIN_SECRET", "x")
        from api.main import parse_tags
        return parse_tags

    def test_namespaced_and_bare_forms(self):
        parse = self._parse()
        assert parse("emotion:surprised, hidden-gem") == [
            ("emotion", "surprised"), ("theme", "hidden-gem")]

    def test_case_and_spacing_are_normalized(self):
        assert self._parse()("  Theme:Hidden Gem  ") == [("theme", "hidden-gem")]

    def test_duplicates_collapse(self):
        assert self._parse()("theme:luxury, luxury") == [("theme", "luxury")]

    def test_an_unknown_namespace_is_refused(self):
        import pytest as _pytest
        from fastapi import HTTPException
        with _pytest.raises(HTTPException):
            self._parse()("emotoin:surprised")

    def test_empty_input_clears_rather_than_erroring(self):
        parse = self._parse()
        assert parse("") == [] and parse(None) == []
