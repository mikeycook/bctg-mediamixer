"""
Contract tests for the content library API.

The point of this service is that admin_ui/src/ContentLibrary.tsx does not
change: the browser keeps calling the same same-origin paths and server 2
forwards them here. That only holds while the paths, methods and response
shapes stay identical to what the admin backend exposed. These tests fail
if any of them drift.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
os.environ.setdefault("MEDIAMIXER_ADMIN_SECRET", "test-secret")

from api.main import app, _EDITABLE, _OPTION_FIELDS, _parse_hooks  # noqa: E402


def routes():
    found = {}
    for route in app.routes:
        if hasattr(route, "methods"):
            found.setdefault(route.path, set()).update(
                route.methods - {"HEAD", "OPTIONS"})
    return found


class TestPathsMatchTheAdminUi:
    @pytest.mark.parametrize("path,method", [
        ("/admin/content-library", "GET"),
        ("/admin/content-library/options", "GET"),
        ("/admin/content-library/sync", "POST"),
        ("/admin/content-library/{asset_pk}", "PUT"),
        ("/admin/content-library/{asset_pk}", "DELETE"),
        ("/admin/music-library", "GET"),
        ("/admin/music-library/options", "GET"),
        ("/admin/music-library/sync", "POST"),
        ("/admin/music-library/{track_pk}", "PUT"),
        ("/admin/music-library/{track_pk}", "DELETE"),
        ("/admin/renders/edited", "POST"),
        ("/admin/renders/{render_id}/alternatives", "GET"),
    ])
    def test_endpoint_exists(self, path, method):
        assert method in routes().get(path, set())

    def test_no_unexpected_admin_surface(self):
        # Asserted exactly rather than loosely: this service sits behind a
        # proxy with no inbound auth of its own beyond a shared secret, so
        # a new route appearing unnoticed is worth failing a build over.
        admin = {p for p in routes() if p.startswith("/admin")}
        assert admin == {
            # Content library
            "/admin/content-library",
            "/admin/content-library/options",
            "/admin/content-library/sync",
            "/admin/content-library/{asset_pk}",
            # Music library
            "/admin/music-library",
            "/admin/music-library/options",
            "/admin/music-library/sync",
            "/admin/music-library/{track_pk}",
            # Briefs and renders
            "/admin/templates",
            "/admin/render-cities",
            "/admin/render-topics",
            "/admin/renders",
            "/admin/renders/preview",
            "/admin/renders/edited",
            "/admin/renders/{render_id}",
            "/admin/renders/{render_id}/alternatives",
            # Tags
            "/admin/tags",
            # Images
            "/admin/image-templates",
            "/admin/image-library",
            "/admin/image-library/options",
            "/admin/image-library/sync",
            "/admin/image-library/{asset_pk}",
            "/admin/images",
            "/admin/images/compose",
            "/admin/images/compose-batch",
            "/admin/images/{image_id}",
        }

    def test_there_is_no_way_to_edit_a_recipe(self):
        # A brief is authored; a recipe is generated and immutable. An
        # endpoint that mutated one would break the guarantee that a video
        # is explainable from the recipe that produced it.
        for path, methods in routes().items():
            if "recipe" in path:
                assert methods <= {"GET"}
        assert "PUT" not in routes().get("/admin/renders/{render_id}", set())
        assert "DELETE" not in routes().get("/admin/renders/{render_id}", set())


class TestHookParsingMatchesTheExistingTab:
    """
    The tab posts a textarea whose placeholder is '"Best Pizza","Hidden
    Gems"'. Behaviour must match the admin backend's _parse_hooks or
    reviewers' input starts landing differently after the cutover.
    """

    def test_quoted_comma_separated(self):
        assert _parse_hooks('"Best Pizza","Hidden Gems"') == \
            ["Best Pizza", "Hidden Gems"]

    def test_plain_comma_separated(self):
        assert _parse_hooks("Best Pizza, Hidden Gems") == \
            ["Best Pizza", "Hidden Gems"]

    def test_list_passes_through(self):
        assert _parse_hooks(["Best Pizza"]) == ["Best Pizza"]

    def test_empty_becomes_none(self):
        assert _parse_hooks("") is None
        assert _parse_hooks("   ") is None
        assert _parse_hooks(None) is None

    def test_whitespace_is_trimmed(self):
        assert _parse_hooks('  "Best Pizza" ,  "Hidden Gems"  ') == \
            ["Best Pizza", "Hidden Gems"]


class TestEditableColumns:
    @pytest.mark.parametrize("column", [
        "place_name", "cityid", "country", "type", "subtype", "category",
        "subcategory", "duration", "hook_compatibility", "notes", "asset_id",
    ])
    def test_legacy_editable_columns_are_preserved(self, column):
        # Anything the existing tab could write must still be writable.
        assert column in _EDITABLE

    @pytest.mark.parametrize("column", [
        "status", "rights_status", "rights_source", "city_agnostic",
    ])
    def test_governance_columns_are_writable(self, column):
        # Without these, no asset can ever reach 'active' and nothing is
        # eligible for selection.
        assert column in _EDITABLE

    @pytest.mark.parametrize("column", [
        "id", "s3_key", "bucket_name", "checksum_sha256", "duration_ms",
        "width", "height", "orientation", "probe_data", "duplicate_of_asset_id",
        "first_seen_at", "last_seen_at",
    ])
    def test_measured_and_identity_columns_are_not_writable(self, column):
        # Measured facts come from ffprobe and S3, never from a form.
        assert column not in _EDITABLE


class TestOptionFields:
    def test_only_known_columns_are_offered(self):
        # The options endpoint interpolates the column name into SQL, so
        # the allowlist is what keeps that safe.
        for field in _OPTION_FIELDS:
            assert field.replace("_", "").isalnum()

    def test_legacy_option_fields_still_present(self):
        for field in ("type", "subtype", "category", "subcategory", "country"):
            assert field in _OPTION_FIELDS
