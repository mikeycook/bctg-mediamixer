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
            if "neighborhoods" in params and (
                    (row.get("neighborhood") or "").lower() not in params["neighborhoods"]):
                continue
            if "feature" in params and (
                    (row.get("subtype") or "").lower() != params["feature"].lower()):
                continue
            out.append(dict(row))
        return out


def asset(pk, asset_type, **kw):
    base = {
        "id": pk, "asset_id": f"UGC-{pk:05d}", "s3_key": f"ugc-assets/x/{pk}.mov",
        "s3_version_id": None, "checksum_sha256": f"sha{pk}", "asset_type": asset_type,
        "category": None, "subcategory": None, "place_name": None,
        "cityid": "CIT-00000000002", "city_slug": "new-york", "city_agnostic": False,
        "neighborhood": None,
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


class TestShotProgression:
    """
    Templates describe a sequence, not just a set of durations: establish
    outside, go inside, then the payoff. Expressed as preferences so a thin
    library still produces a video.
    """

    def test_preferred_subtype_scores_higher(self):
        slot = sel.Slot("hook_visual", ["broll"], 2000, 2500, 3000,
                        prefer_subtypes=["Exterior"])
        brief = sel.VideoBrief()
        exterior = asset(1, "broll", subtype="Exterior")
        interior = asset(2, "broll", subtype="Interior")
        assert sel.score_candidate(exterior, brief, slot, set(), set(), set()) > \
               sel.score_candidate(interior, brief, slot, set(), set(), set())

    def test_topic_still_outranks_shot_kind(self):
        # An off-topic clip must never win merely for being the right
        # framing — a hotel exterior is not a pizza video's hook.
        slot = sel.Slot("hook_visual", ["broll"], 2000, 2500, 3000,
                        prefer_topic_match=True, prefer_subtypes=["Exterior"])
        brief = sel.VideoBrief(topic="pizza")
        on_topic_wrong_shot = asset(1, "broll", subcategory="pizza", subtype="Interior")
        off_topic_right_shot = asset(2, "broll", subcategory="watches", subtype="Exterior")
        assert sel.score_candidate(on_topic_wrong_shot, brief, slot, set(), set(), set()) > \
               sel.score_candidate(off_topic_right_shot, brief, slot, set(), set(), set())

    def test_a_slot_with_no_preference_is_unaffected(self):
        slot = sel.Slot("cta", ["broll"], 2000, 2500, 3000)
        brief = sel.VideoBrief()
        assert sel.score_candidate(asset(1, "broll", subtype="Exterior"),
                                   brief, slot, set(), set(), set()) == \
               sel.score_candidate(asset(2, "broll", subtype="Menu"),
                                   brief, slot, set(), set(), set())

    def test_template_defines_the_expected_progression(self):
        template = sel.load_template("city-discovery-v1")
        by_role = {s.role: s.prefer_subtypes for s in template.slots}
        assert by_role["hook_visual"] == ["Exterior"]
        assert by_role["destination_proof"] == ["Interior"]
        assert "Food" in by_role["supporting_visual"]
        # Listed under both names so the preference survives renaming
        # subtype Food to Dish.
        assert "Dish" in by_role["supporting_visual"]

    def test_app_slots_carry_no_shot_preference(self):
        # App subtypes are features (Guide, Map), not framings.
        template = sel.load_template("city-discovery-v1")
        app_slot = next(s for s in template.slots if s.role == "app_demonstration")
        assert app_slot.prefer_subtypes == []


def reaction(pk, emotions, **kw):
    kw.setdefault("duration_ms", 4000)
    return asset(pk, "reaction", city_agnostic=True, cityid=None, city_slug=None,
                 emotions=emotions, **kw)


class MoodAwareDb(FakeDb):
    """
    Adds the emotion-tag filters the real SQL applies via a join: a single
    mood from the brief, or a set of emotions named on the slot.
    """

    def execute_query_as_dict(self, sql, params=None):
        rows = super().execute_query_as_dict(sql, params)
        if params and "mood" in params:
            rows = [r for r in rows if params["mood"] in (r.get("emotions") or [])]
        if params and "emotions" in params:
            wanted = set(params["emotions"])
            rows = [r for r in rows if wanted & set(r.get("emotions") or [])]
        return rows


class TestOptionalSlots:
    def test_an_unfilled_optional_slot_is_skipped_not_fatal(self):
        # The whole point of an optional reaction beat: no reaction footage
        # for this mood must not stop the video being made.
        rows = full_library()          # contains no reactions at all
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1",
                               mood="surprised", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        assert [c["role"] for c in recipe["timeline"]] == [
            "hook_visual", "app_demonstration", "destination_proof",
            "supporting_visual"]
        assert recipe["skipped_optional_slots"] == ["reaction_beat"]

    def test_the_timeline_closes_up_behind_a_skipped_slot(self):
        rows = full_library()
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        at = 0
        for clip in recipe["timeline"]:
            assert clip["timeline_in_ms"] == at
            at += clip["source_out_ms"] - clip["source_in_ms"]
        assert recipe["total_duration_ms"] == at

    def test_a_required_slot_still_fails_the_brief(self):
        rows = [r for r in full_library() if r["asset_type"] != "app"]
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1", seed="t")
        with pytest.raises(sel.SelectionError):
            sel.select(MoodAwareDb(rows), brief)


class TestMood:
    def test_the_mood_chooses_the_reaction(self):
        rows = full_library() + [
            reaction(80, ["surprised"]), reaction(81, ["happy"])]
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1",
                               mood="happy", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        beat = next(c for c in recipe["timeline"] if c["role"] == "reaction_beat")
        assert beat["asset_pk"] == 81

    def test_a_performance_filed_under_two_emotions_answers_to_both(self):
        # This is what the alias merge buys: one clip, both moods.
        rows = full_library() + [reaction(82, ["surprised", "excited"])]
        for mood in ("surprised", "excited"):
            brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                                   template_id="city-discovery-reaction-v1",
                                   mood=mood, seed="t")
            recipe = sel.select(MoodAwareDb(rows), brief)
            assert any(c["role"] == "reaction_beat" for c in recipe["timeline"])

    def test_reactions_are_not_excluded_by_the_brief_city(self):
        # Reactions carry no city; without city_agnostic_ok every one would
        # be filtered out and the slot would never fill.
        template = sel.load_template("city-discovery-reaction-v1")
        beat = next(s for s in template.slots if s.role == "reaction_beat")
        assert beat.city_agnostic_ok is True
        assert beat.match_mood is True
        assert beat.required is False

    def test_mood_is_recorded_on_the_recipe(self):
        rows = full_library() + [reaction(83, ["shocked"])]
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1",
                               mood="shocked", seed="t")
        assert sel.select(MoodAwareDb(rows), brief)["brief"]["mood"] == "shocked"


