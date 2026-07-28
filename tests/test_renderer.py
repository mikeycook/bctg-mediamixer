"""
Render command construction, recipe validation, export layout, and the
write guard.

None of this needs ffmpeg or AWS: command building and validation are pure
functions, and the export guard is the one piece of the write path worth
testing hardest, because the failure it prevents — overwriting a source
master — cannot be undone.
"""

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import VideoRenderer as vr  # noqa: E402
from S3Exporter import S3Exporter, ExportPathError  # noqa: E402


def clip(pk, start=0, end=3000, at=0, checksum=None, audio=True):
    return {
        "asset_id": f"UGC-{pk:05d}", "asset_pk": pk,
        "s3_key": f"ugc-assets/b-roll/food/pizza/new-york/{pk}.mov",
        "s3_version_id": None,
        "checksum_sha256": checksum or f"{pk:064x}",
        "role": "hook_visual", "source_in_ms": start, "source_out_ms": end,
        "timeline_in_ms": at, "transform": {},
        "audio_policy": {"mode": "keep" if audio else "silent"},
    }


def recipe(clips=None):
    clips = clips or [clip(1, 0, 3000, 0), clip(2, 250, 5250, 3000)]
    total = sum(c["source_out_ms"] - c["source_in_ms"] for c in clips)
    return {
        "recipe_version": 1,
        "template": {"id": "city-discovery-v1", "version": 1},
        "brief": {}, "canvas": {"width": 1080, "height": 1920, "fps": 30},
        "timeline": clips, "total_duration_ms": total,
        "captions": [], "audio_mix": {},
        "renderer": {"name": "ffmpeg", "version": "record-at-runtime"},
    }


class TestRecipeValidation:
    def test_a_well_formed_recipe_passes(self):
        assert vr.validate_recipe(recipe()) == []

    def test_non_contiguous_timeline_is_rejected(self):
        # A gap here becomes a gap in the video, or a clip playing over
        # another, depending on the renderer's mood.
        bad = recipe([clip(1, 0, 3000, 0), clip(2, 0, 3000, 9999)])
        assert any("contiguous" in e for e in vr.validate_recipe(bad))

    def test_total_duration_must_match_the_timeline(self):
        bad = recipe()
        bad["total_duration_ms"] = 12345
        assert any("total_duration_ms" in e for e in vr.validate_recipe(bad))

    def test_reversed_trim_is_rejected(self):
        bad = recipe([clip(1, 5000, 1000, 0)])
        assert any("source_out_ms" in e for e in vr.validate_recipe(bad))

    def test_same_asset_twice_is_rejected(self):
        bad = recipe([clip(1, 0, 3000, 0), clip(1, 0, 3000, 3000)])
        assert any("more than once" in e for e in vr.validate_recipe(bad))

    def test_same_footage_under_two_keys_is_rejected(self):
        # The reaction library's 14 alias pairs, caught at the last gate
        # even if selection somehow let them through.
        same = f"{7:064x}"
        bad = recipe([clip(1, 0, 3000, 0, checksum=same),
                      clip(2, 0, 3000, 3000, checksum=same)])
        assert any("duplicate footage" in e for e in vr.validate_recipe(bad))

    def test_missing_checksum_is_rejected(self):
        bad = recipe()
        bad["timeline"][0]["checksum_sha256"] = None
        assert any("checksum_sha256" in e for e in vr.validate_recipe(bad))

    def test_empty_timeline_is_rejected(self):
        bad = recipe()
        bad["timeline"] = []
        assert any("empty" in e for e in vr.validate_recipe(bad))


