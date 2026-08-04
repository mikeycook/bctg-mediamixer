"""
Image path classification: order-independent city/topic detection.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ImageLibraryPaths as p  # noqa: E402

KNOWN = {"new-york", "los-angeles"}


def test_topic_then_city_with_known_slugs():
    c = p.classify("ugc-assets/images/burgers/new-york/joes.jpg", known_city_slugs=KNOWN)
    assert c.city_slug == "new-york"
    assert c.subcategory == "burgers"
    assert c.recognized


def test_city_then_topic_with_known_slugs():
    c = p.classify("ugc-assets/images/new-york/burgers/joes.jpg", known_city_slugs=KNOWN)
    assert c.city_slug == "new-york"
    assert c.subcategory == "burgers"


def test_positional_fallback_is_topic_then_city():
    # No known set: the layout is <topic>/<city-slug>.
    c = p.classify("ugc-assets/images/burgers/new-york/joes.jpg")
    assert c.subcategory == "burgers"
    assert c.city_slug == "new-york"


def test_filename_is_never_mistaken_for_a_folder():
    c = p.classify("ugc-assets/images/burgers/new-york/joes.jpg", known_city_slugs=KNOWN)
    assert c.filename == "joes.jpg"
    assert "joes.jpg" not in (c.subcategory or "")


def test_is_image_only_matches_image_extensions():
    assert p.is_image("x/y/z.JPG")
    assert p.is_image("x/y/z.png")
    assert not p.is_image("x/y/z.mp4")