class TestTemplateOrdering:
    def test_the_app_moves_earlier_in_the_reaction_template(self):
        # Slot order in the JSON is the order in the video; that is the
        # whole mechanism for rearranging one.
        roles = [s.role for s in sel.load_template("city-discovery-reaction-v1").slots]
        assert roles.index("app_demonstration") < roles.index("destination_proof")
        base = [s.role for s in sel.load_template("city-discovery-v1").slots]
        assert base.index("app_demonstration") > base.index("destination_proof")


class TestRotationAcrossRenders:
    """
    An alternate shot of the same place exists so a run of variations can
    differ. Within one video the pair is a mistake; across a series it is
    the point.
    """

    def test_an_unused_clip_outranks_one_already_in_a_video(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        brief = sel.VideoBrief()
        fresh = asset(1, "broll", use_count=0)
        used = asset(2, "broll", use_count=2)
        assert sel.score_candidate(fresh, brief, slot, set(), set(), set()) > \
               sel.score_candidate(used, brief, slot, set(), set(), set())

    def test_heavier_use_ranks_lower_still(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        brief = sel.VideoBrief()
        once = sel.score_candidate(asset(1, "broll", use_count=1), brief, slot,
                                   set(), set(), set())
        often = sel.score_candidate(asset(2, "broll", use_count=5), brief, slot,
                                    set(), set(), set())
        assert often < once

    def test_rotation_can_be_turned_off_for_a_repeatable_edit(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000)
        brief = sel.VideoBrief(prefer_unused=False)
        fresh = asset(1, "broll", use_count=0)
        used = asset(2, "broll", use_count=9)
        assert sel.score_candidate(fresh, brief, slot, set(), set(), set()) == \
               sel.score_candidate(used, brief, slot, set(), set(), set())

    def test_topic_still_outranks_novelty(self):
        # A fresh clip of the wrong subject must not beat a used clip of the
        # right one — rotation varies the edit, it does not redefine it.
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000, prefer_topic_match=True)
        brief = sel.VideoBrief(topic="pizza")
        on_topic_used = asset(1, "broll", subcategory="pizza", use_count=2)
        off_topic_fresh = asset(2, "broll", subcategory="watches", use_count=0)
        assert sel.score_candidate(on_topic_used, brief, slot, set(), set(), set()) > \
               sel.score_candidate(off_topic_fresh, brief, slot, set(), set(), set())

    def test_the_default_is_on(self):
        assert sel.VideoBrief().prefer_unused is True
        assert sel.VideoBrief.from_dict({}).prefer_unused is True


class TestTopicMatchesTags:
    """
    Venue style does not belong in the cuisine field, but it is still a
    thing to build a video around. Tags are where it lives, so a topic has
    to be able to reach them.
    """

    def test_a_tag_satisfies_a_topic(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000, prefer_topic_match=True)
        brief = sel.VideoBrief(topic="upscale")
        tagged = asset(1, "broll", subcategory="french", tag_slugs=["upscale"])
        plain = asset(2, "broll", subcategory="french", tag_slugs=[])
        assert sel.score_candidate(tagged, brief, slot, set(), set(), set()) > \
               sel.score_candidate(plain, brief, slot, set(), set(), set())

    def test_subcategory_still_outranks_a_tag(self):
        # The typed field is the stronger signal; tags supplement it.
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000, prefer_topic_match=True)
        brief = sel.VideoBrief(topic="pizza")
        by_field = asset(1, "broll", subcategory="pizza")
        by_tag = asset(2, "broll", subcategory="french", tag_slugs=["pizza"])
        assert sel.score_candidate(by_field, brief, slot, set(), set(), set()) > \
               sel.score_candidate(by_tag, brief, slot, set(), set(), set())

    def test_untagged_clips_are_unaffected(self):
        slot = sel.Slot("x", ["broll"], 2000, 2500, 3000, prefer_topic_match=True)
        brief = sel.VideoBrief(topic="upscale")
        assert sel.score_candidate(asset(1, "broll", tag_slugs=None),
                                   brief, slot, set(), set(), set()) == \
               sel.score_candidate(asset(2, "broll", tag_slugs=[]),
                                   brief, slot, set(), set(), set())