class TestFfmpegCommand:
    def test_one_input_per_clip_and_no_silence_input(self):
        args = vr.build_ffmpeg_command(recipe(), ["/tmp/a.mov", "/tmp/b.mov"], "/tmp/out.mp4")
        assert args.count("-i") == 2          # two clips; silence is in-graph
        assert "-i anullsrc" not in " ".join(args)

    def test_trim_is_applied_as_input_seeking(self):
        args = vr.build_ffmpeg_command(recipe(), ["/tmp/a.mov", "/tmp/b.mov"], "/tmp/out.mp4")
        # Second clip: in at 250ms, 5000ms long.
        assert "0.250" in args and "5.000" in args
        assert args.index("-ss") < args.index("-i")

    def test_no_simple_filtering_alongside_the_complex_graph(self):
        # ffmpeg refuses -af or -vf on a stream fed from -filter_complex,
        # and says so in a message that reads like an audio problem. The
        # first real render failed on exactly this.
        args = vr.build_ffmpeg_command(recipe(), ["/tmp/a.mov", "/tmp/b.mov"], "/tmp/out.mp4")
        assert "-filter_complex" in args
        assert "-af" not in args
        assert "-vf" not in args

    def test_loudness_normalization_happens_inside_the_graph(self):
        graph = vr.build_filter_graph(recipe(), [True, True])
        assert "loudnorm=" in graph
        assert graph.rstrip().endswith("[outa]")

    def test_delivery_codecs_are_pinned(self):
        args = vr.build_ffmpeg_command(recipe(), ["/tmp/a.mov", "/tmp/b.mov"], "/tmp/out.mp4")
        for expected in ("libx264", "yuv420p", "aac", "+faststart"):
            assert expected in args

    def test_input_count_mismatch_raises(self):
        with pytest.raises(ValueError):
            vr.build_ffmpeg_command(recipe(), ["/tmp/only-one.mov"], "/tmp/out.mp4")

    def test_output_path_is_last(self):
        args = vr.build_ffmpeg_command(recipe(), ["/tmp/a.mov", "/tmp/b.mov"], "/tmp/out.mp4")
        assert args[-1] == "/tmp/out.mp4"


class TestFilterGraph:
    def test_every_clip_is_normalized_to_the_canvas(self):
        graph = vr.build_filter_graph(recipe(), [True, True])
        assert graph.count("scale=1080:1920:force_original_aspect_ratio=increase") == 2
        assert graph.count("crop=1080:1920") == 2

    def test_pixel_aspect_is_forced_square(self):
        # A single input with non-square pixels makes concat reject the
        # entire graph, and the error names the filter rather than the file.
        graph = vr.build_filter_graph(recipe(), [True, True])
        assert graph.count("setsar=1") == 2

    def test_silent_clips_get_bounded_generated_silence(self):
        # concat with a=1 needs both streams on every segment; a clip without
        # audio gets silence generated in-graph, bounded to its own length
        # (clip 1 here is 250->5250 = 5.000s) so its concat segment can end.
        graph = vr.build_filter_graph(recipe(), [True, False])
        assert "anullsrc=channel_layout=stereo:sample_rate=48000:d=5.000" in graph
        assert "[a1]" in graph
        assert "concat=n=2:v=1:a=1" in graph
        # No shared silence input is referenced any more.
        assert "[2:a]" not in graph

    def test_concat_covers_every_segment(self):
        three = [clip(1, 0, 1000, 0), clip(2, 0, 1000, 1000), clip(3, 0, 1000, 2000)]
        graph = vr.build_filter_graph(recipe(three), [True, True, True])
        assert "concat=n=3:v=1:a=1[outv][cata]" in graph

    def test_no_music_leaves_the_audio_tail_unchanged(self):
        graph = vr.build_filter_graph(recipe(), [True, True])
        assert "[cata]loudnorm=" in graph
        assert "amix" not in graph

    def test_music_ducks_ambient_and_mixes_the_bed(self):
        # index 2: two clips (0,1), then the bed (2). Silence is in-graph now,
        # so there is no separate silence input to shift the index.
        graph = vr.build_filter_graph(recipe(), [True, True],
                                      music={"index": 2, "gain": 0.85, "source_gain": 0.28})
        assert "[cata]volume=0.28[abed]" in graph            # ambient ducked
        assert "[2:a]aresample=48000" in graph and "volume=0.85[amus]" in graph
        assert "amix=inputs=2:duration=first" in graph
        assert "[amixed]loudnorm=" in graph                  # normalized after the mix
        # amix normalize= is ffmpeg >= 4.4 only and errors the graph on 4.2.
        assert "normalize=" not in graph


