"""
Music metadata lifted from ID3 tags. Guards the case-insensitive tag lookup
and the BPM coercion the sync relies on to auto-fill a new track.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.main import _tag, _to_number, _title_from_key  # noqa: E402


class TestTitleFromKey:
    def test_percent_decoded_and_extension_removed(self):
        assert _title_from_key(
            "ugc-assets/music/Ancient%20History%20-%20Bosley.mp3") == "Ancient History - Bosley"

    def test_query_string_after_a_question_mark_is_dropped(self):
        assert _title_from_key(
            "ugc-assets/music/Sunny%20Days.mp3?versionId=abc123") == "Sunny Days"

    def test_only_a_known_audio_extension_is_stripped(self):
        # A dot mid-title is not an extension and must survive.
        assert _title_from_key("ugc-assets/music/Track No. 5.wav") == "Track No. 5"

    def test_plain_name_passes_through(self):
        assert _title_from_key("ugc-assets/music/Roberta's Theme.m4a") == "Roberta's Theme"


class TestTagLookup:
    def test_case_insensitive(self):
        assert _tag({"TITLE": "Sunny Days"}, "title") == "Sunny Days"

    def test_first_non_empty_candidate_wins(self):
        tags = {"artist": "  ", "album_artist": "Real Artist"}
        assert _tag(tags, "artist", "album_artist") == "Real Artist"

    def test_missing_returns_none(self):
        assert _tag({"genre": "Lo-fi"}, "mood") is None

    def test_empty_or_none_tags(self):
        assert _tag(None, "title") is None
        assert _tag({}, "title") is None

    def test_values_are_stripped(self):
        assert _tag({"genre": "  Jazz  "}, "genre") == "Jazz"


class TestBpmCoercion:
    def test_numeric_string_parses(self):
        assert _to_number("92", float) == 92.0

    def test_blank_and_none_are_none(self):
        assert _to_number("", float) is None
        assert _to_number(None, float) is None

    def test_garbage_is_none_not_an_error(self):
        assert _to_number("fast", float) is None
