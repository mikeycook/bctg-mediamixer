"""
Editorial text over video.

Three of these guard failures that are invisible until you watch the
finished file: an apostrophe breaking the ffmpeg command, a long line
running off the frame, and text sitting under the platform's own UI.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import CaptionBuilder as cb  # noqa: E402
import VideoRenderer as vr  # noqa: E402

CANVAS = {"width": 1080, "height": 1920, "fps": 30}


def clip(pk, role, start_at, length, **kw):
    return {"asset_id": f"UGC-{pk:05d}", "asset_pk": pk, "role": role,
            "s3_key": f"ugc-assets/x/{pk}.mov", "checksum_sha256": f"{pk:064x}",
            "source_in_ms": 0, "source_out_ms": length,
            "timeline_in_ms": start_at, **kw}


def recipe():
    return {"canvas": CANVAS, "timeline": [
        clip(1, "hook_visual", 0, 2500),
        clip(2, "destination_proof", 2500, 5000),
        clip(3, "cta", 7500, 2500),
    ]}


ASSETS = {
    1: {"place_name": "L'Industrie Pizza", "subcategory": "Pizza", "subtype": "Exterior"},
    2: {"place_name": "Roberta's", "subcategory": "Pizza", "subtype": "Interior"},
    3: {"place_name": None, "subcategory": "Tacos", "subtype": "Interior"},
}


class TestPatternFilling:
    def test_fields_are_interpolated(self):
        assert cb.fill_pattern("{subcategory} in {city}",
                               {"subcategory": "Pizza", "city": "New York"}) == \
            "Pizza in New York"

    def test_a_missing_field_drops_the_caption_entirely(self):
        # "Pizza in " or "{city}" on screen is worse than no caption.
        assert cb.fill_pattern("{subcategory} in {city}",
                               {"subcategory": "Pizza", "city": None}) is None

    def test_a_blank_field_counts_as_missing(self):
        assert cb.fill_pattern("{place_name}", {"place_name": "   "}) is None

    def test_a_pattern_with_no_placeholders_always_resolves(self):
        assert cb.fill_pattern("Find it in Big City Travel Guide", {}) == \
            "Find it in Big City Travel Guide"


class TestPlanning:
    def specs(self):
        return [
            {"role": "hook_visual", "pattern": "{place_name}", "style": "hook"},
            {"role": "destination_proof", "pattern": "{subcategory} in {city}",
             "style": "label"},
            {"role": "cta", "pattern": "Find it in Big City Travel Guide",
             "style": "cta"},
        ]

    def test_captions_land_inside_their_clip(self):
        plan = cb.plan_captions(recipe(), ASSETS, self.specs(), city_name="New York")
        spans = {c.style: (c.start_ms, c.end_ms) for c in plan.captions}
        assert spans["hook"][0] >= 0 and spans["hook"][1] <= 2500
        assert spans["label"][0] >= 2500 and spans["label"][1] <= 7500

    def test_captions_are_ordered_by_time(self):
        plan = cb.plan_captions(recipe(), ASSETS, self.specs(), city_name="New York")
        starts = [c.start_ms for c in plan.captions]
        assert starts == sorted(starts)

    def test_an_unresolvable_pattern_is_reported_not_guessed(self):
        specs = [{"role": "cta", "pattern": "{place_name}", "style": "cta"}]
        plan = cb.plan_captions(recipe(), ASSETS, specs, city_name="New York")
        assert plan.captions == []
        assert any("cta" in u for u in plan.unresolved)

    def test_a_spec_for_a_skipped_slot_is_reported(self):
        # Optional slots can be absent; a caption for one must not crash.
        specs = [{"role": "reaction_beat", "pattern": "Wow", "style": "hook"}]
        plan = cb.plan_captions(recipe(), ASSETS, specs)
        assert plan.captions == []
        assert any("not present" in u for u in plan.unresolved)

    def test_no_specs_means_no_captions(self):
        assert cb.plan_captions(recipe(), ASSETS, []).captions == []


class TestWrapping:
    def test_long_text_is_broken_into_lines(self):
        # drawtext does not wrap; an unbroken line runs off the frame.
        lines = cb.wrap_text("The best hidden gem pizza place in all of Brooklyn", 20)
        assert len(lines) > 1
        assert all(len(line) <= 20 for line in lines)

    def test_short_text_stays_on_one_line(self):
        assert cb.wrap_text("Roberta's", 20) == ["Roberta's"]

    def test_an_overlong_word_is_not_chopped(self):
        # A place name slightly oversized beats one cut in half.
        assert cb.wrap_text("Supercalifragilistic", 8) == ["Supercalifragilistic"]


class TestEscapingAvoidance:
    def test_apostrophes_never_reach_the_command_line(self, tmp_path):
        # "L'Industrie" and "Roberta's" are the two most-used places here,
        # and drawtext's escaping rules would break on both.
        caps = [cb.Caption("L'Industrie Pizza: the best", 0, 2000, "hook")]
        prepared = cb.write_caption_files(caps, str(tmp_path), CANVAS)
        clauses = cb.drawtext_filters(prepared, CANVAS, fontfile="/f.ttf")
        assert "textfile=" in clauses[0]
        assert "L'Industrie" not in clauses[0]
        assert open(prepared[0]["path"], encoding="utf-8").read().startswith("L'Industrie")

    def test_the_text_file_holds_the_wrapped_lines(self, tmp_path):
        caps = [cb.Caption("A rather long hook line that will need wrapping here",
                           0, 3000, "hook")]
        prepared = cb.write_caption_files(caps, str(tmp_path), CANVAS)
        assert "\n" in open(prepared[0]["path"], encoding="utf-8").read()


class TestSafeAreas:
    def test_lower_text_clears_the_platform_ui(self, tmp_path):
        # Instagram and TikTok draw controls over the bottom fifth.
        caps = [cb.Caption("Pizza in New York", 0, 2000, "label")]
        prepared = cb.write_caption_files(caps, str(tmp_path), CANVAS)
        clause = cb.drawtext_filters(prepared, CANVAS)[0]
        y = int(clause.split(":y=")[1].split(":")[0])
        assert y < CANVAS["height"] * cb.SAFE_BOTTOM

    def test_upper_text_clears_the_status_bar(self, tmp_path):
        caps = [cb.Caption("L'Industrie", 0, 2000, "hook")]
        prepared = cb.write_caption_files(caps, str(tmp_path), CANVAS)
        clause = cb.drawtext_filters(prepared, CANVAS)[0]
        y = int(clause.split(":y=")[1].split(":")[0])
        assert y >= CANVAS["height"] * cb.SAFE_TOP

    def test_each_clause_is_gated_to_its_own_window(self, tmp_path):
        caps = [cb.Caption("First", 0, 2000, "hook"),
                cb.Caption("Second", 3000, 5000, "label")]
        prepared = cb.write_caption_files(caps, str(tmp_path), CANVAS)
        clauses = cb.drawtext_filters(prepared, CANVAS)
        assert "between(t,0.000,2.000)" in clauses[0]
        assert "between(t,3.000,5.000)" in clauses[1]


class TestFilterGraphIntegration:
    def render_recipe(self):
        r = recipe()
        r.update({"recipe_version": 1, "total_duration_ms": 10000,
                  "template": {"id": "t", "version": 1}, "brief": {},
                  "renderer": {"name": "ffmpeg"}})
        return r

    def test_text_is_drawn_after_the_concat(self):
        # Timing is measured against the finished timeline, which is what
        # lets a caption span a cut.
        graph = vr.build_filter_graph(self.render_recipe(), [True] * 3,
                                      drawtext_clauses=["drawtext=x"])
        assert "[cutv]drawtext=x[outv]" in graph
        assert "concat=n=3:v=1:a=1[cutv][cata]" in graph

    def test_without_captions_the_graph_is_unchanged(self):
        graph = vr.build_filter_graph(self.render_recipe(), [True] * 3)
        assert "concat=n=3:v=1:a=1[outv][cata]" in graph
        assert "drawtext" not in graph

    def test_captions_do_not_reintroduce_simple_filtering(self):
        args = vr.build_ffmpeg_command(
            self.render_recipe(), ["/a.mov", "/b.mov", "/c.mov"], "/out.mp4",
            drawtext_clauses=["drawtext=x"])
        assert "-vf" not in args and "-af" not in args


class TestSrt:
    def test_timestamps_are_srt_formatted(self):
        srt = cb.to_srt([cb.Caption("Hello", 1500, 4250, "hook")])
        assert "00:00:01,500 --> 00:00:04,250" in srt
        assert "Hello" in srt

    def test_cues_are_numbered_from_one(self):
        srt = cb.to_srt([cb.Caption("A", 0, 1000, "hook"),
                         cb.Caption("B", 1000, 2000, "label")])
        assert srt.startswith("1\n")
        assert "\n2\n" in srt

    def test_hours_roll_over_correctly(self):
        srt = cb.to_srt([cb.Caption("Late", 3_723_456, 3_724_000, "label")])
        assert "01:02:03,456" in srt