class TestMusicCommand:
    def test_bed_is_the_last_input(self):
        args = vr.build_ffmpeg_command(recipe(), ["/a.mov", "/b.mov"], "/out.mp4",
                                       music_path="/bed.mp3")
        assert "-stream_loop" in args
        assert args[args.index("-stream_loop") + 1] == "-1"
        # No shared silence input now, so the bed follows the two clips at
        # index 2 and the graph mixes [2:a].
        assert "-i anullsrc" not in " ".join(args)
        assert "[2:a]aresample=48000" in " ".join(args)

    def test_looped_bed_is_capped_at_the_timeline_length(self):
        # Without the cap, the infinite loop injects frames past the end and
        # ffmpeg exits 234. The recipe's two clips total 8.0s.
        args = vr.build_ffmpeg_command(recipe(), ["/a.mov", "/b.mov"], "/out.mp4",
                                       music_path="/bed.mp3")
        bed_i = args.index("/bed.mp3")
        # ... -stream_loop -1 -t 8.000 -i /bed.mp3
        assert args[bed_i - 1] == "-i"
        assert args[bed_i - 2] == "8.000"
        assert args[bed_i - 3] == "-t"
        assert args.index("-stream_loop") < bed_i - 3

    def test_without_a_bed_no_loop_and_no_mix(self):
        args = vr.build_ffmpeg_command(recipe(), ["/a.mov", "/b.mov"], "/out.mp4")
        assert "-stream_loop" not in args
        assert "amix" not in " ".join(args)

    def test_no_shared_silence_input(self):
        # Silence is generated per clip inside the filtergraph; there must be
        # no separate anullsrc *input* (the unbounded one broke concat).
        args = vr.build_ffmpeg_command(
            recipe([clip(1, 0, 3000, 0), clip(2, 0, 3000, 3000)]),
            ["/a.mov", "/b.mov"], "/out.mp4")
        assert "-i anullsrc" not in " ".join(args)


class TestRenderId:
    def test_prefix_and_length(self):
        rid = vr.new_render_id()
        assert rid.startswith("RND-") and len(rid) == 30

    def test_ids_sort_by_creation_time(self):
        early = vr.new_render_id(now_ms=1_700_000_000_000)
        later = vr.new_render_id(now_ms=1_800_000_000_000)
        assert early < later

    def test_no_ambiguous_characters(self):
        # Crockford base32 omits I, L, O and U so an id cannot be
        # mistranscribed from a screen.
        body = vr.new_render_id()[4:]
        assert not (set(body) & set("ILOU"))

    def test_ids_are_unique(self):
        assert len({vr.new_render_id() for _ in range(200)}) == 200


class TestExportLayout:
    def test_path_shape(self):
        when = dt.datetime(2026, 7, 26, 4, 30)
        assert vr.export_prefix("RND-ABC", "prod", when) == \
            "ugc-assets/exported/prod/2026/07/26/RND-ABC/"

    def test_always_under_the_export_prefix(self):
        when = dt.datetime(2026, 7, 26)
        for env in ("dev", "staging", "prod"):
            assert vr.export_prefix("RND-X", env, when).startswith("ugc-assets/exported/")

    def test_unknown_environment_is_rejected(self):
        with pytest.raises(ValueError):
            vr.export_prefix("RND-X", "production", dt.datetime(2026, 7, 26))


