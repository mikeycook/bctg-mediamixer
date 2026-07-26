"""
The one-time seed of server 2's tagging into the media database.

Two things here are worth testing hard. The array parser, because
hook_compatibility is the field most likely to be quietly corrupted by a
CSV round-trip. And slug agreement, because city resolution is a join
between a slug built in Python and one built in SQL — if they diverge,
nothing errors, cities just silently stop resolving.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "migration"))
import SeedMediaDatabase as seed  # noqa: E402
import ContentLibraryPaths as clpaths  # noqa: E402


def sql_slug(cityname):
    """
    Mirrors the expression in migration/ExportFromServer2.sh:

        btrim(regexp_replace(lower(btrim(cityname)), '[^a-z0-9]+', '-', 'g'), '-')

    Kept here so a change to either side fails a test rather than silently
    breaking resolution.
    """
    return re.sub(r"[^a-z0-9]+", "-", cityname.strip().lower()).strip("-")


class TestSlugAgreement:
    @pytest.mark.parametrize("cityname", [
        "New York", "Tokyo", "Los Angeles", "Barcelona", "Rome",
        "Washington, D.C.", "St. Petersburg", "San Diego",
        "  Paris  ", "Kuala Lumpur", "'s-Hertogenbosch",
    ])
    def test_python_and_sql_derive_the_same_slug(self, cityname):
        assert clpaths.slugify(cityname) == sql_slug(cityname)

    def test_new_york_matches_the_s3_folder(self):
        # The join that makes city resolution work: cities.cityname
        # 'New York' must produce the same slug as the folder in
        # ugc-assets/app/new-york/.
        from_cities = clpaths.slugify("New York")
        from_path = clpaths.classify(
            "ugc-assets/app/new-york/guide/x.mov").city_slug
        assert from_cities == from_path == "new-york"

    def test_punctuation_does_not_leave_a_trailing_hyphen(self):
        assert clpaths.slugify("Washington, D.C.") == "washington-d-c"


class TestHookArrayParsing:
    def test_quoted_multi_element(self):
        assert seed.parse_pg_array('{"Best Pizza","Hidden Gems"}') == \
            ["Best Pizza", "Hidden Gems"]

    def test_unquoted_single_element(self):
        assert seed.parse_pg_array("{HiddenGems}") == ["HiddenGems"]

    def test_comma_inside_quotes_is_not_a_separator(self):
        assert seed.parse_pg_array('{"Pizza, Pasta and More","Cheap Eats"}') == \
            ["Pizza, Pasta and More", "Cheap Eats"]

    def test_escaped_quote(self):
        assert seed.parse_pg_array('{"The \\"Best\\" Slice"}') == ['The "Best" Slice']

    def test_empty_forms_are_none(self):
        assert seed.parse_pg_array("{}") is None
        assert seed.parse_pg_array("") is None
        assert seed.parse_pg_array(None) is None

    def test_element_count_survives_the_round_trip(self):
        # The specific corruption to guard against: losing or splitting
        # elements would silently change an asset's hook compatibility.
        original = ["Best Pizza", "Hidden Gems", "Local Favorite"]
        encoded = "{" + ",".join(f'"{item}"' for item in original) + "}"
        assert seed.parse_pg_array(encoded) == original


class TestLegacyTypeMapping:
    @pytest.mark.parametrize("legacy,expected", [
        ("B-Roll", "broll"), ("b-roll", "broll"), ("App", "app"),
        ("Reaction", "reaction"), ("Music", "music"), ("CTA", "cta"),
    ])
    def test_display_values_normalize(self, legacy, expected):
        assert seed.ASSET_TYPE_FROM_LEGACY.get(legacy.strip().lower()) == expected

    def test_unknown_type_becomes_none_rather_than_a_guess(self):
        # asset_type has a CHECK constraint; an invented value would be
        # rejected at insert. Leaving it NULL lets review decide.
        assert seed.ASSET_TYPE_FROM_LEGACY.get("something else") is None


class TestBlankHandling:
    def test_empty_string_becomes_null(self):
        # CSV cannot distinguish empty string from NULL, and an empty
        # place_name would look like tagging that does not exist.
        assert seed.blank_to_none("") is None
        assert seed.blank_to_none("Ess-a-Bagel") == "Ess-a-Bagel"


class TestExportedColumnsMatchTheSourceSchema:
    def test_asset_columns_cover_the_irreplaceable_tagging(self):
        # These are the fields that cannot be regenerated from S3.
        for column in ("place_name", "hook_compatibility", "notes", "type",
                       "subtype", "category", "subcategory", "cityid", "asset_id"):
            assert column in seed.ASSET_COLUMNS

    def test_ids_are_carried_so_asset_ids_stay_meaningful(self):
        # asset_id is 'UGC-' || lpad(id, 5, '0'); reassigning ids would
        # leave UGC-00007 on some other row.
        assert "id" in seed.ASSET_COLUMNS
