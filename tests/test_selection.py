"""
Selection: eligibility, ranking, determinism, and the failure modes.

The tests that matter most here are the negative ones. A selector that
picks a good clip is pleasant; a selector that quietly substitutes a New
York restaurant into a Tokyo video, or uses the same reaction twice under
two emotion folders, produces something wrong that looks right.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ContentLibrarySelect as sel  # noqa: E402


class FakeDb:
    """Stands in for PostgresInterpreter, filtering rows the way the SQL does."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def execute_query_as_dict(self, sql, params=None):
        self.queries.append((sql, params))
        params = params or {}
        out = []
        for row in self.rows:
            if row.get("status", "active") != "active":
                continue
            if row.get("rights_status", "owned") not in params.get("rights", []):
                continue
            if row.get("duplicate_of_asset_id") is not None:
                continue
            if row.get("missing_since") is not None:
                continue
            if row.get("duration_ms") is None or row.get("checksum_sha256") is None:
                continue
            if row.get("asset_type") not in params.get("types", []):
                continue
            if row["duration_ms"] < params.get("min_ms", 0):
                continue
            if "cityid" in params and not (
                    row.get("cityid") == params["cityid"] or row.get("city_agnostic")):
                continue
            if "city_slug" in params and not (
                    row.get("city_slug") == params["city_slug"] or row.get("city_agnostic")):
                continue
            out.append(dict(row))
        return out


def asset(pk, asset_type, **kw):
    base = {
        "id": pk, "asset_id": f"UGC-{pk:05d}", "s3_key": f"ugc-assets/x/{pk}.mov",
        "s3_version_id": None, "checksum_sha256": f"sha{pk}", "asset_type": asset_type,
        "category": None, "subcategory": None, "place_name": None,
        "cityid": "CIT-00000000002", "city_slug": "new-york", "city_agnostic": False,
        "duration_ms": 9000, "width": 1080, "height": 1920, "orientation": "portrait",
        "has_audio": True, "frame_rate": 30, "quality_score": None,
        "hook_compatibility": None, "shot_type": None, "last_seen_at": None,
        "status": "active", "rights_status": "owned",
        "duplicate_of_asset_id": None, "missing_since": None,
    }
    base.update(kw)
    return base


def full_library():
    """Enough clips to satisfy city-discovery-v1 for New York pizza."""
    rows = [asset(1, "app", duration_ms=9000), asset(2, "app", duration_ms=9000)]
    for i, place in enumerate(["L'Industrie", "Roberta's", "Joe's", "Lucali"], start=10):
        rows.append(asset(i, "broll", category="food", subcategory="pizza",
                          place_name=place, duration_ms=8000))
    return rows


NY = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza", seed="test")


class TestHappyPath:
    def test_produces_a_full_recipe(self):
        recipe = sel.select(FakeDb(full_library()), NY)
        assert len(recipe["timeline"]) == 5
        assert recipe["canvas"] == {"width": 1080, "height": 1920, "fps": 30}
        assert recipe["template"]["id"] == "city-discovery-v1"

    def test_timeline_is_contiguous(self):
        recipe = sel.select(FakeDb(full_library()), NY)
        at = 0
        for clip in recipe["timeline"]:
            assert clip["timeline_in_ms"] == at
            at += clip["source_out_ms"] - clip["source_in_ms"]
        assert recipe["total_duration_ms"] == at

    def test_lands_near_the_target_duration(self):
        recipe = sel.select(FakeDb(full_library()), sel.VideoBrief(
            cityid="CIT-00000000002", topic="pizza", target_duration_ms=20000, seed="t"))
        assert abs(recipe["total_duration_ms"] - 20000) <= 1500

    def test_every_clip_carries_its_checksum_for_lineage(self):
        recipe = sel.select(FakeDb(full_library()), NY)
        assert all(c["checksum_sha256"] for c in recipe["timeline"])

    def test_trim_stays_inside_the_source(self):
        recipe = sel.select(FakeDb(full_library()), NY)
        by_pk = {a["id"]: a for a in full_library()}
        for clip in recipe["timeline"]:
            assert clip["source_out_ms"] <= by_pk[clip["asset_pk"]]["duration_ms"]
            assert clip["source_in_ms"] < clip["source_out_ms"]