class TestReactionArc:
    """
    Two reactions with different emotions in one video — intrigue at the
    top, delight after the payoff. A single brief-level mood cannot express
    that, so the emotions live on the slots.
    """

    def library_with_reactions(self):
        rows = full_library()
        rows.append(reaction(70, ["surprised"], duration_ms=5000))
        rows.append(reaction(71, ["excited"], duration_ms=5000))
        return rows

    def test_each_slot_gets_its_own_emotion(self):
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="reaction-arc-v1", seed="t")
        recipe = sel.select(MoodAwareDb(self.library_with_reactions()), brief)
        by_role = {c["role"]: c["asset_pk"] for c in recipe["timeline"]}
        assert by_role["reaction_open"] == 70      # surprised
        assert by_role["reaction_payoff"] == 71    # excited

    def test_slot_emotions_ignore_the_brief_mood(self):
        # A blanket mood must not override a slot asking for something
        # specific, or the arc collapses to one emotion.
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="reaction-arc-v1",
                               mood="happy", seed="t")
        recipe = sel.select(MoodAwareDb(self.library_with_reactions()), brief)
        assert any(c["role"] == "reaction_open" for c in recipe["timeline"])

    def test_one_performance_cannot_fill_both_beats(self):
        # A clip filed under both surprised and excited qualifies for each
        # slot. Using it twice would read as a mistake, so the checksum
        # exclusion has to catch it.
        rows = full_library()
        rows.append(reaction(72, ["surprised", "excited"], duration_ms=5000))
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="reaction-arc-v1", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        reactions = [c for c in recipe["timeline"] if c["role"].startswith("reaction")]
        assert len(reactions) == 1
        assert recipe["skipped_optional_slots"] == ["reaction_payoff"]

    def test_a_missing_payoff_costs_the_beat_not_the_video(self):
        rows = full_library() + [reaction(73, ["surprised"], duration_ms=5000)]
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="reaction-arc-v1", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        assert "reaction_payoff" in recipe["skipped_optional_slots"]
        assert len(recipe["timeline"]) == 5

    def test_no_opener_fails_the_brief(self):
        # The opener is what makes this template what it is.
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="reaction-arc-v1", seed="t")
        with pytest.raises(sel.SelectionError) as excinfo:
            sel.select(MoodAwareDb(full_library()), brief)
        assert "reaction_open" in excinfo.value.diagnostics["unfilled_slots"]


