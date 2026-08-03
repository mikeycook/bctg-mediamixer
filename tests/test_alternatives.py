"""
Swap alternatives: reactions offer the whole library.

A reaction performance is not tied to a place, a topic, or a length, so when
an editor swaps a reaction the endpoint returns every reaction — including
ones shorter than the slot and ones tagged with a different emotion — rather
than only slot-fitting matches. Other asset types stay scoped and keep the
duration floor.

The endpoint is exercised as a plain function (FastAPI's Depends are bypassed
by passing db/_ directly), so no test HTTP client or DB is needed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
os.environ.setdefault("MEDIAMIXER_ADMIN_SECRET", "test-secret")

import api.main as main  # noqa: E402


def _clip(pk, take_ms, asset_type="reaction"):
    return {"role": f"{asset_type}_1", "asset_pk": pk, "_asset_type": asset_type,
            "source_in_ms": 0, "source_out_ms": take_ms, "timeline_in_ms": 0}


def _asset(pk, duration_ms, asset_type="reaction"):
    return {
        "id": pk, "asset_id": f"UGC-{pk:05d}", "s3_key": f"x/{pk}.mov",
        "s3_version_id": None, "checksum_sha256": f"sha{pk}",
        "asset_type": asset_type, "duration_ms": duration_ms,
        "orientation": "portrait", "place_name": None, "subcategory": None,
        "notes": None,
    }


class FakeDb:
    """Routes the endpoint's three queries by inspecting the SQL text."""

    def __init__(self, current_clip, library):
        self.current_clip = current_clip
        self.library = library

    def execute_query_as_dict(self, sql, params=None):
        params = params or {}
        if "content_library_renders" in sql and "recipe" in sql:
            return [{"recipe": {"brief": {"cityid": "CIT-00000000002"},
                                "timeline": [self.current_clip]}}]
        if "asset_type FROM public.content_library_assets" in sql:
            return [{"asset_type": self.current_clip["_asset_type"]}]
        # ELIGIBLE_SQL: honour the min_ms / asset_type filters the real SQL applies.
        types = params.get("types", [])
        min_ms = params.get("min_ms", 0)
        return [a for a in self.library
                if a["asset_type"] in types and a["duration_ms"] >= min_ms]


def _call(monkeypatch, current_clip, library):
    monkeypatch.setattr(main._s3, "presign", lambda key, **k: f"https://signed/{key}")
    db = FakeDb(current_clip, library)
    return main.render_alternatives("RID-1", sequence_no=0, db=db, _=None)


def test_reaction_swap_is_unrestricted_and_includes_short_and_off_length_clips(monkeypatch):
    current = _clip(100, 2500)                      # 2.5s reaction in the slot
    library = [_asset(100, 2500), _asset(101, 1200),  # 101 is shorter than take
               _asset(102, 5000)]                     # 102 is longer
    body = _call(monkeypatch, current, library)
    assert body["unrestricted"] is True
    ids = {a["id"] for a in body["alternatives"]}
    assert 100 not in ids            # the current clip is never its own alternative
    assert ids == {101, 102}         # incl. the short one the old floor hid


def test_non_reaction_swap_stays_scoped_and_keeps_the_duration_floor(monkeypatch):
    current = _clip(200, 4000, asset_type="app")    # 4s app clip in the slot
    library = [_asset(200, 4000, "app"), _asset(201, 1500, "app"),  # too short
               _asset(202, 6000, "app")]
    body = _call(monkeypatch, current, library)
    assert body["unrestricted"] is False
    ids = {a["id"] for a in body["alternatives"]}
    assert 201 not in ids            # below the 4s take — excluded
    assert ids == {202}