class TestCityLeakage:
    def test_tokyo_brief_does_not_borrow_new_york_footage(self):
        # The whole library is New York. A Tokyo brief must fail rather
        # than produce a plausible-looking Tokyo video of New York.
        tokyo = sel.VideoBrief(cityid="CIT-TOKYO", topic="ramen", seed="t")
        with pytest.raises(sel.SelectionError) as excinfo:
            sel.select(FakeDb(full_library()), tokyo)
        assert excinfo.value.code == "insufficient_assets"

    def test_city_agnostic_assets_cross_cities(self):
        rows = full_library()
        rows.append(asset(99, "broll", city_agnostic=True, cityid=None,
                          city_slug=None, category="food"))
        candidates = sel.eligible_candidates(
            FakeDb(rows), sel.VideoBrief(cityid="CIT-TOKYO"),
            sel.Slot("x", ["broll"], 1000, 2000, 3000))
        assert [c["id"] for c in candidates] == [99]


class TestFailClosed:
    @pytest.mark.parametrize("mutation", [
        {"status": "needs_review"},
        {"status": "rejected"},
        {"rights_status": "unknown"},
        {"rights_status": "restricted"},
        {"rights_status": "expired"},
        {"missing_since": "2026-07-01"},
        {"checksum_sha256": None},
        {"duration_ms": None},
        {"duplicate_of_asset_id": 1},
    ])
    def test_ineligible_assets_are_excluded(self, mutation):
        rows = [asset(1, "app", **mutation)]
        candidates = sel.eligible_candidates(
            FakeDb(rows), sel.VideoBrief(),
            sel.Slot("app_demonstration", ["app"], 1000, 2000, 3000))
        assert candidates == []

    def test_landscape_is_excluded_from_a_vertical_canvas(self):
        rows = [asset(1, "broll", orientation="landscape", width=1920, height=1080)]
        slot = sel.Slot("x", ["broll"], 1000, 2000, 3000)
        assert sel.eligible_candidates(FakeDb(rows), sel.VideoBrief(), slot) == []

    def test_landscape_can_be_admitted_explicitly(self):
        rows = [asset(1, "broll", orientation="landscape")]
        slot = sel.Slot("x", ["broll"], 1000, 2000, 3000)
        brief = sel.VideoBrief(allow_landscape=True)
        assert len(sel.eligible_candidates(FakeDb(rows), brief, slot)) == 1

    def test_clip_shorter_than_the_slot_minimum_is_excluded(self):
        rows = [asset(1, "app", duration_ms=2000)]
        slot = sel.Slot("app_demonstration", ["app"], 5000, 6500, 8000)
        assert sel.eligible_candidates(FakeDb(rows), sel.VideoBrief(), slot) == []

    def test_missing_one_slot_fails_the_whole_brief(self):
        # No app recordings: a four-clip video is not a silently acceptable
        # substitute for the five the template specifies.
        rows = [r for r in full_library() if r["asset_type"] != "app"]
        with pytest.raises(sel.SelectionError) as excinfo:
            sel.select(FakeDb(rows), NY)
        assert excinfo.value.code == "insufficient_assets"
        assert "app_demonstration" in excinfo.value.diagnostics["unfilled_slots"]