# ---------------------------------------------------------------------------
# Music bed
# ---------------------------------------------------------------------------

def music_row(pk, **kw):
    base = {
        "id": pk, "track_id": f"MUS-{pk:05d}",
        "s3_key": f"ugc-assets/music/{pk}.mp3",
        "title": f"Track {pk}", "artist": "Artist", "album": None,
        "genre": None, "mood": None, "source": "Audio Library",
        "source_url": f"https://y/{pk}", "license": "cc_by",
        "license_url": "https://l/by", "attribution_required": True,
        "attribution_text": None, "duration_ms": 60000,
        "status": "active", "commercial_use_allowed": True,
    }
    base.update(kw)
    return base


class MusicDb(MoodAwareDb):
    """Serves the music query from a separate list; asset queries pass through."""

    def __init__(self, rows, tracks=None):
        super().__init__(rows)
        self.tracks = tracks or []

    def execute_query_as_dict(self, sql, params=None):
        if "content_library_music_tracks" in sql:
            self.queries.append((sql, params))
            return [dict(t) for t in self.tracks
                    if t.get("status") == "active" and t.get("commercial_use_allowed")]
        return super().execute_query_as_dict(sql, params)


class TestMusicBed:
    def test_a_cleared_track_becomes_the_bed(self):
        recipe = sel.select(MusicDb(full_library(), [music_row(1)]), NY)
        assert recipe["music"][0]["track_id"] == "MUS-00001"
        assert recipe["music"][0]["attribution_required"] is True
        mix = recipe["audio_mix"]["music"]
        assert mix["s3_key"] == "ugc-assets/music/1.mp3"
        assert mix["gain"] == sel.DEFAULT_MUSIC_GAIN
        assert mix["source_gain"] == sel.DEFAULT_AMBIENT_GAIN

    def test_no_eligible_track_leaves_no_bed(self):
        recipe = sel.select(MusicDb(full_library(), []), NY)
        assert recipe.get("music") is None
        assert recipe["audio_mix"]["music"] is None

    def test_unconfirmed_commercial_use_is_not_selected(self):
        # commercial_use_allowed NULL means "not confirmed", not a licence.
        recipe = sel.select(
            MusicDb(full_library(), [music_row(1, commercial_use_allowed=None)]), NY)
        assert recipe.get("music") is None

    def test_with_music_false_skips_the_bed(self):
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               seed="test", with_music=False)
        recipe = sel.select(MusicDb(full_library(), [music_row(1)]), brief)
        assert recipe.get("music") is None

    def test_a_pinned_track_is_used(self):
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               seed="test", music_track_id="MUS-00002")
        recipe = sel.select(MusicDb(full_library(), [music_row(1), music_row(2)]), brief)
        assert recipe["music"][0]["track_id"] == "MUS-00002"

    def test_the_choice_is_deterministic_for_a_seed(self):
        tracks = [music_row(i) for i in range(1, 6)]
        a = sel.select(MusicDb(full_library(), tracks), NY)
        b = sel.select(MusicDb(full_library(), tracks), NY)
        assert a["music"][0]["track_id"] == b["music"][0]["track_id"]


