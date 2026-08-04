"""
render_detail resolves caption text for the editor.

A fresh render stores caption_specs (patterns), not resolved text — the worker
resolves them at render time and does not persist them. render_detail resolves
them on read so the editor pre-fills each clip's text box with what was burned
in, instead of the operator retyping it. Edited renders (captions_frozen) keep
their literal captions untouched.

Exercised as a plain function; FastAPI's Depends are bypassed by passing db/_.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
os.environ.setdefault("MEDIAMIXER_ADMIN_SECRET", "test-secret")

import api.main as main  # noqa: E402


def _recipe(captions=None, frozen=False):
    r = {
        "brief": {"cityid": "CIT-00000000002", "caption_overrides": {}},
        "caption_specs": [
            {"role": "app_part_1", "pattern": "{subtype}", "style": "label",
             "lead_in_ms": 200},
            {"role": "cta", "pattern": "Find it in Big City Travel Guide",
             "style": "cta", "lead_in_ms": 100},
        ],
        "timeline": [
            {"role": "app_part_1", "asset_pk": 10, "source_in_ms": 0,
             "source_out_ms": 6000, "timeline_in_ms": 0},
            {"role": "cta", "asset_pk": 11, "source_in_ms": 0,
             "source_out_ms": 3000, "timeline_in_ms": 6000},
        ],
        "captions": captions if captions is not None else [],
    }
    if frozen:
        r["captions_frozen"] = True
    return r


class FakeDb:
    def __init__(self, recipe):
        self.recipe = recipe

    def execute_query_as_dict(self, sql, params=None):
        if "FROM public.content_library_renders" in sql:
            return [{"id": 1, "render_id": "RID-1", "recipe": self.recipe,
                     "created_at": "t", "completed_at": "t"}]
        if "FROM public.content_library_render_artifacts" in sql:
            return []
        if "content_library_render_assets" in sql:
            return []
        # plan_for_recipe's fact lookup
        if "LEFT JOIN public.cities_reference" in sql:
            return [{"id": 10, "place_name": "Joe's", "category": "Food",
                     "subcategory": "burgers", "subtype": "Features",
                     "neighborhood": None, "cityname": "New York"},
                    {"id": 11, "place_name": None, "category": None,
                     "subcategory": None, "subtype": None,
                     "neighborhood": None, "cityname": "New York"}]
        return []


def _call(monkeypatch, recipe):
    monkeypatch.setattr(main._s3, "presign", lambda key, **k: f"https://signed/{key}")
    return main.render_detail("RID-1", db=FakeDb(recipe), _=None)


def test_specs_are_resolved_into_captions_for_prefill(monkeypatch):
    body = _call(monkeypatch, _recipe())
    caps = body["render"]["recipe"]["captions"]
    texts = [c["text"] for c in caps]
    assert "Features" in texts                     # {subtype} resolved
    assert "Find it in Big City Travel Guide" in texts
    # The app_part_1 caption falls inside the first clip's window (0–6000ms).
    app_cap = next(c for c in caps if c["text"] == "Features")
    assert 0 <= app_cap["start_ms"] < 6000


def test_frozen_captions_are_left_untouched(monkeypatch):
    frozen = _recipe(captions=[{"text": "Hand written", "start_ms": 100,
                                "end_ms": 2000, "style": "label"}], frozen=True)
    body = _call(monkeypatch, frozen)
    caps = body["render"]["recipe"]["captions"]
    assert [c["text"] for c in caps] == ["Hand written"]