class TestNoDuplicatesWithinARender:
    def test_the_same_asset_is_not_used_twice(self):
        recipe = sel.select(FakeDb(full_library()), NY)
        pks = [c["asset_pk"] for c in recipe["timeline"]]
        assert len(pks) == len(set(pks))

    def test_identical_payloads_under_different_keys_are_not_both_used(self):
        # The reaction library has 14 such pairs. Sharing a checksum means
        # sharing footage, whatever the key says.
        rows = [asset(1, "app"), asset(2, "app")]
        for i in range(10, 16):
            rows.append(asset(i, "broll", category="food", subcategory="pizza",
                              checksum_sha256="IDENTICAL"))
        with pytest.raises(sel.SelectionError):
            sel.select(FakeDb(rows), NY)

    def test_variety_is_preferred_across_places(self):
        recipe = sel.select(FakeDb(full_library()), NY)
        places = [c["asset_pk"] for c in recipe["timeline"] if c["role"].endswith("visual")]
        assert len(set(places)) == len(places)


class TestDeterminism:
    def test_same_seed_gives_the_same_recipe(self):
        a = sel.select(FakeDb(full_library()), NY)
        b = sel.select(FakeDb(full_library()), NY)
        assert [c["asset_pk"] for c in a["timeline"]] == \
               [c["asset_pk"] for c in b["timeline"]]

    def test_absent_seed_is_still_stable_for_one_brief(self):
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza")
        a = sel.select(FakeDb(full_library()), brief)
        b = sel.select(FakeDb(full_library()), brief)
        assert [c["asset_pk"] for c in a["timeline"]] == \
               [c["asset_pk"] for c in b["timeline"]]


