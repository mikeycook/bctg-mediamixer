"""
ffprobe output interpretation.

These run the pure parser against recorded shapes, so no ffprobe binary is
needed. The rotation cases are the reason this file exists: getting them
wrong silently admits sideways footage into a 9:16 render.
"""

import ContentLibraryProbe as probe


def ffprobe_json(width=1080, height=1920, duration="8.5", avg_frame_rate="30/1",
                 video_codec="h264", audio=True, tags=None, side_data=None):
    video_stream = {
        "codec_type": "video", "codec_name": video_codec,
        "width": width, "height": height, "avg_frame_rate": avg_frame_rate,
    }
    if tags:
        video_stream["tags"] = tags
    if side_data:
        video_stream["side_data_list"] = side_data

    streams = [video_stream]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"duration": duration}}


class TestOrientation:
    def test_native_portrait(self):
        result = probe.parse_probe_json(ffprobe_json(width=1080, height=1920))
        assert result["orientation"] == "portrait"
        assert (result["width"], result["height"]) == (1080, 1920)

    def test_landscape(self):
        result = probe.parse_probe_json(ffprobe_json(width=1920, height=1080))
        assert result["orientation"] == "landscape"

    def test_square(self):
        result = probe.parse_probe_json(ffprobe_json(width=1080, height=1080))
        assert result["orientation"] == "square"

    def test_rotate_tag_makes_landscape_coding_portrait(self):
        # Phones record 1920x1080 and set a rotation flag. Treating this as
        # landscape would let sideways footage into a vertical render.
        result = probe.parse_probe_json(
            ffprobe_json(width=1920, height=1080, tags={"rotate": "90"}))
        assert result["rotation"] == 90
        assert result["orientation"] == "portrait"
        assert (result["width"], result["height"]) == (1080, 1920)

    def test_display_matrix_negative_rotation(self):
        # Newer ffmpeg reports rotation in side_data_list, usually negative.
        result = probe.parse_probe_json(ffprobe_json(
            width=1920, height=1080, side_data=[{"rotation": -90}]))
        assert result["rotation"] == 270
        assert result["orientation"] == "portrait"

    def test_180_rotation_does_not_swap_dimensions(self):
        result = probe.parse_probe_json(
            ffprobe_json(width=1920, height=1080, tags={"rotate": "180"}))
        assert result["rotation"] == 180
        assert result["orientation"] == "landscape"
        assert (result["width"], result["height"]) == (1920, 1080)

    def test_unparseable_rotation_is_treated_as_none(self):
        result = probe.parse_probe_json(
            ffprobe_json(width=1920, height=1080, tags={"rotate": "sideways"}))
        assert result["rotation"] == 0
        assert result["orientation"] == "landscape"


class TestDuration:
    def test_seconds_become_milliseconds(self):
        result = probe.parse_probe_json(ffprobe_json(duration="8.5"))
        assert result["duration_ms"] == 8500

    def test_missing_duration(self):
        data = ffprobe_json()
        data["format"] = {}
        assert probe.parse_probe_json(data)["duration_ms"] is None

    def test_zero_duration_is_not_accepted(self):
        # The column is CHECK (duration_ms > 0); zero must not reach it.
        assert probe.parse_probe_json(ffprobe_json(duration="0"))["duration_ms"] is None

    def test_display_format(self):
        assert probe.format_duration_display(45000) == "0:45"
        assert probe.format_duration_display(125000) == "2:05"
        assert probe.format_duration_display(None) is None


class TestFrameRate:
    def test_simple_rational(self):
        assert probe.parse_probe_json(ffprobe_json(avg_frame_rate="30/1"))["frame_rate"] == 30.0

    def test_ntsc_rational(self):
        result = probe.parse_probe_json(ffprobe_json(avg_frame_rate="30000/1001"))
        assert result["frame_rate"] == 29.97

    def test_unknown_frame_rate(self):
        assert probe.parse_probe_json(ffprobe_json(avg_frame_rate="0/0"))["frame_rate"] is None

    def test_falls_back_to_r_frame_rate(self):
        data = ffprobe_json(avg_frame_rate="0/0")
        data["streams"][0]["r_frame_rate"] = "25/1"
        assert probe.parse_probe_json(data)["frame_rate"] == 25.0


class TestAudio:
    def test_audio_present(self):
        result = probe.parse_probe_json(ffprobe_json(audio=True))
        assert result["has_audio"] is True
        assert result["audio_codec"] == "aac"

    def test_audio_absent(self):
        result = probe.parse_probe_json(ffprobe_json(audio=False))
        assert result["has_audio"] is False
        assert result["audio_codec"] is None


class TestFailures:
    def test_no_video_stream_is_an_error(self):
        # A .mov extension does not guarantee a readable video stream.
        data = {"streams": [{"codec_type": "audio", "codec_name": "aac"}],
                "format": {"duration": "8.5"}}
        result = probe.parse_probe_json(data)
        assert result["error"] == "no video stream"
        assert result["duration_ms"] is None

    def test_empty_response_is_an_error(self):
        assert probe.parse_probe_json({})["error"] == "no video stream"

    def test_missing_ffprobe_is_reported_not_raised(self):
        # An unreadable object is an asset to quarantine, not a reason to
        # abandon the whole inventory run.
        result = probe.probe("https://example.invalid/x.mov", ffprobe="ffprobe-does-not-exist")
        assert result["error"] == "ffprobe not installed"


class TestRawOutputRetained:
    def test_probe_data_keeps_coded_dimensions(self):
        # Stored width/height are display dimensions; the coded pair stays
        # available for anything that needs it.
        result = probe.parse_probe_json(
            ffprobe_json(width=1920, height=1080, tags={"rotate": "90"}))
        assert result["width"] == 1080
        assert result["probe_data"]["streams"][0]["width"] == 1920