# ---------------------------------------------------------------------------
# Branded end-card
# ---------------------------------------------------------------------------

def endcard(pk=500):
    # A city-agnostic cta asset long enough for the cta slot (>= 2s).
    return asset(pk, "cta", city_agnostic=True, cityid=None, city_slug=None,
                 duration_ms=3000, place_name=None, subcategory=None)


class TestEndCard:
    def test_the_card_fills_the_cta_slot_and_drops_its_caption(self):
        rows = full_library() + [endcard()]
        recipe = sel.select(MoodAwareDb(rows), NY)          # with_endcard defaults True
        cta = [c for c in recipe["timeline"] if c["role"] == "cta"][0]
        assert cta["asset_id"] == "UGC-00500"
        assert all(spec["role"] != "cta" for spec in recipe["caption_specs"])

    def test_falls_back_when_no_card_is_available(self):
        # No cta asset in the library: the cta slot uses a generic clip and
        # the generated line is kept, exactly as before the feature.
        recipe = sel.select(MoodAwareDb(full_library()), NY)
        cta = [c for c in recipe["timeline"] if c["role"] == "cta"][0]
        assert cta["asset_id"] != "UGC-00500"
        assert any(spec["role"] == "cta" for spec in recipe["caption_specs"])

    def test_toggling_it_off_ignores_the_card(self):
        rows = full_library() + [endcard()]
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               seed="test", with_endcard=False)
        recipe = sel.select(MoodAwareDb(rows), brief)
        cta = [c for c in recipe["timeline"] if c["role"] == "cta"][0]
        assert cta["asset_id"] != "UGC-00500"
        assert any(spec["role"] == "cta" for spec in recipe["caption_specs"])


# ---------------------------------------------------------------------------
# Neighborhood scoping
# ---------------------------------------------------------------------------

class TestNeighborhoodEligibility:
    def test_match_neighborhood_slot_filters_to_the_brief_area(self):
        rows = [asset(1, "broll", neighborhood="Chelsea"),
                asset(2, "broll", neighborhood="SoHo"),
                asset(3, "broll", neighborhood=None)]
        slot = sel.Slot(role="hook_visual", asset_types=["broll"], min_ms=0,
                        preferred_ms=2500, max_ms=3000, match_neighborhood=True)
        brief = sel.VideoBrief(cityid="CIT-00000000002", neighborhoods=("chelsea",))
        got = {c["id"] for c in sel.eligible_candidates(FakeDb(rows), brief, slot)}
        assert got == {1}                     # case-insensitive; SoHo and NULL excluded

    def test_a_slot_that_does_not_opt_in_ignores_the_neighborhood(self):
        rows = [asset(1, "broll", neighborhood="Chelsea"),
                asset(2, "broll", neighborhood="SoHo")]
        slot = sel.Slot(role="supporting_visual", asset_types=["broll"], min_ms=0,
                        preferred_ms=4000, max_ms=5000, match_neighborhood=False)
        brief = sel.VideoBrief(cityid="CIT-00000000002", neighborhoods=("Chelsea",))
        got = {c["id"] for c in sel.eligible_candidates(FakeDb(rows), brief, slot)}
        assert got == {1, 2}

    def test_no_brief_neighborhood_means_no_filter(self):
        rows = [asset(1, "broll", neighborhood="Chelsea"),
                asset(2, "broll", neighborhood="SoHo")]
        slot = sel.Slot(role="hook_visual", asset_types=["broll"], min_ms=0,
                        preferred_ms=2500, max_ms=3000, match_neighborhood=True)
        brief = sel.VideoBrief(cityid="CIT-00000000002")   # no neighborhood
        got = {c["id"] for c in sel.eligible_candidates(FakeDb(rows), brief, slot)}
        assert got == {1, 2}


