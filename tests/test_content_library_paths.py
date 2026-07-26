"""
Path classification, exercised against the key shapes actually present in
the bucket as of the 2026-07-25 inventory — including the ones that do not
follow the naming standard, because those keys are never renamed.
"""

import ContentLibraryPaths as paths


class TestAppPaths:
    def test_city_and_feature(self):
        result = paths.classify("ugc-assets/app/new-york/guide/app_newyork_best_pizza.mov")
        assert result.asset_type == "app"
        assert result.city_slug == "new-york"
        assert result.feature == "guide"
        assert result.recognized

    def test_filename_spelling_does_not_override_folder(self):
        # Filenames say "newyork"; directories say "new-york". The canonical
        # slug comes from the directory and the key is never renamed.
        result = paths.classify(
            "ugc-assets/app/new-york/livetracking/app_newyork_livetracking_detail.mov")
        assert result.city_slug == "new-york"
        assert result.feature == "livetracking"

    def test_tokyo(self):
        result = paths.classify("ugc-assets/app/tokyo/map/app_tokyo_map_photos.mov")
        assert result.city_slug == "tokyo"
        assert result.feature == "map"


class TestBrollPaths:
    def test_food_has_subcategory_and_city(self):
        result = paths.classify(
            "ugc-assets/b-roll/food/pizza/new-york/lindustrie_pizza-closeup_001.mov")
        assert result.asset_type == "broll"
        assert result.category == "food"
        assert result.subcategory == "pizza"
        assert result.city_slug == "new-york"

    def test_fancy_is_preserved_not_corrected(self):
        # "fancy" mixes venue style with shot content and is not a durable
        # category, but the folder is real and review reclassifies it.
        result = paths.classify("ugc-assets/b-roll/food/fancy/new-york/clip.mov")
        assert result.subcategory == "fancy"

    def test_plural_folder_maps_to_singular_vocabulary(self):
        result = paths.classify("ugc-assets/b-roll/hotels/new-york/new-york-palace.mov")
        assert result.category == "hotel"
        assert result.city_slug == "new-york"
        assert result.subcategory is None

    def test_landmarks_alias(self):
        result = paths.classify("ugc-assets/b-roll/landmarks/new-york/empire-state.mov")
        assert result.category == "landmark"

    def test_shopping_is_already_singular(self):
        result = paths.classify("ugc-assets/b-roll/shopping/new-york/bucherer.mov")
        assert result.category == "shopping"

    def test_category_only(self):
        result = paths.classify("ugc-assets/b-roll/food/loose-clip.mov")
        assert result.category == "food"
        assert result.city_slug is None


class TestReactionPaths:
    def test_single_emotion(self):
        result = paths.classify("ugc-assets/reactions/surprised/clip_01.mp4")
        assert result.asset_type == "reaction"
        assert result.emotions == ("surprised",)

    def test_nested_folders_contribute_both_emotions(self):
        # Filing one performance under two emotions means it suits both.
        # Both labels are kept and later merged onto the canonical asset.
        result = paths.classify("ugc-assets/reactions/happy/excited/clip_02.mp4")
        assert result.emotions == ("happy", "excited")

    def test_untidy_filenames_are_accepted(self):
        # Spaces, capitals and doubled underscores exist in the real keys.
        # They are normalized in the database, never in S3.
        result = paths.classify("ugc-assets/reactions/shocked/Reaction__Shocked 01.mp4")
        assert result.asset_type == "reaction"
        assert result.filename == "Reaction__Shocked 01.mp4"
        assert result.emotions == ("shocked",)

    def test_duplicate_segments_collapse(self):
        result = paths.classify("ugc-assets/reactions/excited/excited/clip.mp4")
        assert result.emotions == ("excited",)


class TestExclusions:
    def test_trailing_slash_is_a_folder_marker(self):
        assert paths.is_folder_marker("ugc-assets/music/", 0)

    def test_zero_byte_object_is_a_folder_marker(self):
        # The bucket contains one for ugc-assets/ itself, which lists with
        # an empty name and must never become a row.
        assert paths.is_folder_marker("ugc-assets/", 0)

    def test_real_object_is_not_a_marker(self):
        assert not paths.is_folder_marker("ugc-assets/b-roll/food/pizza/x.mov", 1234)

    def test_exported_is_excluded(self):
        assert paths.is_exported("ugc-assets/exported/dev/2026/07/25/RND-01/final.mp4")

    def test_source_prefixes_are_not_exported(self):
        assert not paths.is_exported("ugc-assets/b-roll/food/pizza/x.mov")

    def test_media_extensions(self):
        assert paths.is_media("clip.mov")
        assert paths.is_media("CLIP.MOV")
        assert paths.is_media("clip.mp4")
        assert not paths.is_media("manifest.json")
        assert not paths.is_media("notes.txt")


class TestUnrecognized:
    def test_unknown_prefix_is_flagged_not_guessed(self):
        result = paths.classify("ugc-assets/somethingnew/clip.mov")
        assert result.asset_type is None
        assert not result.recognized

    def test_filename_and_folder_still_extracted(self):
        result = paths.classify("ugc-assets/somethingnew/clip.mov")
        assert result.filename == "clip.mov"
        assert result.folder == "ugc-assets/somethingnew/"


class TestSlugify:
    def test_lowercases_and_hyphenates(self):
        assert paths.slugify("New York") == "new-york"
        assert paths.slugify("live_tracking") == "live-tracking"
        assert paths.slugify("  Tokyo  ") == "tokyo"

    def test_empty_becomes_none(self):
        assert paths.slugify("") is None
        assert paths.slugify(None) is None