class TestExportWriteGuard:
    """
    The failure this prevents is unrecoverable, so it is checked
    exhaustively rather than representatively.
    """

    @pytest.mark.parametrize("key", [
        "ugc-assets/b-roll/food/pizza/new-york/lindustrie_001.mov",
        "ugc-assets/app/new-york/guide/app_newyork_best_pizza.mov",
        "ugc-assets/reactions/surprised/clip.mp4",
        "ugc-assets/music/track.mp3",
        "ugc-assets/captions/x.srt",
        "ugc-assets/",
        "other-bucket-path/final.mp4",
        "",
    ])
    def test_writes_outside_the_export_prefix_are_refused(self, key):
        with pytest.raises(ExportPathError):
            S3Exporter("big-city-travel-guide-clips").check_key(key)

    def test_traversal_out_of_the_prefix_is_refused(self):
        exporter = S3Exporter("big-city-travel-guide-clips")
        with pytest.raises(ExportPathError):
            exporter.check_key("ugc-assets/exported/../b-roll/oops.mp4")

    def test_export_keys_are_accepted(self):
        exporter = S3Exporter("big-city-travel-guide-clips")
        key = "ugc-assets/exported/dev/2026/07/26/RND-ABC/final.mp4"
        assert exporter.check_key(key) == key

    def test_exporter_exposes_no_delete_or_copy(self):
        for forbidden in ("delete_object", "delete", "copy_object", "copy",
                          "move", "rename"):
            assert not hasattr(S3Exporter, forbidden)


class TestOutputValidation:
    def good_probe(self):
        return {"width": 1080, "height": 1920, "frame_rate": 30.0,
                "video_codec": "h264", "audio_codec": "aac",
                "duration_ms": 8000, "error": None}

    def test_a_conforming_render_passes(self):
        assert vr.validate_output(self.good_probe(), recipe())["passed"]

    def test_wrong_dimensions_fail(self):
        probe = {**self.good_probe(), "width": 1920, "height": 1080}
        result = vr.validate_output(probe, recipe())
        assert not result["passed"] and "1080x1920" in result["failures"][0]

    def test_wrong_codec_fails(self):
        probe = {**self.good_probe(), "video_codec": "hevc"}
        assert not vr.validate_output(probe, recipe())["passed"]

    def test_short_render_fails(self):
        # The failure worth catching: a concat that dropped a segment
        # produces a playable file that is simply missing content.
        probe = {**self.good_probe(), "duration_ms": 3000}
        result = vr.validate_output(probe, recipe())
        assert not result["passed"]
        assert any("duration" in f for f in result["failures"])

    def test_small_duration_drift_is_tolerated(self):
        # Frame quantisation means the output is rarely exact to the ms.
        probe = {**self.good_probe(), "duration_ms": 8200}
        assert vr.validate_output(probe, recipe())["passed"]

    def test_unprobeable_output_fails(self):
        probe = {"error": "no video stream"}
        assert not vr.validate_output(probe, recipe())["passed"]


class TestManifest:
    def test_pins_every_source_by_checksum(self):
        r = recipe()
        sources = [{**c, "bucket": "big-city-travel-guide-clips"} for c in r["timeline"]]
        manifest = vr.build_manifest(
            "RND-ABC", "dev", r,
            [{"role": "final", "s3_key": "ugc-assets/exported/dev/x/final.mp4",
              "checksum_sha256": "a" * 64, "size_bytes": 1}],
            sources, created_at="2026-07-26T04:00:00Z")
        assert len(manifest["sources"]) == 2
        assert all(s["checksum_sha256"] for s in manifest["sources"])
        assert manifest["artifacts"][0]["role"] == "final"

    def test_embeds_the_recipe_for_reproducibility(self):
        r = recipe()
        manifest = vr.build_manifest("RND-ABC", "dev", r, [], [],
                                     created_at="2026-07-26T04:00:00Z")
        assert manifest["recipe"] == r