class TestNeighborhoodTemplate:
    def library(self):
        return [
            asset(1, "app", duration_ms=9000),
            asset(2, "app", duration_ms=9000),
            asset(10, "broll", neighborhood="Chelsea", duration_ms=9000,
                  place_name="Taqueria A", subcategory="tacos"),
            asset(11, "broll", neighborhood="Chelsea", duration_ms=9000,
                  place_name="Taqueria B", subcategory="tacos"),
            asset(12, "broll", neighborhood="SoHo", duration_ms=9000,
                  place_name="Elsewhere", subcategory="tacos"),
        ]

    def test_broll_scopes_to_the_neighborhood_but_app_does_not(self):
        brief = sel.VideoBrief(cityid="CIT-00000000002",
                               template_id="neighborhood-feature-v1",
                               neighborhoods=("Chelsea",), with_endcard=False, seed="t")
        recipe = sel.select(FakeDb(self.library()), brief)
        by_role = {c["role"]: c for c in recipe["timeline"]}
        # Both b-roll slots came from Chelsea; the SoHo clip was never eligible.
        assert by_role["hook_visual"]["asset_id"] in {"UGC-00010", "UGC-00011"}
        assert by_role["supporting_visual"]["asset_id"] in {"UGC-00010", "UGC-00011"}
        # The app moment is unaffected by the neighborhood filter.
        assert by_role["app_demonstration"]["asset_id"] in {"UGC-00001", "UGC-00002"}

    def test_no_local_footage_fails_the_neighborhood_slots(self):
        # Only SoHo b-roll available for a Chelsea brief: the required
        # neighborhood slots cannot fill, and the brief fails rather than
        # borrowing another neighborhood's footage.
        rows = [asset(1, "app"), asset(2, "app"),
                asset(12, "broll", neighborhood="SoHo")]
        brief = sel.VideoBrief(cityid="CIT-00000000002",
                               template_id="neighborhood-feature-v1",
                               neighborhoods=("Chelsea",), seed="t")
        with pytest.raises(sel.SelectionError) as excinfo:
            sel.select(FakeDb(rows), brief)
        assert "hook_visual" in excinfo.value.diagnostics["unfilled_slots"]


class TestMultipleNeighborhoods:
    def test_a_render_can_span_several_neighborhoods(self):
        rows = [asset(1, "broll", neighborhood="Chelsea"),
                asset(2, "broll", neighborhood="SoHo"),
                asset(3, "broll", neighborhood="Harlem")]
        slot = sel.Slot(role="hook_visual", asset_types=["broll"], min_ms=0,
                        preferred_ms=2500, max_ms=3000, match_neighborhood=True)
        brief = sel.VideoBrief(cityid="CIT-00000000002",
                               neighborhoods=("chelsea", "soho"))
        got = {c["id"] for c in sel.eligible_candidates(FakeDb(rows), brief, slot)}
        assert got == {1, 2}                       # Harlem excluded

    def test_from_dict_parses_a_comma_separated_string(self):
        brief = sel.VideoBrief.from_dict({"neighborhood": "Chelsea, SoHo , chelsea"})
        assert brief.neighborhoods == ("Chelsea", "SoHo")   # trimmed, deduped

    def test_from_dict_accepts_a_list(self):
        brief = sel.VideoBrief.from_dict({"neighborhoods": ["Chelsea", "SoHo"]})
        assert brief.neighborhoods == ("Chelsea", "SoHo")