class TestRanking:
    def test_topic_match_outranks_a_generic_clip(self):
        slot = sel.Slot("hook_visual", ["broll"], 2000, 2500, 3000,
                        prefer_topic_match=True)
        pizza = asset(1, "broll", category="food", subcategory="pizza")
        hotel = asset(2, "broll", category="hotel")
        brief = sel.VideoBrief(topic="pizza")
        assert sel.score_candidate(pizza, brief, slot, set(), set()) > \
               sel.score_candidate(hotel, brief, slot, set(), set())

    def test_duration_near_the_slot_preference_scores_higher(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        close = asset(1, "broll", duration_ms=2600)
        far = asset(2, "broll", duration_ms=30000)
        brief = sel.VideoBrief()
        assert sel.score_candidate(close, brief, slot, set(), set()) > \
               sel.score_candidate(far, brief, slot, set(), set())

    def test_repeating_a_place_is_penalised(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        clip = asset(1, "broll", place_name="Roberta's")
        brief = sel.VideoBrief()
        fresh = sel.score_candidate(clip, brief, slot, set(), set())
        repeat = sel.score_candidate(clip, brief, slot, {"roberta's"}, set())
        assert repeat < fresh


class TestDurationFitting:
    def test_preferences_are_used_when_they_already_hit_the_target(self):
        slots = [sel.Slot("a", ["broll"], 1000, 2000, 3000),
                 sel.Slot("b", ["broll"], 1000, 3000, 5000)]
        assert sel.fit_durations(slots, 5000) == [2000, 3000]

    def test_grows_within_bounds(self):
        slots = [sel.Slot("a", ["broll"], 1000, 2000, 3000),
                 sel.Slot("b", ["broll"], 1000, 3000, 5000)]
        assert sum(sel.fit_durations(slots, 7000)) == 7000

    def test_never_exceeds_a_slot_maximum(self):
        slots = [sel.Slot("a", ["broll"], 1000, 2000, 2500)]
        fitted = sel.fit_durations(slots, 999999)
        assert fitted[0] <= 2500

    def test_never_goes_below_a_slot_minimum(self):
        slots = [sel.Slot("a", ["broll"], 2000, 3000, 4000)]
        assert sel.fit_durations(slots, 100)[0] >= 2000


class TestTemplates:
    def test_both_templates_load(self):
        for name in ("city-discovery-v1", "reaction-hook-v1"):
            template = sel.load_template(name)
            assert template.slots and all(s.min_ms <= s.preferred_ms <= s.max_ms
                                          for s in template.slots)

    def test_unknown_template_is_reported_not_guessed(self):
        with pytest.raises(sel.SelectionError) as excinfo:
            sel.load_template("no-such-template")
        assert excinfo.value.code == "recipe_invalid"

    def test_reaction_slot_ignores_the_brief_city(self):
        # Reactions carry no city, so a city filter would exclude all of
        # them and make the template unusable.
        template = sel.load_template("reaction-hook-v1")
        assert template.slots[0].city_agnostic_ok is True


class TestShotTypeVariety:
    """
    Depth on one location is only useful if the selector can tell the
    angles apart. Two exteriors of the same restaurant reads as an editing
    error; an exterior then a dish close-up of that restaurant reads as a
    sequence.
    """

    def test_repeating_a_shot_type_is_penalised(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        clip = asset(1, "broll", shot_type="exterior")
        brief = sel.VideoBrief()
        fresh = sel.score_candidate(clip, brief, slot, set(), set(), set())
        repeat = sel.score_candidate(clip, brief, slot, set(), set(), {"exterior"})
        assert repeat < fresh

    def test_same_place_and_same_shot_type_is_the_worst_case(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        clip = asset(1, "broll", place_name="L'Industrie Pizza", shot_type="exterior")
        brief = sel.VideoBrief()
        both = sel.score_candidate(clip, brief, slot, {"l'industrie pizza"},
                                   set(), {"exterior"})
        place_only = sel.score_candidate(clip, brief, slot, {"l'industrie pizza"},
                                         set(), set())
        assert both < place_only

    def test_a_different_angle_at_a_used_place_still_beats_an_off_topic_clip(self):
        # The point of the two penalties being separate: eight clips of two
        # restaurants stay usable, provided the angles differ.
        slot = sel.Slot("supporting_visual", ["broll"], 3000, 4000, 5000,
                        prefer_topic_match=True)
        brief = sel.VideoBrief(topic="pizza")
        same_place_new_angle = asset(1, "broll", category="food", subcategory="pizza",
                                     place_name="L'Industrie Pizza", shot_type="close-up")
        off_topic = asset(2, "broll", category="hotel", place_name="Some Hotel",
                          shot_type="wide")
        assert sel.score_candidate(same_place_new_angle, brief, slot,
                                   {"l'industrie pizza"}, set(), {"exterior"}) > \
               sel.score_candidate(off_topic, brief, slot, set(), set(), set())

    def test_untagged_shot_type_does_not_penalise(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        clip = asset(1, "broll", shot_type=None)
        brief = sel.VideoBrief()
        assert sel.score_candidate(clip, brief, slot, set(), set(), {"exterior"}) == \
               sel.score_candidate(clip, brief, slot, set(), set(), set())


class TestShotSignalReadsTheTaggedField:
    """
    Shot type was entered into `subtype` during review, not into the
    `shot_type` column the schema provides. The selector reads where the
    data is.
    """

    def test_subtype_is_used_when_shot_type_is_empty(self):
        assert sel.shot_signal({"subtype": "Exterior", "shot_type": None}) == "exterior"

    def test_shot_type_wins_when_both_are_present(self):
        assert sel.shot_signal({"subtype": "Exterior", "shot_type": "close-up"}) == "close-up"

    def test_absent_on_both_is_none(self):
        assert sel.shot_signal({"subtype": None, "shot_type": None}) is None
        assert sel.shot_signal({}) is None

    def test_two_exteriors_of_one_place_are_penalised_via_subtype(self):
        slot = sel.Slot("supporting_visual", ["broll"], 3000, 4000, 5000,
                        prefer_topic_match=True)
        brief = sel.VideoBrief(topic="pizza")
        second_exterior = asset(1, "broll", category="food", subcategory="pizza",
                                place_name="L'Industrie Pizza", subtype="Exterior")
        different_angle = asset(2, "broll", category="food", subcategory="pizza",
                                place_name="L'Industrie Pizza", subtype="Interior")
        used_places, used_shots = {"l'industrie pizza"}, {"exterior"}
        assert sel.score_candidate(different_angle, brief, slot, used_places,
                                   set(), used_shots) > \
               sel.score_candidate(second_exterior, brief, slot, used_places,
                                   set(), used_shots)
