"""
Image composition: templates load, slots fill, output is a portrait canvas.
"""
import os
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ImageComposer as ic  # noqa: E402

ALL_TEMPLATES = [
    "split-top-bottom-banner-v1",
    "full-photo-banner-lower-v1",
    "full-photo-banner-upper-v1",
    "triptych-v1",
    "quote-card-v1",
]


def _solid(w=1200, h=1600, color=(80, 120, 200)):
    return Image.new("RGB", (w, h), color)


class TestTemplates:
    @pytest.mark.parametrize("tid", ALL_TEMPLATES)
    def test_loads_and_is_portrait(self, tid):
        t = ic.load_image_template(tid)
        assert t["canvas"]["width"] == 1080
        assert t["canvas"]["height"] == 1920

    def test_unknown_template_raises(self):
        with pytest.raises(ic.ImageComposeError):
            ic.load_image_template("no-such-template")

    def test_out_of_bounds_slot_rejected(self):
        bad = {"canvas": {"width": 1080, "height": 1920},
               "image_slots": [{"name": "x", "x": 0, "y": 0, "w": 2000, "h": 100}]}
        with pytest.raises(ic.ImageComposeError):
            ic.validate_template(bad)


class TestCompose:
    @pytest.mark.parametrize("tid", ALL_TEMPLATES)
    def test_every_template_composes_to_canvas_size(self, tid):
        t = ic.load_image_template(tid)
        images = {s["name"]: _solid() for s in t.get("image_slots", [])}
        texts = {s["name"]: "Best Burgers in New York"
                 for s in t.get("text_slots", [])}
        out = ic.compose(t, images, texts)
        assert out.size == (1080, 1920)
        assert out.mode == "RGB"

    def test_cover_crop_exactly_fills_slot(self):
        cropped = ic._cover(_solid(1200, 400), 1080, 800)
        assert cropped.size == (1080, 800)

    def test_missing_required_image_raises(self):
        t = ic.load_image_template("split-top-bottom-banner-v1")
        with pytest.raises(ic.ImageComposeError):
            ic.compose(t, {"top": _solid()}, {"banner": "hi"})  # no bottom

    def test_missing_required_text_raises(self):
        t = ic.load_image_template("split-top-bottom-banner-v1")
        with pytest.raises(ic.ImageComposeError):
            ic.compose(t, {"top": _solid(), "bottom": _solid()}, {})  # no banner

    def test_optional_text_slot_may_be_empty(self):
        t = ic.load_image_template("triptych-v1")
        images = {s["name"]: _solid() for s in t["image_slots"]}
        out = ic.compose(t, images, {})   # title is optional
        assert out.size == (1080, 1920)

    def test_long_text_still_fits_without_error(self):
        t = ic.load_image_template("quote-card-v1")
        out = ic.compose(t, {}, {"quote": "word " * 200})
        assert out.size == (1080, 1920)
