"""
Image compose endpoint: builds a still, uploads to the export prefix, records it.

Exercised as a plain function; S3 read/write are monkeypatched so no bucket is
touched, and the fake db routes the two queries the endpoint issues.
"""
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
os.environ.setdefault("MEDIAMIXER_ADMIN_SECRET", "test-secret")

import api.main as main  # noqa: E402


class FakeDb:
    def __init__(self):
        self.inserted = None

    def execute_query_as_dict(self, sql, params=None):
        if "FROM public.image_library_assets" in sql and "s3_key" in sql:
            return [{"s3_key": "ugc-assets/images/new-york/burgers/x.jpg"}]
        return []

    def execute_query(self, sql, params=None):
        if "INSERT INTO public.image_renders" in sql:
            self.inserted = params
        return None


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(main, "_download_pil",
                        lambda key: Image.new("RGB", (1200, 1600), (90, 90, 90)))
    monkeypatch.setattr(main._exporter, "put_file",
                        lambda path, key, ct, overwrite=False: {
                            "size_bytes": 4242, "checksum_sha256": "deadbeef",
                            "s3_key": key})
    monkeypatch.setattr(main._s3, "presign", lambda key, **k: f"https://signed/{key}")


def test_compose_builds_records_and_exports_under_the_export_prefix(patched):
    db = FakeDb()
    body = main.ImageComposeBody(
        template_id="split-top-bottom-banner-v1",
        slots={"top": 10, "bottom": 11},
        texts={"banner": "Best Burgers in New York"},
        cityid="CIT-00000000002", topic="burgers")
    out = main.compose_image(body, db=db, _=None)

    assert out["image_id"].startswith("IMG-")
    assert out["width"] == 1080 and out["height"] == 1920
    assert out["s3_key"].startswith("ugc-assets/exported/images/")
    assert out["url"].startswith("https://signed/")
    # The render was recorded with its output key and dimensions.
    assert db.inserted["key"] == out["s3_key"]
    assert db.inserted["w"] == 1080


def test_missing_photo_is_a_400(patched, monkeypatch):
    # A slot pointing at a non-existent asset id: the SELECT returns nothing.
    monkeypatch.setattr(FakeDb, "execute_query_as_dict",
                        lambda self, sql, params=None: [])
    db = FakeDb()
    body = main.ImageComposeBody(
        template_id="split-top-bottom-banner-v1",
        slots={"top": 999, "bottom": 998}, texts={"banner": "hi"})
    with pytest.raises(main.HTTPException) as exc:
        main.compose_image(body, db=db, _=None)
    assert exc.value.status_code == 400


def test_unknown_template_is_a_400(patched):
    db = FakeDb()
    body = main.ImageComposeBody(template_id="nope-v9", slots={}, texts={})
    with pytest.raises(main.HTTPException) as exc:
        main.compose_image(body, db=db, _=None)
    assert exc.value.status_code == 400


def test_carousel_composes_a_group_in_sequence(patched):
    db = FakeDb()
    body = main.ImageCarouselBody(
        items=[
            main.CarouselItem(template_id="full-photo-banner-upper-v1",
                              slots={"photo": 10}, texts={"banner": "Cover"}),
            main.CarouselItem(template_id="full-photo-banner-lower-v1",
                              slots={"photo": 11}, texts={"banner": "Slide 2"}),
            main.CarouselItem(template_id="full-photo-banner-lower-v1",
                              slots={"photo": 12}, texts={"banner": "Slide 3"}),
        ],
        cityid="CIT-00000000002", topic="pizza")
    out = main.compose_batch(body, db=db, _=None)
    assert out["count"] == 3
    assert out["group_id"].startswith("CAR-")
    seqs = [im["sequence"] for im in out["images"]]
    assert seqs == [0, 1, 2]
    assert all(im["group_id"] == out["group_id"] for im in out["images"])
    # every slide is 9:16 here, so the sizes are uniform
    assert {(im["width"], im["height"]) for im in out["images"]} == {(1080, 1920)}


def test_carousel_rejects_mixed_canvas_sizes(patched):
    db = FakeDb()
    body = main.ImageCarouselBody(items=[
        main.CarouselItem(template_id="full-photo-banner-lower-v1",
                          slots={"photo": 10}, texts={"banner": "a"}),
        main.CarouselItem(template_id="carousel-caption-1x1-v1",
                          slots={"photo": 11}, texts={"caption": "b"}),
    ])
    with pytest.raises(main.HTTPException) as exc:
        main.compose_batch(body, db=db, _=None)
    assert exc.value.status_code == 400
    assert "canvas size" in str(exc.value.detail)


def test_empty_carousel_is_a_400(patched):
    with pytest.raises(main.HTTPException) as exc:
        main.compose_batch(main.ImageCarouselBody(items=[]), db=FakeDb(), _=None)
    assert exc.value.status_code == 400
