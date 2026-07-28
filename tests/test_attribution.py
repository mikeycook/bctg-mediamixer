"""
Music credits. These guard a licence breach that is invisible until someone
audits a published Reel: a track that required attribution shipped without it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import Attribution as attribution  # noqa: E402


def track(**kw):
    base = {"title": "Sunny Days", "artist": "Some Artist",
            "attribution_required": True}
    base.update(kw)
    return base


class TestCreditLine:
    def test_explicit_attribution_text_wins(self):
        line = attribution.credit_line(track(attribution_text="Use me verbatim"))
        assert line == "Use me verbatim"

    def test_composed_from_fields_as_tasl(self):
        line = attribution.credit_line(track(
            source_url="https://youtu.be/x", license="cc_by"))
        assert line == "Sunny Days by Some Artist | https://youtu.be/x | cc_by"

    def test_prefers_urls_over_bare_names(self):
        line = attribution.credit_line(track(
            source="Audio Library", source_url="https://s/x",
            license="cc_by", license_url="https://l/by"))
        assert "https://s/x" in line and "https://l/by" in line
        assert "Audio Library" not in line and "| cc_by" not in line

    def test_title_only_still_credits(self):
        assert attribution.credit_line(
            {"title": "Sunny Days", "artist": ""}) == "Sunny Days"


class TestNeedsCredit:
    def test_cc0_needs_no_attribution(self):
        tracks = [track(attribution_required=False, license="cc0")]
        assert attribution.tracks_needing_credit(tracks) == []

    def test_a_required_credit_with_no_fields_is_dropped(self):
        # Better nothing than a blank line that looks like a credit.
        tracks = [{"attribution_required": True}]
        assert attribution.tracks_needing_credit(tracks) == []

    def test_only_required_tracks_are_listed(self):
        tracks = [track(title="A"),
                  track(title="B", attribution_required=False),
                  track(title="C")]
        lines = attribution.tracks_needing_credit(tracks)
        assert len(lines) == 2
        assert all("B by" not in line for line in lines)


class TestSidecar:
    def test_empty_when_nothing_needs_crediting(self):
        assert attribution.attribution_text([]) == ""
        assert attribution.attribution_text(None) == ""
        assert attribution.attribution_text(
            [track(attribution_required=False)]) == ""

    def test_header_and_one_line_per_track(self):
        body = attribution.attribution_text(
            [track(title="A"), track(title="B")])
        assert body.startswith("Music:\n")
        assert "A by Some Artist" in body and "B by Some Artist" in body
        assert body.endswith("\n")


class TestBurnIn:
    def test_none_when_no_attribution_needed(self):
        assert attribution.burn_in_credit([]) == ""
        assert attribution.burn_in_credit(
            [track(attribution_required=False)]) == ""

    def test_single_track_gets_a_real_credit(self):
        assert attribution.burn_in_credit([track()]) == "♪ Sunny Days — Some Artist"

    def test_several_tracks_point_to_the_description(self):
        line = attribution.burn_in_credit([track(title="A"), track(title="B")])
        assert line == "♪ Music credits in description"