class TestFeatureTargeting:
    def test_match_feature_slot_scopes_to_the_brief_feature(self):
        rows = [asset(1, "app", subtype="live-tracking"),
                asset(2, "app", subtype="pizza-map")]
        slot = sel.Slot(role="app_demonstration", asset_types=["app"], min_ms=0,
                        preferred_ms=6500, max_ms=8000, match_feature=True)
        brief = sel.VideoBrief(cityid="CIT-00000000002", feature="Live-Tracking")
        got = {c["id"] for c in sel.eligible_candidates(FakeDb(rows), brief, slot)}
        assert got == {1}                          # case-insensitive; pizza-map excluded

    def test_a_slot_that_does_not_opt_in_ignores_the_feature(self):
        rows = [asset(1, "app", subtype="live-tracking"),
                asset(2, "app", subtype="pizza-map")]
        slot = sel.Slot(role="app_demonstration", asset_types=["app"], min_ms=0,
                        preferred_ms=6500, max_ms=8000, match_feature=False)
        brief = sel.VideoBrief(cityid="CIT-00000000002", feature="live-tracking")
        got = {c["id"] for c in sel.eligible_candidates(FakeDb(rows), brief, slot)}
        assert got == {1, 2}

    def test_feature_and_neighborhood_scope_the_right_slots(self):
        # The app slot scopes to the feature; the b-roll slots to the
        # neighborhood; neither constrains the other.
        rows = [asset(1, "app", subtype="live-tracking"),
                asset(2, "app", subtype="pizza-map"),
                asset(10, "broll", neighborhood="Chelsea", place_name="A", subcategory="tacos"),
                asset(11, "broll", neighborhood="Chelsea", place_name="B", subcategory="tacos"),
                asset(12, "broll", neighborhood="SoHo", place_name="C", subcategory="tacos")]
        brief = sel.VideoBrief(cityid="CIT-00000000002",
                               template_id="neighborhood-feature-v1",
                               neighborhoods=("Chelsea",), feature="live-tracking",
                               with_endcard=False, seed="t")
        recipe = sel.select(FakeDb(rows), brief)
        by_role = {c["role"]: c for c in recipe["timeline"]}
        assert by_role["app_demonstration"]["asset_id"] == "UGC-00001"   # live-tracking
        assert by_role["hook_visual"]["asset_id"] in {"UGC-00010", "UGC-00011"}
        assert by_role["supporting_visual"]["asset_id"] in {"UGC-00010", "UGC-00011"}


# ---------------------------------------------------------------------------
# Cohesion — the interior and the dish should be one venue
# ---------------------------------------------------------------------------

def coherent_library():
    rows = [asset(1, "app", duration_ms=9000), asset(2, "app", duration_ms=9000)]
    pk = 10
    for place in ["Joe", "Sal"]:
        for sub in ["Exterior", "Interior", "Food"]:
            rows.append(asset(pk, "broll", place_name=place, subcategory="pizza",
                              subtype=sub, duration_ms=8000))
            pk += 1
    return rows


class TestCohesion:
    def test_the_dish_stays_with_the_venue_we_went_inside(self):
        rows = coherent_library()
        recipe = sel.select(FakeDb(rows), sel.VideoBrief(
            cityid="CIT-00000000002", topic="pizza", seed="t", with_endcard=False))
        lib = {a["id"]: a for a in rows}
        by_role = {c["role"]: lib[c["asset_pk"]] for c in recipe["timeline"]}
        assert by_role["destination_proof"]["place_name"] == \
            by_role["supporting_visual"]["place_name"]

    def test_falls_back_to_the_same_food_category(self):
        # The interior is Joe's; only Sal has a dish. Different place, but the
        # payoff is still pizza rather than an unrelated category.
        rows = [asset(1, "app"), asset(2, "app"),
                asset(10, "broll", place_name="Joe", subcategory="pizza", subtype="Exterior"),
                asset(11, "broll", place_name="Joe", subcategory="pizza", subtype="Interior"),
                asset(12, "broll", place_name="Sal", subcategory="pizza", subtype="Food"),
                asset(13, "broll", place_name="Nao", subcategory="ramen", subtype="Food")]
        recipe = sel.select(FakeDb(rows), sel.VideoBrief(
            cityid="CIT-00000000002", seed="t", with_endcard=False))
        lib = {a["id"]: a for a in rows}
        supp = lib[[c for c in recipe["timeline"] if c["role"] == "supporting_visual"][0]["asset_pk"]]
        assert supp["subcategory"] == "pizza"          # not the ramen dish


class TestEndCardAppend:
    def test_appended_when_the_template_has_no_cta_slot(self):
        # city-discovery-reaction-v1 has no cta slot; the card is appended.
        rows = full_library() + [endcard()]
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        assert recipe["timeline"][-1]["role"] == "cta"
        assert recipe["timeline"][-1]["asset_id"] == "UGC-00500"

    def test_absent_when_no_card_exists(self):
        rows = full_library()
        brief = sel.VideoBrief(cityid="CIT-00000000002", topic="pizza",
                               template_id="city-discovery-reaction-v1", seed="t")
        recipe = sel.select(MoodAwareDb(rows), brief)
        assert all(c["role"] != "cta" for c in recipe["timeline"])
